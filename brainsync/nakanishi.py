"""Nakanishi et al. (2015) 12-target JFPM SSVEP benchmark: loader and accuracy table.

Each sN.mat holds ``eeg`` shaped (12 targets, 8 channels, 1114 samples, 15 blocks) at
256 Hz; the stimulus starts 38 samples into every trial. The 12 targets form a 3 x 4
matrix: rows step 2 Hz from 9.25 Hz, columns step 0.5 Hz and pi/2 in phase, and the file
stores them column by column (9.25, 11.25, 13.25, 9.75, ...). ``load`` returns them in
ascending frequency order.

    python -m brainsync.nakanishi --windows 1 2 4
    python -m brainsync.nakanishi --windows 0.5 1 --methods cca etrca mscca   # trained, held out
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import paths
from .classify import METHODS, is_trained, make
from .metrics import accuracy
from .signal import Band, preprocess

FS = 256.0
N_SUBJECTS = 10
N_TARGETS = 12
ONSET = 38          # pre-stimulus samples at the start of every trial
LATENCY = 0.135     # visual pathway delay, seconds
CH_NAMES = ("PO7", "PO3", "POz", "PO4", "PO8", "O1", "Oz", "O2")
BAND: Band = (6.0, 90.0)    # applied before scoring; also strips the large DC offset

_k = np.arange(N_TARGETS)
FREQS = 9.25 + 0.5 * _k                  # ascending; target k sits at row k // 4, column k % 4
PHASES = (_k % 4) * (np.pi / 2.0)
FILE_INDEX = 3 * (_k % 4) + _k // 4      # position of sorted target k inside the file
del _k


@dataclass
class Dataset:
    """One subject's trials, grouped by target in ascending frequency order."""

    X: np.ndarray            # (n_trials, n_ch, n_samples) raw µV, whole trial
    y: np.ndarray            # (n_trials,) index into freqs
    blocks: np.ndarray       # (n_trials,) recording block 0..n_blocks-1
    freqs: np.ndarray        # (12,) Hz, ascending
    phases: np.ndarray       # (12,) radians
    fs: float = FS
    onset: int = ONSET
    latency: float = LATENCY
    ch_names: list[str] = field(default_factory=lambda: list(CH_NAMES))
    subject: int = 0
    path: str = ""

    @property
    def n_targets(self) -> int:
        return int(self.freqs.size)

    @property
    def n_blocks(self) -> int:
        return int(np.unique(self.blocks).size)

    @property
    def start(self) -> int:
        """First analysis sample: stimulus onset plus the visual latency."""
        return int(self.onset) + int(round(self.latency * self.fs))


def subject_path(subject: int, directory: str | Path | None = None) -> Path:
    if not 1 <= int(subject) <= N_SUBJECTS:
        raise ValueError(f"subject must be 1..{N_SUBJECTS}, got {subject}")
    base = paths.nakanishi_dir() if directory is None else Path(directory)
    return base / f"s{int(subject)}.mat"


def load(subject: int, directory: str | Path | None = None) -> Dataset:
    """Load subject 1..10; trials come target by target (ascending Hz), blocks in order."""
    from scipy.io import loadmat

    path = subject_path(subject, directory)
    if not path.is_file():
        raise FileNotFoundError(f"Nakanishi 2015 file not found: {path}")
    eeg = np.asarray(loadmat(path, variable_names=["eeg"])["eeg"], dtype=float)
    if eeg.ndim != 4 or eeg.shape[0] != N_TARGETS:
        raise ValueError(f"expected eeg shaped (12, ch, samples, blocks) in {path}, got {eeg.shape}")
    n_blocks, n_ch = eeg.shape[3], eeg.shape[1]
    # eeg[j] is (ch, samples, blocks); move blocks first and stack the targets
    X = np.concatenate([np.moveaxis(eeg[j], -1, 0) for j in FILE_INDEX])
    names = list(CH_NAMES) if n_ch == len(CH_NAMES) else [f"ch{i}" for i in range(n_ch)]
    return Dataset(X=X, y=np.repeat(np.arange(N_TARGETS), n_blocks),
                   blocks=np.tile(np.arange(n_blocks), N_TARGETS),
                   freqs=FREQS.copy(), phases=PHASES.copy(), ch_names=names,
                   subject=int(subject), path=str(path))


