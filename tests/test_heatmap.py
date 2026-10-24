"""brainsync.heatmap: manifest format, frame lookup, rendering on a toy mesh, chunked attention,
the CLI, and that neither the decoder nor a plain import pulls in torch/tribev2."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync import paths
from brainsync.heatmap import manifest
from brainsync.heatmap.__main__ import main
from brainsync.heatmap.predict import sha256

PACKAGE = Path(__file__).resolve().parent.parent / "brainsync"


def good_manifest(n: int = 3, **over) -> dict:
    m = {"format": 1, "clip_id": "beach", "duration_s": float(n),
         "frame_times_s": [float(k) for k in range(n)],
         "frames": [f"frames/{k:04d}.png" for k in range(n)], "vmax": 0.4,
         "label": manifest.LABEL, "credit": manifest.CREDIT, "model": {"repo": "facebook/tribev2"}}
    m.update(over)
    return m


def write_clip(clips: Path, m: dict) -> Path:
    d = clips / m["clip_id"]
    (d / "frames").mkdir(parents=True)
    for f in m["frames"]:
        (d / f).write_bytes(b"png")
    (d / manifest.MANIFEST).write_text(json.dumps(m))
    return d


# ---------------------------------------------------------------- manifest

@pytest.mark.parametrize("t, want", [(-1, 0), (0, 0), (0.99, 0), (1, 1), (2.5, 2), (4, 4), (99, 4),
                                     (float("nan"), 0), (float("inf"), 0)])
def test_frame_at_matches_panel_js(t: float, want: int) -> None:
    # same table as extension/heatmap/test/panel.test.mjs
    assert manifest.frame_at(t, [0, 1, 2, 3, 4]) == want


def test_frame_at_uses_the_frame_times_not_row_numbers() -> None:
    # rows are already shifted for the haemodynamic lag: no second shift here
    assert manifest.frame_at(5, [2, 7]) == 0
    assert manifest.frame_at(7, [2, 7]) == 1
    with pytest.raises(ValueError, match="no frames"):
        manifest.frame_at(1, [])


def test_good_manifest_has_no_problems(tmp_path: Path) -> None:
    m = good_manifest()
    assert manifest.problems(m) == []
    d = write_clip(tmp_path, m)
    assert manifest.load(d)["clip_id"] == "beach"


@pytest.mark.parametrize("over, msg", [
    ({"format": 2}, "format"),
    ({"clip_id": "Beach Day"}, "clip id"),
    ({"frames": []}, "no frames"),
    ({"frame_times_s": [0.0, 1.0]}, "3 frames but 2"),
    ({"frame_times_s": [0.0, 2.0, 1.0]}, "increase"),
    ({"frame_times_s": [0.0, float("nan"), 2.0]}, "finite"),
    ({"duration_s": 0}, "duration_s"),
    ({"vmax": -1}, "vmax"),
    ({"vmax": True}, "wrong type"),
    ({"label": "Your brain on reels"}, "not the wearer's EEG"),
    ({"frames": ["../x.png", "frames/0001.png", "frames/0002.png"]}, "relative path"),
])
def test_problems_name_what_is_wrong(over: dict, msg: str) -> None:
    assert any(msg in p for p in manifest.problems(good_manifest(**over)))


def test_missing_keys_and_missing_files(tmp_path: Path) -> None:
    m = good_manifest()
    del m["credit"]
    assert manifest.problems(m) == ["missing 'credit'"]
    d = write_clip(tmp_path, good_manifest(video="clip.mp4"))
    (d / "frames" / "0001.png").unlink()
    with pytest.raises(ValueError) as err:
        manifest.load(d)
    assert "frames/0001.png is missing" in str(err.value)
    assert "clip.mp4 is missing" in str(err.value)


def test_write_index_lists_valid_clips_and_refuses_bad_ones(tmp_path: Path) -> None:
    write_clip(tmp_path, good_manifest(clip_id="b"))
    write_clip(tmp_path, good_manifest(clip_id="a"))
    (tmp_path / "not-a-clip").mkdir()
    assert manifest.write_index(tmp_path) == ["a", "b"]
    index = json.loads((tmp_path / manifest.INDEX).read_text())
    assert index["clips"] == [{"clip_id": "a", "manifest": "a/manifest.json"},
                              {"clip_id": "b", "manifest": "b/manifest.json"}]
    write_clip(tmp_path, good_manifest(clip_id="c", vmax=0))
    with pytest.raises(ValueError, match="vmax"):
        manifest.write_index(tmp_path)


def test_heatmaps_dir_follows_the_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path))
    assert paths.heatmaps_dir() == tmp_path / "heatmaps"


# ---------------------------------------------------------------- isolation

def test_importing_the_package_loads_no_model_code() -> None:
    code = ("import sys, brainsync.heatmap, brainsync.heatmap.manifest, brainsync.heatmap.render, "
            "brainsync.heatmap.predict, brainsync.heatmap.__main__; "
            "print(sorted(m for m in ('torch', 'tribev2', 'nilearn', 'transformers') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_nothing_outside_the_heatmap_package_imports_it() -> None:
    for py in PACKAGE.rglob("*.py"):
        if "heatmap" in py.parts:
            continue
        text = py.read_text()
        assert "heatmap" not in text or "heatmaps_dir" in text, f"{py} mentions heatmap"


# ---------------------------------------------------------------- render (toy mesh)

def octahedron(shift: float):
    from brainsync.heatmap import render

    v = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]], float)
    faces = []
    for a in (0, 1):
        for b in (2, 3):
            for c in (4, 5):
                n = np.cross(v[b] - v[a], v[c] - v[a])
                faces.append([a, b, c] if n @ v[[a, b, c]].mean(0) > 0 else [a, c, b])
    return render.Hemisphere(v + [shift, 0, 0], np.array(faces), np.linspace(-1, 1, 6))


def toy_mesh() -> dict:
    return {"left": octahedron(-2.0), "right": octahedron(2.0)}


def test_projection_keeps_only_faces_toward_the_camera() -> None:
    from brainsync.heatmap import render

    hemi = octahedron(0.0)
    for cam in (-1.0, 1.0):
        proj = render.project(hemi, cam)
        assert len(proj.faces) == 4
        assert all(hemi.coords[f].mean(0)[0] * cam > 0 for f in proj.faces)
        assert np.all((proj.shade > 0) & (proj.shade <= 1))
    assert render.camera_x("left", "lateral") == -1 and render.camera_x("left", "medial") == 1
    assert render.camera_x("right", "lateral") == 1 and render.camera_x("right", "medial") == -1


def test_colours_fade_to_grey_near_zero() -> None:
    import matplotlib
    from brainsync.heatmap import render

    proj = render.project(octahedron(0.0), 1.0)
    cmap = matplotlib.colormaps[render.CMAP]
    grey = render.face_rgb(np.zeros(6), proj, 1.0, cmap)
    np.testing.assert_allclose(grey, np.repeat(grey[:, :1], 3, axis=1))
    hot = render.face_rgb(np.full(6, 5.0), proj, 1.0, cmap)
    assert np.all(hot[:, 0] > hot[:, 2])


def make_preds(tmp_path: Path, rows: int = 3, video: Path | None = None) -> Path:
    d = tmp_path / "preds"
    d.mkdir()
    rng = np.random.default_rng(0)
    np.save(d / "preds.npy", rng.normal(size=(rows, 12)).astype(np.float16))
    meta = {"clip": "clip.mp4", "clip_sha256": sha256(video) if video else "0" * 64,
            "duration_s": float(rows), "frame_times_s": [float(k) for k in range(rows)],
            "device": "cpu", "dtype": "fp32", "predict_s": 1.0,
            "model": {"repo": "facebook/tribev2", "revision": "abc", "modalities": ["video", "audio"]}}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def test_render_clip_writes_frames_and_a_valid_manifest(tmp_path: Path) -> None:
    from brainsync.heatmap import render

    video = tmp_path / "beach.MP4"
    video.write_bytes(b"not really a video")
    preds = make_preds(tmp_path, video=video)
    clip_dir = tmp_path / "clips" / "beach"
    m = render.render_clip(preds, clip_dir, clip_id="beach", video=video, mesh=toy_mesh(),
                           size=(120, 100))
    assert m["frames"] == ["frames/0000.png", "frames/0001.png", "frames/0002.png"]
    assert m["video"] == "clip.mp4" and (clip_dir / "clip.mp4").is_file()
    assert m["model"]["device"] == "cpu" and m["label"] == manifest.LABEL
    assert (clip_dir / "frames" / "0000.png").read_bytes()[:4] == b"\x89PNG"
    assert manifest.load(clip_dir)["vmax"] == m["vmax"] > 0

    (clip_dir / "frames" / "0009.png").write_bytes(b"stale")
    render.render_clip(preds, clip_dir, clip_id="beach", mesh=toy_mesh(), size=(120, 100))
    assert sorted(p.name for p in (clip_dir / "frames").iterdir()) == ["0000.png", "0001.png", "0002.png"]


def test_render_refuses_the_wrong_video_or_shape(tmp_path: Path) -> None:
    from brainsync.heatmap import render

    preds = make_preds(tmp_path)
    other = tmp_path / "other.mp4"
    other.write_bytes(b"x")
    with pytest.raises(ValueError, match="not the clip"):
        render.render_clip(preds, tmp_path / "c", clip_id="c", video=other, mesh=toy_mesh())
    np.save(preds / "preds.npy", np.zeros((3, 10), np.float16))
    with pytest.raises(ValueError, match="expected"):
        render.render_clip(preds, tmp_path / "c", clip_id="c", mesh=toy_mesh())


def test_clip_vmax_is_never_zero() -> None:
    from brainsync.heatmap import render

    assert render.clip_vmax(np.zeros((2, 4))) == 1.0
    assert render.clip_vmax(np.array([[0.0, -2.0], [1.0, 0.5]])) == pytest.approx(
        np.percentile([0, 2, 1, 0.5], render.VMAX_PERCENTILE))


# ---------------------------------------------------------------- chunked attention

@pytest.mark.parametrize("chunk", [1, 7, 64, 1024])
def test_chunked_sdpa_equals_full_sdpa(chunk: int, monkeypatch: pytest.MonkeyPatch) -> None:
    torch = pytest.importorskip("torch")
    from brainsync.heatmap import predict

    monkeypatch.setattr(predict, "ATTN_CHUNK", chunk)
    g = torch.Generator().manual_seed(0)
    q, k, v = (torch.randn(2, 3, 50, 8, generator=g) for _ in range(3))
    out, probs = predict.chunked_sdpa(None, q, k, v, None, scaling=0.3)
    want = torch.nn.functional.scaled_dot_product_attention(q, k, v, scale=0.3).transpose(1, 2)
    assert probs is None and out.shape == (2, 50, 3, 8)
    torch.testing.assert_close(out, want)
    with pytest.raises(ValueError, match="unmasked"):
        predict.chunked_sdpa(None, q, k, v, torch.ones(50, 50, dtype=torch.bool))


def test_resolve_device_rejects_unknown_names() -> None:
    pytest.importorskip("torch")
    from brainsync.heatmap.predict import resolve_device

    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") in ("cpu", "mps", "cuda")
    with pytest.raises(ValueError, match="device must be"):
        resolve_device("tpu")


# ---------------------------------------------------------------- CLI

def test_cli_check_and_index(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["check", "--clips", str(tmp_path / "none")]) == 1
    assert main(["check", "--clips", str(tmp_path)]) == 1          # no clips yet
    write_clip(tmp_path, good_manifest(clip_id="beach"))
    assert main(["check", "--clips", str(tmp_path)]) == 0
    assert "beach: 3 frames over 3 s" in capsys.readouterr().out
    assert main(["index", "--clips", str(tmp_path)]) == 0
    assert (tmp_path / manifest.INDEX).is_file()
    write_clip(tmp_path, good_manifest(clip_id="bad", label="no disclaimer"))
    assert main(["check", "--clips", str(tmp_path)]) == 1


def test_cli_rejects_a_bad_clip_id(capsys: pytest.CaptureFixture) -> None:
    assert main(["predict", "x.mp4", "--clip-id", "Beach Day"]) == 2
    assert "clip id" in capsys.readouterr().err
    assert main(["render", "../escape"]) == 2
