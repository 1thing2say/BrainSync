"""Read, write and epoch the .npz recordings made with the Cerelog X8 board.

Two kinds of file exist. ``*_trials.npz`` hold one flicker target (key ``f0``);
``*_cued.npz`` hold several (keys ``freqs`` and ``phases``, plus ``f0`` = freqs[0]).
Both store raw µV ``X`` (n_ch, n), unix seconds ``t``, ``fs``, ``ch_names``, the
stimulus log text ``markers``, and the analysis defaults ``window_s``, ``latency``,
``band`` and ``notch``.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .paths import recordings_dir
from .signal import preprocess

MONTAGE = ("Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2")
POSTERIOR = ("P7", "P8", "O1", "O2")
# The C3/C4 pins never reach the scalp on the dry headset; with their leads moved to gel
# pads at the left and right outer canthus (LO, RO) they record the horizontal EOG, and
# Fp1/Fp2 against the mastoid reference give the vertical one (brainsync.eog).
EOG = ("LO", "RO")
EOG_MONTAGE = ("Fp1", "Fp2", "LO", "RO", "P7", "P8", "O1", "O2")
KINDS = ("trials", "cued")

Marker = tuple[float, str, int]
Event = tuple[float, str, int]

_MARKER_RE = re.compile(r"^\s*\[marker\s+([-+0-9.eE]+)\]\s+(stim_on|stim_off):(\d+)\s*$")
_EVENT_RE = re.compile(r"^\s*\[marker\s+([-+0-9.eE]+)\]\s+([A-Za-z_]+):(-?\d+)\s*$")


def parse_events(text: str, kinds: Sequence[str] | None = None) -> list[Event]:
    """Return (unix_time, kind, index) for every ``[marker t] kind:index`` line, in file
    order: stim_on / stim_off, cue, cond, rate, and the coded stimuli's cycle and flash
    markers. ``kinds`` keeps only those event names."""
    out: list[Event] = []
    wanted = None if kinds is None else set(kinds)
    for line in text.splitlines():
        m = _EVENT_RE.match(line)
        if m and (wanted is None or m.group(2) in wanted):
            out.append((float(m.group(1)), m.group(2), int(m.group(3))))
    return out


def parse_markers(text: str) -> list[Marker]:
    """Return (unix_time, kind, target) for every stim_on/stim_off line, in file order.

    Other lines (session/block/cue markers, PsychoPy warnings) are ignored.
    """
    out: list[Marker] = []
    for line in text.splitlines():
        m = _MARKER_RE.match(line)
        if m:
            out.append((float(m.group(1)), m.group(2), int(m.group(3))))
    return out


def channel_indices(ch_names: Sequence[str], channels: Sequence[str | int] | None) -> list[int]:
    """Map channel names (or pass through indices) to row indices; None means all."""
    names = list(ch_names)
    if channels is None:
        return list(range(len(names)))
    idx: list[int] = []
    for c in channels:
        if isinstance(c, (int, np.integer)):
            if not 0 <= int(c) < len(names):
                raise ValueError(f"channel index {c} out of range for {len(names)} channels")
            idx.append(int(c))
        else:
            if c not in names:
                raise KeyError(f"channel {c!r} not in {names}")
            idx.append(names.index(c))
    return idx


@dataclass
class Recording:
    """One recording session as stored on disk."""

    path: Path | None
    X: np.ndarray
    t: np.ndarray
    fs: float
    ch_names: tuple[str, ...]
    freqs: np.ndarray
    phases: np.ndarray
    markers: list[Marker]
    window_s: float
    latency: float
    kind: str
    band: tuple[float, float] = (1.0, 100.0)
    notch: float = 60.0
    marker_text: str = field(default="", repr=False)

    @property
    def n_targets(self) -> int:
        return int(len(self.freqs))

    def channels(self, names: Sequence[str | int] | None = None) -> list[int]:
        return channel_indices(self.ch_names, names)


def _scalar(d: np.lib.npyio.NpzFile, key: str, path: Path) -> float:
    if key not in d.files:
        raise KeyError(f"{path}: missing key {key!r} (has {d.files})")
    return float(np.asarray(d[key]).reshape(-1)[0])


def load_recording(path: str | Path) -> Recording:
    """Load a ``*_trials.npz`` or ``*_cued.npz`` file."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"recording not found: {path}")
    with np.load(path, allow_pickle=False) as d:
        for key in ("X", "t", "fs", "ch_names", "markers"):
            if key not in d.files:
                raise KeyError(f"{path}: missing key {key!r} (has {d.files})")
        X = np.asarray(d["X"], dtype=float)
        t = np.asarray(d["t"], dtype=float)
        fs = _scalar(d, "fs", path)
        ch_names = tuple(str(c) for c in d["ch_names"])
        text = str(d["markers"])
        if "freqs" in d.files:
            kind = "cued"
            freqs = np.asarray(d["freqs"], dtype=float).reshape(-1)
            phases = (np.asarray(d["phases"], dtype=float).reshape(-1)
                      if "phases" in d.files else np.zeros_like(freqs))
        elif "f0" in d.files:
            kind = "trials"
            freqs = np.array([_scalar(d, "f0", path)])
            phases = np.zeros(1)
        else:
            raise KeyError(f"{path}: neither 'freqs' nor 'f0' present (has {d.files})")
        window_s = _scalar(d, "window_s", path) if "window_s" in d.files else 3.86
        latency = _scalar(d, "latency", path) if "latency" in d.files else 0.14
        band = (tuple(float(v) for v in np.asarray(d["band"]).reshape(-1)[:2])
                if "band" in d.files else (1.0, 100.0))
        notch_hz = _scalar(d, "notch", path) if "notch" in d.files else 60.0
    if X.ndim != 2 or X.shape[1] != t.shape[0] or X.shape[0] != len(ch_names):
        raise ValueError(f"{path}: inconsistent shapes X{X.shape} t{t.shape} "
                         f"ch_names={len(ch_names)}")
    if phases.shape != freqs.shape:
        raise ValueError(f"{path}: phases {phases.shape} do not match freqs {freqs.shape}")
    markers = parse_markers(text)
    if kind == "trials":
        markers = [(s, k, 0) for s, k, _ in markers]
    return Recording(path=path, X=X, t=t, fs=fs, ch_names=ch_names, freqs=freqs,
                     phases=phases, markers=markers, window_s=window_s, latency=latency,
                     kind=kind, band=band, notch=notch_hz, marker_text=text)


