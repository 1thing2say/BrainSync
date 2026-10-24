"""Run Meta TRIBE v2 on one clip (video + audio, no text) and save its prediction rows.

Needs the TRIBE venv (torch < 2.7, tribev2 pinned in README); everything heavy is imported
inside predict_clip so the rest of brainsync never loads it. Two changes from the stock
pipeline, both checked against the CPU fp32 path on 2026-10-24:

* attention runs over 1024-query chunks. torch 2.6's MPS SDPA builds the whole
  (heads, 8192, 8192) matrix for V-JEPA2's 8192 tokens (a 6 GB allocation that pushed the M4
  into swap); chunking is exact and also bounds memory on the Jetson's 8 GB.
* on a GPU the video model runs in fp16 (pooled features within 0.1% of CPU fp32).

The text extractor (gated Llama-3.2-3B, WhisperX) is never built: without word events TRIBE
drops it, as it was trained with modality dropout.

Writes <out>/preds.npy (float16, rows x 20484 fsaverage5 vertices, left hemisphere first) and
<out>/meta.json (frame times, clip hash, device, dtype, versions).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DEVICES = ("auto", "cpu", "mps", "cuda")
REPO_ID = "facebook/tribev2"
ATTN_CHUNK = 1024
N_VERTICES = 20484

_patched = False


def resolve_device(name: str) -> str:
    """'auto' picks cuda, then mps, then cpu; an explicit device must be available."""
    import torch

    if name not in DEVICES:
        raise ValueError(f"device must be one of {', '.join(DEVICES)}, got {name!r}")
    if name == "auto":
        if torch.cuda.is_available():
            return "cuda"
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if name == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("--device cuda but torch sees no CUDA device")
    if name == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("--device mps but torch sees no Metal device")
    return name


def chunked_sdpa(module, query, key, value, attention_mask, dropout=0.0, scaling=None,
                 is_causal=None, **kwargs):
    """Exact scaled dot-product attention over query chunks (transformers attention API).

    Never holds more than (heads, ATTN_CHUNK, keys) scores; returns (batch, seq, heads, dim).
    """
    import torch

    if attention_mask is not None or is_causal or dropout:
        raise ValueError("chunked_sdpa supports unmasked, non-causal inference only")
    outs = [torch.nn.functional.scaled_dot_product_attention(q, key, value, scale=scaling)
            for q in query.split(ATTN_CHUNK, dim=2)]
    return torch.cat(outs, dim=2).transpose(1, 2).contiguous(), None


def _half_inputs(module, args, kwargs):
    import torch

    def cast(v):
        return v.half() if torch.is_tensor(v) and v.is_floating_point() else v
    return tuple(cast(a) for a in args), {k: cast(v) for k, v in kwargs.items()}


def _free_gpu_cache() -> None:
    import torch

    if torch.backends.mps.is_available():
        torch.mps.empty_cache()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _patch_video_model(fp16: bool) -> None:
    """Make neuralset's V-JEPA2 wrapper fit a laptop or Jetson GPU.

    Chunked attention (and fp16 when asked), and each pass's stacked hidden states (41 layers
    x 8192 tokens) go to the CPU at once with the GPU cache emptied: left on MPS they piled up
    pass after pass (5.9 -> 13.8 GB in 3 passes) until the process died.
    """
    global _patched
    from neuralset.extractors import video
    from transformers import AttentionInterface

    AttentionInterface.register("chunked_sdpa", chunked_sdpa)
    cls = video._HFVideoModel
    if _patched:
        cls.__init__ = cls.__init__.__wrapped__
        cls.predict_hidden_states = cls.predict_hidden_states.__wrapped__
    init, hidden_states = cls.__init__, cls.predict_hidden_states

    def __init__(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.model.set_attn_implementation("chunked_sdpa")
        if fp16:
            self.model.half()
            self.model.register_forward_pre_hook(_half_inputs, with_kwargs=True)

    def predict_hidden_states(self, *args, **kwargs):
        out = hidden_states(self, *args, **kwargs).cpu()
        _free_gpu_cache()
        return out

    __init__.__wrapped__ = init
    predict_hidden_states.__wrapped__ = hidden_states
    cls.__init__ = __init__
    cls.predict_hidden_states = predict_hidden_states
    _patched = True


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def predict_clip(video: Path, out_dir: Path, *, device: str = "auto", fp16: bool | None = None,
                 cache_dir: Path, progress=print) -> dict:
    """Predict one clip into out_dir; returns the meta dict also written to meta.json.

    fp16 defaults to True on a GPU and False on the CPU. cache_dir holds TRIBE's feature cache
    (per device and dtype: its cache key ignores both) and scratch files.
    """
    from importlib import metadata

    import numpy as np
    import pandas as pd
    import torch
    from huggingface_hub import hf_hub_download
    from tribev2.demo_utils import TribeModel, get_audio_and_text_events

    video = Path(video).resolve()
    if not video.is_file():
        raise FileNotFoundError(f"no clip at {video}")
    device = resolve_device(device)
    fp16 = device != "cpu" if fp16 is None else fp16
    if fp16 and device == "cpu":
        raise ValueError("fp16 needs a GPU device (mps or cuda)")
    dtype = "fp16" if fp16 else "fp32"
    _patch_video_model(fp16)

    revision = Path(hf_hub_download(REPO_ID, "config.yaml")).parent.name
    features = Path(cache_dir) / "features" / f"{device}-{dtype}"
    progress(f"loading TRIBE v2 ({REPO_ID}@{revision[:10]}) on {device} {dtype}")
    model = TribeModel.from_pretrained(
        REPO_ID, cache_folder=str(features), device=device,
        config_update={
            "infra.folder": str(Path(cache_dir) / "infra"),
            "data.num_workers": 0,
            "data.text_feature.device": "cpu",
            "data.audio_feature.device": "cpu",
            "data.video_feature.image.device": "cpu",
        },
    )
    if device != "cpu":
        # the extractor configs only accept auto/cpu/cuda; set the device after validation
        object.__setattr__(model.data.audio_feature, "device", device)
        object.__setattr__(model.data.video_feature.image, "device", device)

    event = {"type": "Video", "filepath": str(video), "start": 0, "timeline": "default",
             "subject": "default"}
    events = get_audio_and_text_events(pd.DataFrame([event]), audio_only=True)
    t0 = time.perf_counter()
    preds, segments = model.predict(events, verbose=False)
    seconds = time.perf_counter() - t0
    if preds.ndim != 2 or preds.shape[1] != N_VERTICES or not len(preds):
        raise RuntimeError(f"TRIBE returned shape {preds.shape}, expected (rows, {N_VERTICES})")
    if not np.isfinite(preds).all():
        raise RuntimeError("TRIBE returned non-finite predictions")

    clips = events[events["type"] == "Video"]          # clips over 60 s arrive in chunks
    duration = float((clips["start"] + clips["duration"]).max())
    meta = {
        "clip": video.name,
        "clip_sha256": sha256(video),
        "duration_s": round(duration, 3),
        "frame_times_s": [round(float(s.start), 3) for s in segments],
        "device": device,
        "dtype": dtype,
        "predict_s": round(seconds, 1),
        "model": {"repo": REPO_ID, "revision": revision, "modalities": ["video", "audio"],
                  "tribev2": metadata.version("tribev2"),
                  "torch": torch.__version__},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "preds.npy", preds.astype(np.float16))
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta
