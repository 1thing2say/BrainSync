"""Electrode check: watch one flickering target for a while, then grade every channel.

    python -m brainsync.stimulus -c one_target_15 --free --seconds 60   # terminal 1
    python -m brainsync.check --target 15                                # terminal 2
    python -m brainsync.check --fake --target 10 --no-show               # synthetic, no board

The first --warmup seconds are thrown away while the amplifier settles, then --seconds are
analysed. The table gives, per channel, the raw DC offset in mV, the RMS after the shared
preprocessing, the SNR at the target and at its second harmonic, the strongest frequency
between 5 and 30 Hz, and what looks wrong with the contact, if anything. As a rule of
thumb an SNR over 6 dB is a clear SSVEP, 3 to 6 dB a weak one, and under 3 dB none. If all
channels show the same waveform, the shared reference or bias electrode is the suspect.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal as sps

from .board import ADC_FULL_SCALE_UV, FS, FakeBoard, record
from .paths import unwritable_reason
from .recordings import MONTAGE
from .signal import preprocess

CLIP_LEVEL = 0.999                    # |x| at this share of full scale counts as clipped
FLAT_RMS_UV = 0.25                    # filtered RMS under this: nothing connected
NOISY_RMS_UV = 75.0                   # filtered RMS over this: poor contact
DC_LIMIT_UV = 0.5 * ADC_FULL_SCALE_UV  # offset eating over half the input range
SAME_SIGNAL_R = 0.98                  # median |r| over this: reference problem
PEAK_RANGE = (5.0, 30.0)
SNR_SPAN_HZ = 1.0                     # SNR reference bins lie up to this far from the target
SNR_GUARD_BINS = 1                    # bins next to the target left out of the reference
MIN_SEGMENT_SAMPLES = 8


@dataclass(frozen=True)
class ChannelStats:
    """What the check found on one channel."""

    name: str
    dc_uv: float
    rms_uv: float
    snr_f: float
    snr_2f: float
    peak_hz: float
    flags: tuple[str, ...] = ()

    @property
    def note(self) -> str:
        return "; ".join(self.flags)

    @property
    def usable(self) -> bool:
        return not any(f.startswith(("railed", "flat")) for f in self.flags)


@dataclass
class CheckResult:
    """All channels of one electrode check, plus the spectra behind the numbers."""

    target: float
    fs: float
    channels: list[ChannelStats]
    median_r: float
    freqs: np.ndarray
    power: np.ndarray  # (n_ch, n_freqs) Welch PSD, µV²/Hz

    def __getitem__(self, name: str) -> ChannelStats:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(f"no channel {name!r}; have {[c.name for c in self.channels]}")

    @property
    def reference_suspect(self) -> bool:
        return bool(np.isfinite(self.median_r) and self.median_r > SAME_SIGNAL_R)

    def best(self) -> ChannelStats | None:
        """Usable channel with the highest finite SNR at the target; None if there is none."""
        rated = [c for c in self.channels if c.usable and np.isfinite(c.snr_f)]
        return max(rated, key=lambda c: c.snr_f) if rated else None


def welch_psd(Y: np.ndarray, fs: float, segment_s: float = 4.0) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD along the last axis using Hann segments of ``segment_s`` seconds."""
    nper = min(int(round(segment_s * fs)), Y.shape[-1])
    if nper < MIN_SEGMENT_SAMPLES:
        raise ValueError(f"need at least {MIN_SEGMENT_SAMPLES} samples per segment, got {nper}")
    return sps.welch(Y, fs=fs, nperseg=nper, axis=-1)


def local_snr_db(freqs: np.ndarray, power: np.ndarray, f: float,
                 span_hz: float = SNR_SPAN_HZ, guard: int = SNR_GUARD_BINS) -> np.ndarray:
    """Power at the bin of ``f`` against the mean of the bins up to ``span_hz`` away, in dB.

    The ``guard`` bins on each side are left out of the reference (Hann leakage). Returns
    NaN for frequencies outside the spectrum.
    """
    df = float(freqs[1] - freqs[0])
    k = int(round((f - freqs[0]) / df))
    if f <= 0 or k >= freqs.size:
        return np.full(power.shape[:-1], np.nan)
    m = int(round(span_hz / df))
    offsets = np.arange(-m, m + 1)
    ring = k + offsets[np.abs(offsets) > guard]
    ring = ring[(ring >= 0) & (ring < freqs.size)]
    if ring.size == 0:
        raise ValueError(f"{df:g} Hz bins leave no reference within {span_hz} Hz of {f} Hz; "
                         "use a longer --segment")
    ref = np.maximum(power[..., ring].mean(axis=-1), np.finfo(float).tiny)
    return 10.0 * np.log10(np.maximum(power[..., k], np.finfo(float).tiny) / ref)


