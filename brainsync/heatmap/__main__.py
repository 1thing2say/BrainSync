"""Predicted-brain heatmaps for the Reels demo panel (Meta TRIBE v2, offline).

    .venv-tribe/bin/python -m brainsync.heatmap predict clip.mp4 --clip-id beach
    .venv-tribe/bin/python -m brainsync.heatmap render beach --video clip.mp4
    .venv/bin/python -m brainsync.heatmap check

predict and render need the TRIBE venv (see README); check and index only need the standard
library. Predictions go to $BRAINSYNC_DATA_DIR/heatmaps/<id>/, rendered clips to
extension/heatmap/clips/<id>/ with an index.json the panel loads. Only use clips you own.

Exit status: 0 done, 1 failed, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import paths
from . import manifest

CLIPS_DIR = paths.REPO_ROOT / "extension" / "heatmap" / "clips"
CACHE_DIR = paths.REPO_ROOT / ".tribe-cache"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.heatmap", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("predict", help="run TRIBE v2 on one clip (video + audio)")
    pr.add_argument("clip", type=Path, help="video file (.mp4, .mov, .webm, ...) you own")
    pr.add_argument("--clip-id", required=True, help="name for the clip: a-z, 0-9, _ and -")
    pr.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"],
                    help="auto: cuda, then mps, then cpu (default)")
    pr.add_argument("--fp32", action="store_true",
                    help="keep the video model in fp32 on a GPU (default fp16 there)")
    pr.add_argument("--out", type=Path, default=None,
                    help="prediction directory (default $BRAINSYNC_DATA_DIR/heatmaps/<id>)")
    pr.add_argument("--cache", type=Path, default=CACHE_DIR,
                    help=f"TRIBE feature cache and scratch (default {CACHE_DIR})")

    rd = sub.add_parser("render", help="draw a clip's predictions as panel frames")
    rd.add_argument("clip_id")
    rd.add_argument("--preds", type=Path, default=None,
                    help="prediction directory (default $BRAINSYNC_DATA_DIR/heatmaps/<id>)")
    rd.add_argument("--clips", type=Path, default=CLIPS_DIR, help=f"default {CLIPS_DIR}")
    rd.add_argument("--video", type=Path, default=None,
                    help="copy this clip next to the frames for the demo page")

    for name, text in (("check", "validate every rendered clip"),
                       ("index", "rewrite index.json from the rendered clips")):
        c = sub.add_parser(name, help=text)
        c.add_argument("--clips", type=Path, default=CLIPS_DIR, help=f"default {CLIPS_DIR}")
    return p


def _predict(args: argparse.Namespace) -> int:
    # before torch and huggingface_hub load: models in the deletable cache, and on MPS fail
    # with an out-of-memory error instead of swapping the Mac to a halt
    os.environ.setdefault("HF_HOME", str(args.cache / "hf"))
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.8")
    os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.6")
    from .predict import predict_clip

    out = args.out or paths.heatmaps_dir() / args.clip_id
    reason = paths.unwritable_reason(out)
    if reason:
        print(f"error: cannot write to {out}: {reason}", file=sys.stderr)
        return 1
    meta = predict_clip(args.clip, out, device=args.device, fp16=False if args.fp32 else None,
                        cache_dir=args.cache,
                        progress=lambda msg: print(msg, file=sys.stderr, flush=True))
    print(f"wrote {len(meta['frame_times_s'])} rows to {out} in {meta['predict_s']:g} s "
          f"({meta['device']} {meta['dtype']})", file=sys.stderr)
    return 0


def _render(args: argparse.Namespace) -> int:
    from .render import render_clip

    preds = args.preds or paths.heatmaps_dir() / args.clip_id
    clip_dir = args.clips / args.clip_id
    found = render_clip(preds, clip_dir, clip_id=args.clip_id, video=args.video)
    ids = manifest.write_index(args.clips)
    print(f"wrote {len(found['frames'])} frames to {clip_dir}; index lists {', '.join(ids)}",
          file=sys.stderr)
    return 0


def _check(args: argparse.Namespace) -> int:
    if not args.clips.is_dir():
        print(f"error: no clips directory at {args.clips}", file=sys.stderr)
        return 1
    bad = 0
    dirs = sorted(p for p in args.clips.iterdir() if (p / manifest.MANIFEST).is_file())
    for d in dirs:
        try:
            m = manifest.load(d)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            bad += 1
            continue
        print(f"{m['clip_id']}: {len(m['frames'])} frames over {m['duration_s']:g} s")
    if not dirs:
        print(f"error: no rendered clips in {args.clips}", file=sys.stderr)
        return 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in ("predict", "render"):
        reason = manifest.clip_id_problem(args.clip_id)
        if reason:
            print(f"error: {reason}", file=sys.stderr)
            return 2
    try:
        if args.cmd == "predict":
            return _predict(args)
        if args.cmd == "render":
            return _render(args)
        if args.cmd == "check":
            return _check(args)
        ids = manifest.write_index(args.clips)
        print(f"index lists {len(ids)} clips: {', '.join(ids) or '(none)'}", file=sys.stderr)
        return 0
    except ImportError as exc:
        print(f"error: {exc}; run predict and render from the TRIBE venv (see README)",
              file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
