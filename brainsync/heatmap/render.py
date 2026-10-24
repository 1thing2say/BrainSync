"""Draw TRIBE v2 prediction rows on the fsaverage5 inflated cortex, one PNG per row.

Four fixed views (left/right x lateral/medial) with matplotlib's Agg backend. The mesh is
projected once per view: only triangles facing the camera are kept, sorted far to near, so a
frame is just new face colours, well under a second each. One symmetric colour scale per clip
(99th percentile of |prediction|); values near zero fade to the grey, sulcus-shaded anatomy.

Needs nilearn for the mesh (it ships fsaverage5, no download); tests pass their own mesh.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import manifest
from .predict import sha256

VIEWS = (("left", "lateral"), ("right", "lateral"), ("left", "medial"), ("right", "medial"))
SIZE_PX = (480, 400)
DPI = 100
BACKGROUND = "#0b0b0f"
CMAP = "RdBu_r"
VMAX_PERCENTILE = 99.0
FADE = (0.15, 0.5)          # |value| / vmax: grey below the first, full colour above the second
FOOTER = "TRIBE v2 prediction · average brain · not your EEG"


@dataclass(frozen=True)
class Hemisphere:
    coords: np.ndarray      # (n, 3) inflated surface, RAS mm
    faces: np.ndarray       # (m, 3) vertex indices, outward (counter-clockwise) winding
    sulc: np.ndarray        # (n,) sulcal depth, > 0 in sulci


def load_fsaverage5() -> dict[str, Hemisphere]:
    from nilearn import datasets

    mesh = datasets.load_fsaverage("fsaverage5")["inflated"]
    sulc = datasets.load_fsaverage_data(mesh="fsaverage5", data_type="sulcal")
    return {h: Hemisphere(np.asarray(mesh.parts[h].coordinates, float),
                          np.asarray(mesh.parts[h].faces, int),
                          np.asarray(sulc.data.parts[h], float).reshape(-1))
            for h in ("left", "right")}


def camera_x(hemi: str, view: str) -> float:
    """x of the camera direction: the left lateral view looks from -x, the left medial from +x."""
    return -1.0 if (hemi == "left") == (view == "lateral") else 1.0


@dataclass(frozen=True)
class Projection:
    polys: np.ndarray       # (k, 3, 2) visible triangles on screen, far to near
    faces: np.ndarray       # (k, 3) their vertex indices into the hemisphere
    shade: np.ndarray       # (k,) Lambert term in [0, 1]
    grey: np.ndarray        # (k,) anatomy grey level


def project(hemi: Hemisphere, cam: float) -> Projection:
    """Orthographic view along x: screen x = cam * y (anterior toward the viewer's side), y = z."""
    v, f = hemi.coords, hemi.faces
    normals = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    lengths = np.linalg.norm(normals, axis=1)
    facing = normals[:, 0] * cam
    keep = facing > 1e-12 * np.maximum(lengths, 1e-30)
    f = f[keep]
    depth = v[f].mean(axis=1)[:, 0] * cam
    order = np.argsort(depth)
    f = f[order]
    shade = (facing[keep] / np.maximum(lengths[keep], 1e-30))[order]
    screen = np.stack([cam * v[:, 1], v[:, 2]], axis=1)
    sulc = hemi.sulc[f].mean(axis=1)
    grey = np.where(sulc > 0, 0.36, 0.58)
    return Projection(screen[f], f, shade, grey)


def face_rgb(values: np.ndarray, proj: Projection, vmax: float, cmap) -> np.ndarray:
    """(k, 3) colours: the colour map over the anatomy grey, faded in by |value| / vmax."""
    v = values[proj.faces].mean(axis=1)
    rgb = np.asarray(cmap(np.clip(0.5 + 0.5 * v / vmax, 0.0, 1.0)))[:, :3]
    a = np.clip((np.abs(v) / vmax - FADE[0]) / (FADE[1] - FADE[0]), 0.0, 1.0)[:, None]
    lit = (0.5 + 0.5 * proj.shade)[:, None]
    return np.clip(lit * ((1 - a) * proj.grey[:, None] + a * rgb), 0.0, 1.0)


class Renderer:
    """One matplotlib figure reused for every frame of a clip."""

    def __init__(self, mesh: dict[str, Hemisphere], vmax: float, size=SIZE_PX) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.collections import PolyCollection

        self.mesh, self.vmax = mesh, vmax
        self.cmap = plt.get_cmap(CMAP)
        self.splits = np.cumsum([0, len(mesh["left"].coords)])
        self.fig = plt.figure(figsize=(size[0] / DPI, size[1] / DPI), dpi=DPI,
                              facecolor=BACKGROUND)
        self.cells = []
        for k, (hemi, view) in enumerate(VIEWS):
            ax = self.fig.add_axes([(k % 2) * 0.5, 0.52 - (k // 2) * 0.45, 0.5, 0.45])
            ax.set_axis_off()
            proj = project(mesh[hemi], camera_x(hemi, view))
            coll = PolyCollection(proj.polys, edgecolors="none", antialiaseds=False)
            ax.add_collection(coll)
            pts = proj.polys.reshape(-1, 2)
            ax.set_xlim(pts[:, 0].min() - 2, pts[:, 0].max() + 2)
            ax.set_ylim(pts[:, 1].min() - 2, pts[:, 1].max() + 2)
            ax.set_aspect("equal")
            self.cells.append((hemi, proj, coll))
        self.time = self.fig.text(0.02, 0.965, "", color="#d8d8de", fontsize=9, va="top")
        self.fig.text(0.98, 0.965, "left · right", color="#8a8a94", fontsize=8, va="top",
                      ha="right")
        self.fig.text(0.5, 0.025, FOOTER, color="#8a8a94", fontsize=8, ha="center")

    def draw(self, row: np.ndarray, t: float, path: Path) -> None:
        parts = {"left": row[: self.splits[1]], "right": row[self.splits[1]:]}
        for hemi, proj, coll in self.cells:
            coll.set_facecolor(face_rgb(parts[hemi], proj, self.vmax, self.cmap))
        self.time.set_text(f"t = {t:.0f} s")
        self.fig.savefig(path, dpi=DPI, facecolor=BACKGROUND)

    def close(self) -> None:
        import matplotlib.pyplot as plt
        plt.close(self.fig)


def clip_vmax(preds: np.ndarray) -> float:
    """Symmetric colour limit for a clip: the VMAX_PERCENTILE of |prediction|, never 0."""
    vmax = float(np.percentile(np.abs(preds), VMAX_PERCENTILE))
    return vmax if vmax > 0 else 1.0


def render_clip(preds_dir: Path, clip_dir: Path, *, clip_id: str, video: Path | None = None,
                mesh: dict[str, Hemisphere] | None = None, size=SIZE_PX) -> dict:
    """Write clip_dir/frames/*.png and clip_dir/manifest.json from predict's output.

    With video, copies it to clip_dir/clip<suffix> after checking it is the clip predicted.
    Returns the manifest.
    """
    preds = np.load(preds_dir / "preds.npy").astype(np.float32)
    meta = json.loads((preds_dir / "meta.json").read_text())
    mesh = mesh or load_fsaverage5()
    n_vertices = sum(len(h.coords) for h in mesh.values())
    if preds.ndim != 2 or preds.shape[1] != n_vertices:
        raise ValueError(f"{preds_dir}/preds.npy has shape {preds.shape}, expected "
                         f"(rows, {n_vertices})")
    times = meta["frame_times_s"]
    if len(times) != len(preds):
        raise ValueError(f"{len(preds)} prediction rows but {len(times)} frame times in meta.json")
    if video is not None and sha256(video) != meta["clip_sha256"]:
        raise ValueError(f"{video} is not the clip these predictions came from "
                         f"({meta['clip']}, sha256 {meta['clip_sha256'][:12]}...)")

    frames_dir = clip_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.png"):
        old.unlink()
    vmax = clip_vmax(preds)
    renderer = Renderer(mesh, vmax, size)
    frames = []
    try:
        for k, (row, t) in enumerate(zip(preds, times)):
            name = f"frames/{k:04d}.png"
            renderer.draw(row, t, clip_dir / name)
            frames.append(name)
    finally:
        renderer.close()

    found = {
        "format": manifest.FORMAT,
        "clip_id": clip_id,
        "duration_s": meta["duration_s"],
        "frame_times_s": times,
        "frames": frames,
        "vmax": round(vmax, 6),
        "label": manifest.LABEL,
        "credit": manifest.CREDIT,
        "model": {**meta["model"], "device": meta["device"], "dtype": meta["dtype"]},
    }
    if video is not None:
        name = "clip" + video.suffix.lower()
        shutil.copyfile(video, clip_dir / name)
        found["video"] = name
    problems = manifest.problems(found, clip_dir)
    if problems:
        raise ValueError("rendered manifest is invalid: " + "; ".join(problems))
    (clip_dir / manifest.MANIFEST).write_text(json.dumps(found, indent=1) + "\n")
    return found