def segment_problem(segment_s: float, fs: float, span_hz: float = SNR_SPAN_HZ,
                    guard: int = SNR_GUARD_BINS) -> str:
    """'' when Welch segments of ``segment_s`` s at ``fs`` Hz allow an SNR, else why not."""
    nper = int(round(segment_s * fs)) if np.isfinite(segment_s) and segment_s > 0 else 0
    if nper < MIN_SEGMENT_SAMPLES:
        return (f"--segment {segment_s:g} s gives {nper} samples at {fs:g} Hz; need at least "
                f"{MIN_SEGMENT_SAMPLES}")
    df = fs / nper
    if int(round(span_hz / df)) <= guard:
        shortest = (guard + 0.5) / span_hz
        return (f"--segment {segment_s:g} s gives {df:.3g} Hz bins, too coarse for an SNR "
                f"reference within {span_hz:g} Hz of the target; use at least {shortest:g} s")
    return ""


def strongest_hz(freqs: np.ndarray, power: np.ndarray,
                 band: tuple[float, float] = PEAK_RANGE) -> np.ndarray:
    """Frequency of the biggest bin inside ``band`` for every row of ``power``."""
    inside = (freqs >= band[0]) & (freqs <= band[1])
    if not inside.any():
        raise ValueError(f"the spectrum has no bins inside {band} Hz")
    sub = np.where(inside, power, -np.inf)
    return freqs[np.argmax(sub, axis=-1)]


def diagnose(raw: np.ndarray, rms_uv: float) -> tuple[str, ...]:
    """Problems seen on one channel from its raw µV samples and its filtered RMS."""
    clipped = np.abs(raw) >= CLIP_LEVEL * ADC_FULL_SCALE_UV
    if clipped.mean() > 0.5:
        return (f"railed at the ADS1299 full scale +-{ADC_FULL_SCALE_UV / 1000:g} mV: "
                "electrode off or lead open",)
    found = []
    if rms_uv < FLAT_RMS_UV:
        found.append(f"flat ({rms_uv:.2f} µV RMS): channel disconnected or shorted")
    elif rms_uv > NOISY_RMS_UV:
        found.append(f"noisy ({rms_uv:.0f} µV RMS): press the electrode in, part the hair")
    dc = float(raw.mean())
    if abs(dc) > DC_LIMIT_UV:
        share = 100.0 * abs(dc) / ADC_FULL_SCALE_UV
        found.append(f"large DC {dc / 1000:+.0f} mV ({share:.0f}% of the range): reseat or re-wet")
    return tuple(found)


def similarity(Y: np.ndarray, keep: np.ndarray) -> float:
    """Median absolute Pearson r over all pairs of the kept rows; NaN with under two rows."""
    rows = Y[np.asarray(keep, dtype=bool)]
    k = rows.shape[0]
    if k < 2:
        return float("nan")
    z = rows - rows.mean(axis=1, keepdims=True)
    z /= np.maximum(np.linalg.norm(z, axis=1, keepdims=True), np.finfo(float).tiny)
    r = z @ z.T
    return float(np.median(np.abs(r[~np.eye(k, dtype=bool)])))


