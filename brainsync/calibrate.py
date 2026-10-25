"""Record a cued calibration session, score every classifier on it, save the trained model.

    python -m brainsync.calibrate -c four_diamond                 # headset + display, 32 trials
    python -m brainsync.calibrate --from <file>_cued.npz          # an existing recording
    python -m brainsync.calibrate -c four_diamond --fake          # synthetic, no board or display

One session serves two purposes (PLAN.md section 14): it is the 4-target gate (does this
headset separate the four flickers?) and the calibration data the trained classifiers in
``brainsync.templates`` need. The tool records with ``brainsync.record`` (or takes a file),
then prints ``score_cued``'s table: FBCCA scored on every trial, the trained methods scored
leave-one-block-out, at the requested window lengths. The gate line reads the best accuracy at
``--gate-window`` against the thresholds of PLAN.md section 14 (keep page mode at 70 % or
more; 55-70 % means train and re-check; below that fall back to list mode). Finally the chosen
classifier is fitted on all trials, its rest baseline on the still windows, and saved as
``cache/templates/<recording>_<classifier>.npz`` for ``brainsync.demo --model`` and
``brainsync.server --model``. The saved model keeps the window length it was scored at only
through its templates; the decoder's window and abstain rules stay its own.

Exit status: 0 when scored and saved, 1 when recording or scoring failed, 2 for bad options.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from .classify import NAMES, TRAINED, make
from .paths import REPO_ROOT
from .recordings import POSTERIOR, Recording, load_recording
from .score_cued import (DEFAULT_MARGIN, Report, format_report, parse_channels,
                         score_recording, trial_windows)
from .signal import default_bands
from .templates import TemplateClassifier, save_model

DEFAULT_METHODS = ("fbcca", "ecca", "etrca", "etrcar", "sfcca")
DEFAULT_WINDOWS = (1.0, 1.5, 2.0)
GATE_WINDOW_S = 1.5
GATE_KEEP = 0.70      # PLAN.md section 14: keep page mode
GATE_TRAIN = 0.55     # train on the recording and re-check; below: list mode
MODELS_DIR = REPO_ROOT / "cache" / "templates"


def gate_line(rep: Report, window_s: float) -> str:
    """The 4-target gate verdict from the best cell at ``window_s`` (nearest scored window)."""
    rows = [r for r in rep.rows]
    nearest = min({r.window_s for r in rows}, key=lambda w: abs(w - window_s))
    cells = [r for r in rows if r.window_s == nearest]
    best = max(cells, key=lambda r: r.accuracy)
    acc = best.accuracy
    if acc >= GATE_KEEP:
        verdict = "keep page mode (4 targets)"
    elif acc >= GATE_TRAIN:
        verdict = "borderline: use the trained model and re-check live before trusting it"
    else:
        verdict = "fall back to list mode (SAY / NEXT on 2 targets)"
    kind = "trained, held out" if best.folds else "training-free"
    return (f"gate at {nearest:g} s: best {best.method} {best.hits}/{best.n} "
            f"({100 * acc:.0f} %, {kind}; chance {100 * rep.chance:.0f} %) -> {verdict}")


def fit_all(rec: Recording, classifier: str, channels: Sequence[str | int], reject: float,
            harmonics: int, bands=None) -> TemplateClassifier:
    """Fit ``classifier`` on every kept trial of the recording, baseline on its still windows."""
    w, kept, _ = trial_windows(rec, channels, reject)
    kw: dict[str, object] = dict(freqs=rec.freqs, fs=rec.fs, harmonics=harmonics,
                                 phases=rec.phases)
    if bands is not None:
        kw["bands"] = list(bands)
    clf = make(classifier, **kw)
    if not isinstance(clf, TemplateClassifier):
        raise ValueError(f"{classifier} is training-free; choose one of {list(TRAINED)} to save")
    clf.fit(w.on, w.labels, rec.latency)
    clf.fit_baseline(w.off)
    return clf


def model_path(rec_path: Path | None, classifier: str, out_dir: Path = MODELS_DIR) -> Path:
    stem = rec_path.stem if rec_path is not None else "recording"
    return out_dir / f"{stem}_{classifier}.npz"


def _record(args: argparse.Namespace) -> Path | None:
    """Run brainsync.record with these options; returns the saved file."""
    from . import record

    save_dir = args.save_dir or (Path(record.fake_dir()) if args.fake else None)
    argv = ["-c", args.config, "--trials", str(args.trials), "--cue-s", str(args.cue_s),
            "--stim-s", str(args.stim_s), "--rest-s", str(args.rest_s)]
    if save_dir is not None:
        argv += ["--save-dir", str(save_dir)]
    if args.fake:
        argv += ["--fake"]
        if args.seed is not None:
            argv += ["--seed", str(args.seed)]
    if args.ignore_refresh:
        argv += ["--ignore-refresh"]
    before = set(Path(save_dir or record.recordings_dir()).glob("*_cued.npz"))
    code = record.main(argv)
    if code != 0:
        return None
    after = set(Path(save_dir or record.recordings_dir()).glob("*_cued.npz")) - before
    return max(after, key=lambda p: p.stat().st_mtime) if after else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.calibrate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("the session")
    src.add_argument("-c", "--config", default="four_diamond",
                     help="stimulus config to record (default four_diamond)")
    src.add_argument("--from", dest="source", type=Path, default=None, metavar="FILE",
                     help="score and fit an existing *_cued.npz instead of recording")
    src.add_argument("--trials", type=int, default=32,
                     help="cued trials, rounded up to whole blocks (default 32: 8 per target)")
    src.add_argument("--cue-s", type=float, default=1.0)
    src.add_argument("--stim-s", type=float, default=4.0)
    src.add_argument("--rest-s", type=float, default=3.0)
    src.add_argument("--save-dir", type=Path, default=None, help="where the recording goes")
    src.add_argument("--fake", action="store_true", help="synthetic board, no display")
    src.add_argument("--seed", type=int, default=None, help="with --fake: noise seed")
    src.add_argument("--ignore-refresh", action="store_true",
                     help="let the display run when its refresh is not 60 Hz")
    sc = p.add_argument_group("scoring")
    sc.add_argument("--methods", nargs="+", default=list(DEFAULT_METHODS), choices=list(NAMES))
    sc.add_argument("--windows", type=float, nargs="+", default=list(DEFAULT_WINDOWS))
    sc.add_argument("--gate-window", type=float, default=GATE_WINDOW_S,
                    help=f"window the gate verdict reads (default {GATE_WINDOW_S:g} s)")
    sc.add_argument("--channels", default=",".join(POSTERIOR))
    sc.add_argument("--reject", type=float, default=3.0)
    sc.add_argument("--harmonics", type=int, default=3)
    sc.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    sc.add_argument("--fb-bands", metavar="N:UPPER",
                    help="filter bank for fbcca and the trained methods (N sub-bands to UPPER Hz)")
    out = p.add_argument_group("the saved model")
    out.add_argument("--classifier", default="etrcar", choices=list(TRAINED),
                     help="trained classifier to fit on all trials and save (default etrcar)")
    out.add_argument("--out", type=Path, default=None,
                     help=f"model file (default {MODELS_DIR}/<recording>_<classifier>.npz)")
    out.add_argument("--no-save", action="store_true", help="score only")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if args.trials < 2:
        p.error(f"--trials must be >= 2, got {args.trials}")
    if any(w <= 0 for w in args.windows) or args.gate_window <= 0:
        p.error("window lengths must be positive")
    channels = parse_channels(args.channels)

    path = args.source
    if path is None:
        path = _record(args)
        if path is None:
            print("error: the recording did not complete; nothing to calibrate", file=sys.stderr)
            return 1
    try:
        rec = load_recording(path)
        bands = None
        if args.fb_bands:
            n, upper = args.fb_bands.split(":")
            bands = default_bands(rec.fs, float(upper), int(n))
        rep = score_recording(rec, args.methods, args.windows, args.margin, channels,
                              args.reject, args.harmonics, bands)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error: {path}: {exc}", file=sys.stderr)
        return 1
    print(format_report(rep))
    print(gate_line(rep, args.gate_window))
    if args.no_save:
        return 0
    try:
        clf = fit_all(rec, args.classifier, channels, args.reject, args.harmonics, bands)
        out = args.out or model_path(rec.path, args.classifier)
        save_model(clf, out, recording=str(rec.path), config=args.config,
                   channels=[str(c) for c in channels],
                   created=dt.datetime.now().isoformat(timespec="seconds"),
                   n_trials=int(clf.n_train),
                   gate=gate_line(rep, args.gate_window))
    except (ValueError, OSError) as exc:
        print(f"error: could not save the model: {exc}", file=sys.stderr)
        return 1
    print(f"saved {clf.name} fitted on {clf.n_train} trials "
          f"({', '.join(f'{f:g}' for f in rec.freqs)} Hz, {clf.n_ch} channels) to {out}")
    print(f"use it with: python -m brainsync.demo -c {args.config} --model {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