def list_recordings(kind: str | None = None, directory: str | Path | None = None) -> list[Path]:
    """Sorted recording paths in ``directory`` (default recordings_dir()), optionally of one kind."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS} or None, got {kind!r}")
    root = Path(directory) if directory is not None else recordings_dir()
    if not root.is_dir():
        return []
    kinds = KINDS if kind is None else (kind,)
    return sorted(p for k in kinds for p in root.glob(f"*_{k}.npz"))


def save_recording(path: str | Path, X: np.ndarray, t: np.ndarray, fs: float,
                   ch_names: Sequence[str], freqs: Sequence[float], markers: str,
                   window_s: float = 3.86, latency: float = 0.14,
                   phases: Sequence[float] | None = None,
                   band: tuple[float, float] = (1.0, 100.0), notch: float = 60.0,
                   kind: str | None = None) -> Path:
    """Write a recording with the same keys as the existing files.

    ``kind`` defaults to "cued" for several targets and "trials" for one; a "trials"
    file stores only ``f0``. ``markers`` is the raw stimulus log text.
    """
    path = Path(path)
    if path.suffix != ".npz":
        raise ValueError(f"recording path must end in .npz, got {path}")
    X = np.asarray(X, dtype=float)
    t = np.asarray(t, dtype=float)
    names = np.array([str(c) for c in ch_names])
    freqs_a = np.asarray(freqs, dtype=float).reshape(-1)
    if freqs_a.size == 0:
        raise ValueError("need at least one target frequency, got []")
    phases_a = (np.zeros_like(freqs_a) if phases is None
                else np.asarray(phases, dtype=float).reshape(-1))
    if phases_a.shape != freqs_a.shape:
        raise ValueError(f"phases {phases_a.tolist()} do not match freqs {freqs_a.tolist()}")
    if X.ndim != 2 or X.shape[1] != t.shape[0] or X.shape[0] != names.size:
        raise ValueError(f"inconsistent shapes X{X.shape} t{t.shape} ch_names={names.size}")
    if kind is None:
        kind = "cued" if freqs_a.size > 1 else "trials"
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if kind == "trials" and freqs_a.size != 1:
        raise ValueError(f"a 'trials' file holds one target, got freqs {freqs_a.tolist()}")
    payload: dict[str, np.ndarray] = {
        "X": X, "t": t, "fs": np.float64(fs), "ch_names": names,
    }
    if kind == "cued":
        payload["freqs"] = freqs_a
        payload["phases"] = phases_a
    payload["f0"] = np.float64(freqs_a[0])
    payload.update({
        "markers": np.array(str(markers)),
        "window_s": np.float64(window_s),
        "latency": np.float64(latency),
        "band": np.asarray(band, dtype=float),
        "notch": np.float64(notch),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, **payload)
    return path


@dataclass
class Windows:
    """Preprocessed flicker (``on``) and still-screen (``off``) windows, one pair per trial."""

    on: np.ndarray
    off: np.ndarray
    labels: np.ndarray
    on_start: np.ndarray
    off_start: np.ndarray
    fs: float = 250.0
    ch_names: tuple[str, ...] = MONTAGE

    def __len__(self) -> int:
        return int(self.labels.shape[0])

    def subset(self, idx: Sequence[int] | np.ndarray) -> Windows:
        idx = np.asarray(idx, dtype=int)
        return Windows(self.on[idx], self.off[idx], self.labels[idx], self.on_start[idx],
                       self.off_start[idx], self.fs, self.ch_names)

    def pick(self, channels: Sequence[str | int] | None) -> Windows:
        ch = channel_indices(self.ch_names, channels)
        return Windows(self.on[:, ch], self.off[:, ch], self.labels, self.on_start,
                       self.off_start, self.fs, tuple(self.ch_names[i] for i in ch))


def pair_trials(markers: Sequence[Marker]) -> list[tuple[float, float, int]]:
    """Match each stim_on with the next stim_off of the same target; orphans are dropped."""
    trials: list[tuple[float, float, int]] = []
    pending: tuple[float, int] | None = None
    for stamp, kind, target in sorted(markers, key=lambda m: m[0]):
        if kind == "stim_on":
            pending = (stamp, target)
        elif kind == "stim_off":
            if pending is not None and pending[1] == target:
                trials.append((pending[0], stamp, target))
            pending = None
    return trials


def cut_windows(rec: Recording, window_s: float | None = None, latency: float | None = None,
                band: tuple[float, float] | None = (1.0, 100.0),
                notch_hz: float = 60.0) -> Windows:
    """Preprocess the whole recording, then cut one flicker and one still window per trial.

    The flicker window starts at the first sample at or after stim_on + latency, the still
    window at stim_off + latency; both last round(window_s * fs) samples. Trials missing a
    marker or running past either end of the data are dropped.
    """
    window_s = rec.window_s if window_s is None else float(window_s)
    latency = rec.latency if latency is None else float(latency)
    if window_s <= 0:
        raise ValueError(f"window_s must be positive, got {window_s}")
    n_win = int(round(window_s * rec.fs))
    n_total = rec.X.shape[1]
    tol = 0.01 / rec.fs  # absorbs float error in printed marker stamps
    Y =preprocess(rec.X, rec.fs, band=band, notch_hz=notch_hz)
    ons, offs, labels = [], [], []
    for t_on, t_off, target in pair_trials(rec.markers):
        if t_on + latency < rec.t[0] - tol:
            continue
        s_on = int(np.searchsorted(rec.t, t_on + latency - tol))
        s_off = int(np.searchsorted(rec.t, t_off + latency - tol))
        if s_on + n_win > n_total or s_off + n_win > n_total:
            continue
        if not 0 <= target < rec.n_targets:
            raise ValueError(f"{rec.path}: marker target {target} but only "
                             f"{rec.n_targets} frequencies {rec.freqs.tolist()}")
        ons.append(s_on)
        offs.append(s_off)
        labels.append(target)
    on_start = np.asarray(ons, dtype=int)
    off_start = np.asarray(offs, dtype=int)
    n_ch = rec.X.shape[0]
    if on_start.size:
        span = np.arange(n_win)
        on = Y[:, on_start[:, None] + span].transpose(1, 0, 2)
        off = Y[:, off_start[:, None] + span].transpose(1, 0, 2)
    else:
        on = off = np.empty((0, n_ch, n_win))
    return Windows(on=on, off=off, labels=np.asarray(labels, dtype=int), on_start=on_start,
                   off_start=off_start, fs=rec.fs, ch_names=tuple(rec.ch_names))


def reject_artifacts(on: np.ndarray, off: np.ndarray,
                     channels: Sequence[str | int] | None = None, factor: float = 3.0,
                     ch_names: Sequence[str] = MONTAGE) -> np.ndarray:
    """Indices of trials to keep.

    A trial is rejected when the RMS of its flicker or its still window (over ``channels``,
    names resolved against ``ch_names``) exceeds ``factor`` times the median RMS of that
    window type across trials. ``factor`` 0 keeps everything.
    """
    on = np.asarray(on, dtype=float)
    off = np.asarray(off, dtype=float)
    if on.ndim != 3 or off.ndim != 3 or on.shape[:2] != off.shape[:2]:
        raise ValueError(f"on/off must be (n_trials, n_ch, n) with matching trials and "
                         f"channels, got {on.shape} and {off.shape}")
    if factor < 0:
        raise ValueError(f"factor must be >= 0, got {factor}")
    n = on.shape[0]
    if factor == 0 or n == 0:
        return np.arange(n)
    names: Sequence[str] = ch_names
    if len(names) != on.shape[1]:
        if channels is not None and any(isinstance(c, str) for c in channels):
            raise ValueError(f"ch_names has {len(names)} entries but windows have "
                             f"{on.shape[1]} channels")
        names = [str(i) for i in range(on.shape[1])]
    ch = channel_indices(names, channels)
    bad = np.zeros(n, dtype=bool)
    for w in (on, off):
        rms = np.sqrt(np.mean(w[:, ch] ** 2, axis=(1, 2)))
        bad |= rms > factor * np.median(rms)
    return np.flatnonzero(~bad)
