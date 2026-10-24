"""The on-disk format the heatmap panel reads: one manifest.json per clip plus an index.json.

    <clips>/index.json             {"clips": [{"clip_id": "smoke", "manifest": "smoke/manifest.json"}]}
    <clips>/<id>/manifest.json     see REQUIRED below
    <clips>/<id>/frames/0000.png   one frame per prediction row

frame_times_s are the stimulus times the rows describe (TRIBE has already shifted them for the
haemodynamic lag), so the frame for video time t is the last one starting at or before t.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

INDEX = "index.json"
MANIFEST = "manifest.json"
FORMAT = 1
LABEL = ("Predicted average-brain fMRI response (Meta TRIBE v2, video + audio only). "
         "Not your EEG.")
CREDIT = "TRIBE v2 by Meta FAIR, CC BY-NC 4.0"
CLIP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REQUIRED = {
    "format": int,
    "clip_id": str,
    "duration_s": (int, float),
    "frame_times_s": list,
    "frames": list,
    "vmax": (int, float),
    "label": str,
    "credit": str,
    "model": dict,
}


def clip_id_problem(clip_id: str) -> str:
    """'' for a usable clip id (lowercase letters, digits, _ and -), else why not."""
    if not CLIP_ID.match(clip_id):
        return (f"clip id {clip_id!r} must be 1-64 lowercase letters, digits, '_' or '-', "
                "starting with a letter or digit")
    return ""


def frame_at(t: float, frame_times: list[float]) -> int:
    """Index of the frame shown at video time t: the last one starting at or before t.

    Before the first frame (or for a non-finite t) this is frame 0; past the end, the last.
    """
    if not frame_times:
        raise ValueError("no frames")
    if not math.isfinite(t) or t < frame_times[0]:
        return 0
    lo, hi = 0, len(frame_times) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if frame_times[mid] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def problems(manifest: dict, clip_dir: Path | None = None) -> list[str]:
    """Everything wrong with a manifest; empty when the panel can use it.

    With clip_dir, also checks that every frame (and the video, if named) exists there.
    """
    out = []
    for key, kind in REQUIRED.items():
        if key not in manifest:
            out.append(f"missing {key!r}")
        elif not isinstance(manifest[key], kind) or isinstance(manifest[key], bool):
            out.append(f"{key!r} has the wrong type ({type(manifest[key]).__name__})")
    if out:
        return out
    if manifest["format"] != FORMAT:
        out.append(f"format {manifest['format']} is not {FORMAT}")
    reason = clip_id_problem(manifest["clip_id"])
    if reason:
        out.append(reason)
    times, frames = manifest["frame_times_s"], manifest["frames"]
    if not frames:
        out.append("no frames")
    if len(times) != len(frames):
        out.append(f"{len(frames)} frames but {len(times)} frame times")
    if any(not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t)
           for t in times):
        out.append("frame times must be finite numbers")
    elif any(b <= a for a, b in zip(times, times[1:])):
        out.append("frame times must increase")
    if not manifest["duration_s"] > 0:
        out.append(f"duration_s must be positive, got {manifest['duration_s']}")
    if not manifest["vmax"] > 0:
        out.append(f"vmax must be positive, got {manifest['vmax']}")
    if "Not your EEG" not in manifest["label"]:
        out.append("label must say the map is not the wearer's EEG")
    names = list(frames) + ([manifest["video"]] if "video" in manifest else [])
    for name in names:
        if not isinstance(name, str) or name.startswith("/") or ".." in Path(name).parts:
            out.append(f"{name!r} must be a relative path inside the clip directory")
        elif clip_dir is not None and not (clip_dir / name).is_file():
            out.append(f"{name} is missing from {clip_dir}")
    return out


def load(clip_dir: Path) -> dict:
    """The manifest in clip_dir; raises ValueError listing its problems."""
    path = clip_dir / MANIFEST
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} is not a JSON object")
    found = problems(manifest, clip_dir)
    if found:
        raise ValueError(f"{path}: " + "; ".join(found))
    return manifest


def write_index(clips_dir: Path) -> list[str]:
    """Rewrite clips_dir/index.json from the valid clip directories; returns their ids.

    Raises ValueError naming any clip directory whose manifest is invalid.
    """
    ids, bad = [], []
    for sub in sorted(p for p in clips_dir.iterdir() if (p / MANIFEST).is_file()):
        try:
            ids.append(load(sub)["clip_id"])
        except ValueError as exc:
            bad.append(str(exc))
    if bad:
        raise ValueError("; ".join(bad))
    index = {"format": FORMAT,
             "clips": [{"clip_id": i, "manifest": f"{i}/{MANIFEST}"} for i in ids]}
    (clips_dir / INDEX).write_text(json.dumps(index, indent=1) + "\n")
    return ids