def run_check(X: np.ndarray, fs: float, target: float, names: Sequence[str] = MONTAGE,
              band: tuple[float, float] = (1.0, 100.0), notch_hz: float = 60.0,
              segment_s: float = 4.0) -> CheckResult:
    """Grade raw µV data ``X`` (n_ch, n) recorded while watching ``target`` Hz."""
    X = np.asarray(X, dtype=float)
    if X.ndim != 2 or X.shape[0] != len(names):
        raise ValueError(f"X must be ({len(names)}, n) for channels {list(names)}, got {X.shape}")
    if not 0 < target < fs / 2:
        raise ValueError(f"target must lie in (0, {fs / 2:g}) Hz, got {target}")
    Y = preprocess(X, fs, band=band, notch_hz=notch_hz)
    freqs, power = welch_psd(Y, fs, segment_s)
    rms = np.sqrt(np.mean(Y * Y, axis=1))
    snr1 = local_snr_db(freqs, power, target)
    snr2 = local_snr_db(freqs, power, 2 * target)
    peaks = strongest_hz(freqs, power)
    chans = []
    for i, nm in enumerate(names):
        flags = diagnose(X[i], float(rms[i]))
        ch = ChannelStats(str(nm), float(X[i].mean()), float(rms[i]), float(snr1[i]),
                          float(snr2[i]), float(peaks[i]), flags)
        if not ch.usable:  # a railed or flat channel has no spectrum worth quoting
            ch = ChannelStats(ch.name, ch.dc_uv, ch.rms_uv, np.nan, np.nan, np.nan, flags)
        chans.append(ch)
    r = similarity(Y, np.array([c.usable for c in chans]))
    return CheckResult(float(target), float(fs), chans, r, freqs, power)


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Columns padded to their widest cell: first left-aligned, last free, others right."""
    widths = [max(len(r[j]) for r in (header, *rows)) for j in range(len(header) - 1)]
    out = []
    for r in (header, *rows):
        cells = [r[0].ljust(widths[0])] + [c.rjust(w) for c, w in zip(r[1:-1], widths[1:])]
        out.append(("  ".join(cells) + "  " + r[-1]).rstrip())
    return out


def render(res: CheckResult) -> str:
    """The per-channel table and the summary lines."""
    def num(v: float, fmt: str) -> str:
        return format(v, fmt) if np.isfinite(v) else "n/a"

    header = ["channel", "DC mV", "RMS uV", f"SNR {res.target:g} Hz",
              f"SNR {2 * res.target:g} Hz", "peak Hz", "note"]
    rows = [[c.name, f"{round(c.dc_uv / 1000, 1) + 0.0:.1f}", f"{c.rms_uv:.1f}", num(c.snr_f, ".1f"),
             num(c.snr_2f, ".1f"), num(c.peak_hz, ".2f"), c.note] for c in res.channels]
    lines = _table(header, rows)
    top = res.best()
    if top is None:
        lines.append(f"strongest {res.target:g} Hz response: none (no usable channel); fix the "
                     "contacts noted above")
    else:
        lines.append(f"strongest {res.target:g} Hz response: {top.name} at {top.snr_f:.1f} dB "
                     "(over 6 dB: clear SSVEP; 3 to 6 dB: weak; under 3 dB: none)")
    if res.reference_suspect:
        lines.append(f"warning: the usable channels carry nearly the same signal (median |r| "
                     f"{res.median_r:.3f} > {SAME_SIGNAL_R:g}); suspect the reference or bias "
                     "electrode rather than the individual contacts")
    return "\n".join(lines)


def plot(res: CheckResult, fmax: float = 45.0):
    """SNR bars per channel above one PSD panel per channel."""
    import matplotlib.pyplot as plt

    n = len(res.channels)
    cols = 2
    prow = -(-n // cols)
    fig = plt.figure(figsize=(10, 2.0 + 1.9 * prow), layout="constrained")
    grid = fig.add_gridspec(prow + 1, cols, height_ratios=[1.3] + [1.0] * prow)
    bars = fig.add_subplot(grid[0, :])
    x = np.arange(n)
    bars.bar(x - 0.2, [c.snr_f for c in res.channels], 0.4, label=f"{res.target:g} Hz")
    bars.bar(x + 0.2, [c.snr_2f for c in res.channels], 0.4, label=f"{2 * res.target:g} Hz")
    for level in (3.0, 6.0):
        bars.axhline(level, color="0.5", lw=0.8, ls=":")
    bars.set_xticks(x, [c.name for c in res.channels])
    bars.set_ylabel("SNR (dB)")
    bars.legend(loc="upper left", frameon=False)
    sel = (res.freqs >= 1.0) & (res.freqs <= fmax)
    first = None
    for i, ch in enumerate(res.channels):
        ax = fig.add_subplot(grid[1 + i // cols, i % cols], sharex=first)
        first = first or ax
        ax.semilogy(res.freqs[sel], np.maximum(res.power[i, sel], 1e-6), lw=0.8)
        for f in (res.target, 2 * res.target):
            if f <= fmax:
                ax.axvspan(f - 0.5, f + 0.5, color="tab:orange", alpha=0.25, lw=0)
        snr = f"{ch.snr_f:.1f} dB" if np.isfinite(ch.snr_f) else "n/a"
        ax.set_title(f"{ch.name}  {snr}" + (f"  [{ch.flags[0].split(':')[0]}]"
                                            if ch.flags else ""),
                     fontsize=9, color="tab:red" if ch.flags else "black", loc="left")
        ax.tick_params(labelsize=7)
    fig.supxlabel("frequency (Hz)", fontsize=9)
    fig.supylabel("PSD (µV²/Hz)", fontsize=9)
    return fig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.check", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-t", "--target", type=float, default=15.0,
                   help="frequency of the flicker you watch, Hz (default 15)")
    p.add_argument("-s", "--seconds", type=float, default=30.0,
                   help="seconds analysed (default 30)")
    p.add_argument("--warmup", type=float, default=5.0,
                   help="seconds recorded first and thrown away (default 5)")
    p.add_argument("--names", default=",".join(MONTAGE),
                   help="the 8 board channel names in order, comma separated")
    p.add_argument("--low", type=float, default=1.0, help="high-pass edge in Hz (default 1)")
    p.add_argument("--high", type=float, default=100.0, help="low-pass edge in Hz (default 100)")
    p.add_argument("--notch", type=float, default=60.0, help="mains notch Hz, 0 for none")
    p.add_argument("--segment", type=float, default=4.0,
                   help="Welch segment length in seconds (default 4)")
    p.add_argument("--save-dir", type=Path, default=None,
                   help="also write the raw data (.npz) and figure (.png) here")
    p.add_argument("--no-show", action="store_true", help="do not open the figure window")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--verbose", action="store_true", help="let BrainFlow log everything")
    p.add_argument("--fake", action="store_true",
                   help="use a FakeBoard with a tone at --target on P7..O2 (no hardware)")
    return p


def _writable(directory: Path) -> str:
    """'' when files can be created in ``directory`` (made if missing), else the problem."""
    reason = unwritable_reason(directory)
    return f"cannot write to {directory}: {reason}" if reason else ""


def _argument_problems(args: argparse.Namespace, names: list[str]) -> list[str]:
    bad = []
    if len(names) != 8:
        bad.append(f"--names needs 8 channel names, got {len(names)}: {names}")
    elif len(set(names)) != len(names):
        dup = sorted({n for n in names if names.count(n) > 1})
        bad.append(f"--names must not repeat a name, got {dup} more than once")
    if not args.segment > 0:
        bad.append(f"--segment must be positive, got {args.segment:g}")
    elif args.seconds < 2 * args.segment:
        bad.append(f"--seconds {args.seconds:g} is under twice --segment {args.segment:g}")
    if args.warmup < 0:
        bad.append(f"--warmup must be >= 0, got {args.warmup}")
    if args.target <= 0:
        bad.append(f"--target must be positive, got {args.target}")
    if not 0 < args.low < args.high:
        bad.append(f"need 0 < --low < --high, got {args.low}, {args.high}")
    if args.save_dir is not None and not bad:
        reason = _writable(args.save_dir)
        if reason:
            bad.append(reason)
    return bad


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    problems = _argument_problems(args, names)
    for msg in problems:
        print(f"error: {msg}", file=sys.stderr)
    if problems:
        return 2

    try:
        if args.fake:
            fs = FS
        else:
            from .board import CerelogBoard
            board = CerelogBoard(serial_port=args.port, ch_names=names, verbose=args.verbose)
            fs = board.fs
        if args.target >= fs / 2:
            print(f"error: --target must lie in (0, {fs / 2:g}) Hz at {fs:g} Hz sampling, got "
                  f"{args.target:g}", file=sys.stderr)
            return 2
        reason = segment_problem(args.segment, fs)
        if reason:
            print(f"error: {reason}", file=sys.stderr)
            return 2
        if args.fake:
            board = FakeBoard([args.target], switch_s=float("inf"), amp=5.0, ch_names=names)
        else:
            print(f"opening the Cerelog X8 on {board.serial_port or 'a BrainFlow default port'}"
                  " (up to 15 s)...", file=sys.stderr)

        def tick(done: float, total: float) -> None:
            if round(done) % 10 == 0:
                print(f"recorded {done:.0f}/{total:.0f} s", file=sys.stderr)

        with board:
            if args.warmup > 0:
                print(f"settling for {args.warmup:g} s...", file=sys.stderr)
                record(board, args.warmup)
            print(f"watch the {args.target:g} Hz target and keep still for {args.seconds:g} s",
                  file=sys.stderr)
            X, t = record(board, args.seconds, progress=tick, flush=False,
                          keep_on_interrupt=True)
    except (RuntimeError, ImportError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted before the check started", file=sys.stderr)
        return 130

    expected = int(round(args.seconds * fs))
    if X.shape[1] < 2 * args.segment * fs:
        print(f"error: {X.shape[1]} samples arrived, too few for two {args.segment:g} s "
              "segments", file=sys.stderr)
        return 1
    if X.shape[1] < 0.95 * expected:
        short = 100.0 * (1 - X.shape[1] / expected)
        print(f"warning: {X.shape[1]} of {expected} expected samples ({short:.0f}% short); "
              "is another program streaming from the board?", file=sys.stderr)

    try:
        res = run_check(X, fs, args.target, names, (args.low, args.high), args.notch,
                        args.segment)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render(res))
    if args.no_show and args.save_dir is None:
        return 0

    import matplotlib
    if args.no_show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plot(res)
    if args.save_dir is not None:
        stem = f"check_{args.target:g}Hz_{time.strftime('%Y%m%d-%H%M%S')}"
        npz, png = args.save_dir / f"{stem}.npz", args.save_dir / f"{stem}.png"
        try:
            np.savez(npz, X=X, t=t, fs=np.float64(fs), ch_names=np.array(names),
                     target=np.float64(args.target))
            fig.savefig(png, dpi=110)
        except OSError as exc:
            print(f"error: could not save to {args.save_dir}: {exc}", file=sys.stderr)
            plt.close(fig)
            return 1
        print(f"wrote {npz} and {png}", file=sys.stderr)
    if args.no_show:
        plt.close(fig)
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