def epochs(ds: Dataset, window_s: float, band: Band | None = BAND,
           notch_hz: float = 0.0) -> np.ndarray:
    """Windows (n_trials, n_ch, round(window_s * fs)) starting at ``ds.start``.

    Whole trials are preprocessed before cutting (``band`` None only demeans), so the
    filter's edge effects stay outside the window.
    """
    n = int(round(float(window_s) * ds.fs))
    if n < 2:
        raise ValueError(f"window must span at least 2 samples, got {window_s} s")
    stop = ds.start + n
    if stop > ds.X.shape[-1]:
        room = (ds.X.shape[-1] - ds.start) / ds.fs
        raise ValueError(f"window {window_s} s does not fit in the trial (at most {room:.3f} s)")
    return preprocess(ds.X, ds.fs, band, notch_hz)[..., ds.start:stop]


def subject_accuracy(ds: Dataset, method: str, window_s: float, harmonics: int = 3) -> float:
    """Accuracy of a classifier (default settings) over all of a subject's trials.

    Training-free methods score every trial directly. Trained methods (trca, etrca, their
    -R variants, mscca) are scored leave-one-block-out: fitted on the other blocks'
    trials, scored on the held-out block, so every trial is predicted by a filter that
    never saw it (the protocol of Nakanishi 2018 and Wong 2020).
    """
    clf = make(method, freqs=ds.freqs, fs=ds.fs, harmonics=harmonics, phases=ds.phases)
    X = epochs(ds, window_s)
    if not is_trained(method):
        return accuracy(ds.y, clf.predict(X))
    pred = np.empty(ds.y.size, dtype=int)
    for b in np.unique(ds.blocks):
        test = ds.blocks == b
        try:
            clf.fit(X[~test], ds.y[~test])
        except ValueError as exc:
            raise ValueError(f"{method} with block {b} held out of {ds.n_blocks}: {exc}") from None
        pred[test] = clf.predict(X[test])
    return accuracy(ds.y, pred)


def accuracy_table(subjects: Sequence[int], windows: Sequence[float],
                   methods: Sequence[str] = ("cca", "fbcca"), harmonics: int = 3,
                   directory: str | Path | None = None, log=None) -> dict[str, np.ndarray]:
    """Accuracy per method as arrays (n_subjects, n_windows)."""
    table = {m: np.zeros((len(subjects), len(windows))) for m in methods}
    for row, subject in enumerate(subjects):
        ds = load(subject, directory)
        for m in methods:
            table[m][row] = [subject_accuracy(ds, m, w, harmonics) for w in windows]
        if log is not None:
            parts = [f"{m}=" + "/".join(f"{a:.0%}" for a in table[m][row]) for m in methods]
            print(f"subject {subject}: " + "  ".join(parts), file=log)
    return table


def format_table(table: dict[str, np.ndarray], subjects: Sequence[int],
                 windows: Sequence[float]) -> str:
    title = f"Nakanishi 2015, 12 targets, mean accuracy over {len(subjects)} subjects"
    rows = [title, "method  " + "".join(f"{w:>8g}s" for w in windows)]
    for m, acc in table.items():
        rows.append(f"{m:<8}" + "".join(f"{100 * a:>8.1f}%" for a in acc.mean(axis=0)))
    return "\n".join(rows)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m brainsync.nakanishi",
                                 description="CCA / FBCCA accuracy on the Nakanishi 2015 dataset.")
    ap.add_argument("--windows", type=float, nargs="+", default=[1.0, 2.0, 4.0],
                    help="window lengths in seconds (default 1 2 4)")
    ap.add_argument("--subjects", type=int, nargs="+", default=list(range(1, N_SUBJECTS + 1)))
    ap.add_argument("--methods", nargs="+", default=["cca", "fbcca"], choices=list(METHODS),
                    help="classifiers (default cca fbcca); trained ones are leave-one-block-out")
    ap.add_argument("--harmonics", type=int, default=3)
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="folder with s1.mat..s10.mat (default $BRAINSYNC_DATA_DIR/nakanishi2015)")
    args = ap.parse_args(argv)
    try:
        table = accuracy_table(args.subjects, args.windows, args.methods, args.harmonics,
                               args.data_dir, log=sys.stderr)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(format_table(table, args.subjects, args.windows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
