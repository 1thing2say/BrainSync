"""What the live map shows for one window of EEG: band power, SSVEP SNR, contact quality.

    filt = CausalFilter(fs, n_ch)          # one per stream, fed every chunk in order
    Y = filt(X)                            # X raw µV (n_ch, n) -> 1-100 Hz, mains notched
    f = window_features(raw, fs, filtered=Y_window)   # the newest 2 s of both
    f.db                                   # (len(BANDS), n_ch) band power, dB re 1 µV²/Hz
    f.snr                                  # (len(ssvep_hz), n_ch) SSVEP SNR of this window, dB
    f.quality                              # ["ok", "flat", ...] per channel
    snr = SsvepSnr(f.freqs, (15.0, 17.0), n_ch, n_avg=16)  # then snr.update(f.power, f.ok)
    base = Baseline(30.0, n_channels=8)    # then base.add(db, f.ok, elapsed_s, ptp=f.ptp)
    z = base.z(db)                         # NaN until a channel's rest baseline is ready

The stream filters causally (1-100 Hz band-pass, mains notch) with the filter state carried
from chunk to chunk, so each window is a slice of one continuously filtered signal. Filtering
each 2 s window on its own instead would ring at both ends: a 200 µV mains hum turns into
fake delta power and an inflated RMS. ``window_features`` without ``filtered`` still filters
the window by itself, for offline use, and has exactly that edge problem.

The spectrum is one Hann segment over the whole window, so a 2 s window gives 0.5 Hz bins:
fine enough for an SNR reference up to 2 Hz either side of 15 and 17 Hz. One 2 s periodogram
is far too noisy to call an SSVEP from (noise alone reaches 6-10 dB), so the stream's SNR
comes from the PSD averaged over the last ~4 s of hops (``SsvepSnr``). Raw band power varies
a lot from person to person and electrode to electrode, so the map shows it as a z-score
against the user's own rest period instead: "alpha is up 3 standard deviations on O1" means
something, "alpha is 4 dB on O1" does not. Contact quality uses the electrode check's
thresholds; a channel that is not "ok" gets no z-score, so the map greys it rather than
interpolating junk.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

from ..board import ADC_FULL_SCALE_UV
from ..check import CLIP_LEVEL, diagnose, segment_problem, welch_psd
from ..signal import preprocess

BANDS: dict[str, tuple[float, float]] = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
}
FILTER_BAND = (1.0, 100.0)  # as brainsync.check, whose flat/noisy RMS limits assume it
FILTER_ORDER = 4
NOTCH_Q = 35.0              # as brainsync.signal.notch
POWER_FLOOR = 1e-12         # µV²/Hz; a dead channel reads -120 dB instead of -inf
STD_FLOOR_DB = 0.5          # a near-constant baseline must not turn noise into huge z
MIN_BASELINE_WINDOWS = 4    # good windows a channel needs before it gets a z-score
SNR_SPAN_HZ = 2.0           # SNR reference bins lie up to this far from the target ...
SNR_GUARD_BINS = 1          # ... but not in the bins next to it (Hann leakage)
SNR_AVG_S = 4.0             # the stream's SNR averages the PSD over this many seconds of hops
# A window whose filtered peak-to-peak tops this on a good channel is a blink or a movement,
# not rest: it stays out of every channel's baseline. Blinks reach 100-300 µV on Fp1/Fp2;
# resting EEG after the 1 Hz high-pass stays well under 150 µV in 2 s, strong alpha included.
ARTIFACT_PTP_UV = 150.0
ARTIFACT_HOLDOFF_S = 1.0    # and so do the windows just after: the high-pass's undershoot
OUTLIER_MADS = 5.0          # band dB this many robust SDs off the running median: not rest
OUTLIER_MIN_WINDOWS = 16    # kept windows before the running-median test starts (4 s)
CHRONIC_MIN_WINDOWS = 8     # a channel over the ptp limit in more than CHRONIC_SHARE of
CHRONIC_SHARE = 0.75        # its windows no longer rejects the other channels' windows
MAD_TO_SD = 1.4826          # MAD of a normal distribution times this is its SD
QUALITIES = ("ok", "flat", "noisy", "clipped", "offset", "invalid")


@dataclass(frozen=True)
class WindowFeatures:
    """Features of one window; arrays have one column (or row) per channel."""

    db: np.ndarray            # (n_bands, n_ch)
    snr: np.ndarray           # (n_freqs, n_ch), this window alone
    rms: np.ndarray           # (n_ch,) µV after filtering
    ptp: np.ndarray           # (n_ch,) µV peak-to-peak after filtering
    quality: list[str]
    freqs: np.ndarray         # (n_bins,) Hz
    power: np.ndarray         # (n_ch, n_bins) µV²/Hz

    @property
    def ok(self) -> np.ndarray:
        return np.array([q == "ok" for q in self.quality], dtype=bool)


def causal_sos(fs: float, band: tuple[float, float] = FILTER_BAND,
               line_hz: float = 60.0) -> np.ndarray:
    """Second-order sections of the stream's band-pass followed by its mains notch.

    Same edges as ``brainsync.signal.preprocess``: a high edge at or above Nyquist leaves a
    high-pass, and a notch at or above Nyquist (or ``line_hz`` 0) is skipped.
    """
    fs = float(fs)
    nyq = fs / 2.0
    lo, hi = float(band[0]), float(band[1])
    if not 0 < lo < nyq:
        raise ValueError(f"the {lo:g} Hz high-pass edge must lie in (0, {nyq:g}) Hz")
    if hi >= nyq:
        sos = sps.butter(FILTER_ORDER, lo, btype="highpass", fs=fs, output="sos")
    else:
        sos = sps.butter(FILTER_ORDER, [lo, hi], btype="bandpass", fs=fs, output="sos")
    if line_hz and 0 < line_hz < nyq:
        b, a = sps.iirnotch(float(line_hz), NOTCH_Q, fs=fs)
        sos = np.vstack([sos, sps.tf2sos(b, a)])
    return sos


class CausalFilter:
    """The stream's band-pass and notch, run sample by sample with the state kept between calls.

    The state starts as if the first sample had always been there (``sosfilt_zi`` times it),
    so a large DC offset does not kick the filter. Non-finite samples and samples at the
    ADC rail are replaced by the channel's last usable one before filtering: the windows
    that hold them are flagged "invalid" or "clipped" from the raw samples anyway, NaN in
    the state would stay there forever, and a 187 mV spike would ring on for a second of
    windows after it. Splitting the input into chunks does not change the output.
    """

    def __init__(self, fs: float, n_channels: int, band: tuple[float, float] = FILTER_BAND,
                 line_hz: float = 60.0) -> None:
        self.sos = causal_sos(fs, band, line_hz)
        self.n_channels = int(n_channels)
        self._zi: np.ndarray | None = None
        self._last = np.zeros(self.n_channels)

    def __call__(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] != self.n_channels:
            raise ValueError(f"expected ({self.n_channels}, n) samples, got shape {X.shape}")
        if X.shape[1] == 0:
            return X.copy()
        X = self._hold(X)
        if self._zi is None:
            self._zi = sps.sosfilt_zi(self.sos)[:, None, :] * X[None, :, 0, None]
        Y, self._zi = sps.sosfilt(self.sos, X, axis=-1, zi=self._zi)
        return Y

    def _hold(self, X: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            finite = np.abs(X) < CLIP_LEVEL * ADC_FULL_SCALE_UV     # False for NaN too
        if finite.all():
            self._last = X[:, -1].copy()
            return X
        idx = np.where(finite, np.arange(X.shape[1]), -1)
        np.maximum.accumulate(idx, axis=1, out=idx)
        rows = np.arange(X.shape[0])[:, None]
        held = np.where(idx >= 0, X[rows, np.maximum(idx, 0)], self._last[:, None])
        self._last = held[:, -1].copy()
        return held


def channel_quality(raw: np.ndarray, rms: float) -> str:
    """One word for a channel's contact from its raw µV samples and its filtered RMS.

    Unlike the electrode check, a single sample at the rail flags the window: the step it
    makes spreads over the whole spectrum of a 2 s window.
    """
    if not np.isfinite(raw).all():
        return "invalid"
    if np.any(np.abs(raw) >= CLIP_LEVEL * ADC_FULL_SCALE_UV):
        return "clipped"
    flags = diagnose(raw, rms)
    for prefix, word in (("railed", "clipped"), ("flat", "flat"), ("noisy", "noisy"),
                         ("large DC", "offset")):
        if any(f.startswith(prefix) for f in flags):
            return word
    return "ok"


def band_db(freqs: np.ndarray, power: np.ndarray,
            bands: Mapping[str, tuple[float, float]] = BANDS) -> np.ndarray:
    """10 log10 of the mean PSD over [lo, hi) for each band: (n_bands, n_ch)."""
    out = np.empty((len(bands), power.shape[0]))
    for i, (name, (lo, hi)) in enumerate(bands.items()):
        inside = (freqs >= lo) & (freqs < hi)
        if not inside.any():
            raise ValueError(f"band {name} {lo:g}-{hi:g} Hz holds no {freqs[1] - freqs[0]:g} Hz bin")
        out[i] = power[:, inside].mean(axis=1)
    return 10.0 * np.log10(np.maximum(out, POWER_FLOOR))


def snr_bins(freqs: np.ndarray, ssvep_hz: Sequence[float], span_hz: float = SNR_SPAN_HZ,
             guard: int = SNR_GUARD_BINS) -> list[tuple[int, np.ndarray]]:
    """(target bin, reference bins) for each SSVEP frequency.

    The reference is every bin up to ``span_hz`` away except the ``guard`` bins next to the
    target and the bins at or next to the other targets: 15 and 17 Hz lie 2 Hz apart, and
    the 15 Hz peak of someone looking at 15 Hz must not count as noise for 17 Hz.
    """
    df = float(freqs[1] - freqs[0])
    targets = [int(round((float(f) - freqs[0]) / df)) for f in ssvep_hz]
    m = int(round(span_hz / df))
    offsets = np.arange(-m, m + 1)
    out = []
    for f, k in zip(ssvep_hz, targets):
        if not 0 < k < freqs.size:
            raise ValueError(f"{f:g} Hz lies outside the {freqs[-1]:g} Hz spectrum")
        ring = k + offsets[np.abs(offsets) > guard]
        ring = ring[(ring >= 0) & (ring < freqs.size)]
        for other in targets:
            if other != k:
                ring = ring[np.abs(ring - other) > guard]
        if ring.size == 0:
            raise ValueError(f"{df:g} Hz bins leave no SNR reference within {span_hz:g} Hz of "
                             f"{f:g} Hz")
        out.append((k, ring))
    return out


def snr_db(power: np.ndarray, bins: Sequence[tuple[int, np.ndarray]]) -> np.ndarray:
    """Target bin over the mean of its reference bins, in dB: (len(bins), *power.shape[:-1])."""
    tiny = np.finfo(float).tiny
    return np.stack([10.0 * np.log10(np.maximum(power[..., k], tiny)
                                     / np.maximum(power[..., ring].mean(axis=-1), tiny))
                     for k, ring in bins])


def window_features(X: np.ndarray, fs: float, *, filtered: np.ndarray | None = None,
                    bands: Mapping[str, tuple[float, float]] = BANDS,
                    ssvep_hz: Sequence[float] = (15.0, 17.0),
                    line_hz: float = 60.0) -> WindowFeatures:
    """Band power, SSVEP SNR and contact quality of raw µV ``X`` (n_ch, n).

    ``filtered`` is the same window cut from the stream's continuously filtered signal
    (``CausalFilter``); without it the window is filtered on its own, edge effects and all.
    Contact quality always comes from the raw samples (rail, DC) and the filtered RMS.
    """
    X = np.asarray(X, dtype=float)
    if X.ndim != 2 or X.shape[0] < 1:
        raise ValueError(f"X must be (n_ch, n) samples, got shape {X.shape}")
    fs = float(fs)
    reason = segment_problem(X.shape[1] / fs, fs, SNR_SPAN_HZ, SNR_GUARD_BINS)
    if reason:
        raise ValueError(f"a {X.shape[1]}-sample window is too short: {reason}")
    raw = X
    if filtered is None:
        finite = np.isfinite(X)
        if not finite.all():
            X = np.where(finite, X, 0.0)  # keep NaN out of the filters; the channel is flagged
        Y = preprocess(X, fs, band=FILTER_BAND, notch_hz=line_hz)
    else:
        Y = np.asarray(filtered, dtype=float)
        if Y.shape != X.shape:
            raise ValueError(f"filtered must match X {X.shape}, got {Y.shape}")
    freqs, power = welch_psd(Y, fs, segment_s=Y.shape[1] / fs)
    centred = Y - Y.mean(axis=1, keepdims=True)
    rms = np.sqrt(np.mean(centred * centred, axis=1))
    ptp = Y.max(axis=1) - Y.min(axis=1)
    snr = snr_db(power, snr_bins(freqs, ssvep_hz))
    quality = [channel_quality(raw[i], float(rms[i])) for i in range(X.shape[0])]
    return WindowFeatures(band_db(freqs, power, bands), snr, rms, ptp, quality, freqs, power)


class SsvepSnr:
    """SSVEP SNR from each channel's PSD averaged over its newest ``n_avg`` windows.

    Only the target and reference bins are kept, so a frame costs a few hundred additions.
    The average restarts whenever a channel's contact is not "ok": a bad window reports its
    own SNR, and the next good one starts a fresh average, so a clipped second does not
    linger for ``n_avg`` hops.
    """

    def __init__(self, freqs: np.ndarray, ssvep_hz: Sequence[float], n_channels: int,
                 n_avg: int, *, span_hz: float = SNR_SPAN_HZ,
                 guard: int = SNR_GUARD_BINS) -> None:
        if int(n_avg) < 1:
            raise ValueError(f"n_avg must be >= 1, got {n_avg}")
        bins = snr_bins(np.asarray(freqs, dtype=float), ssvep_hz, span_hz, guard)
        self.n_avg = int(n_avg)
        self._idx = np.unique(np.concatenate([[k, *ring] for k, ring in bins]))
        where = {int(b): i for i, b in enumerate(self._idx)}
        self._bins = [(where[k], np.array([where[int(r)] for r in ring])) for k, ring in bins]
        self._hist = np.zeros((self.n_avg, int(n_channels), self._idx.size))
        self._run = np.zeros(int(n_channels), dtype=int)   # windows in each channel's average
        self._prev_ok = np.zeros(int(n_channels), dtype=bool)

    def update(self, power: np.ndarray, ok: np.ndarray) -> np.ndarray:
        """Add one window's PSD (n_ch, n_bins); the averaged SNR (n_freqs, n_ch) in dB."""
        ok = np.asarray(ok, dtype=bool)
        self._hist = np.roll(self._hist, 1, axis=0)
        self._hist[0] = np.asarray(power, dtype=float)[:, self._idx]
        self._run = np.where(ok & self._prev_ok, np.minimum(self._run + 1, self.n_avg), 1)
        self._prev_ok = ok
        use = np.arange(self.n_avg)[:, None] < self._run[None, :]          # (n_avg, n_ch)
        mean = (self._hist * use[:, :, None]).sum(axis=0) / self._run[:, None]
        return snr_db(mean, self._bins)


class Baseline:
    """Per channel and band median and robust spread of band power over a rest period.

    ``add`` is called once per window with the window's band dB, which channels were good,
    how long the stream has been collecting and, ideally, each channel's filtered
    peak-to-peak. The rest period ends ``seconds`` in; then every channel that was good all
    along is frozen with what it collected. A channel that was bad at any point first needs
    a full rest period of its own: ``round(seconds / hop_s)`` windows that start after its
    last bad window ended (so ``window_s`` after it), with z NaN until then. Windows from
    before ``settled`` (the filters' start-up) never count.

    Windows that do not look like rest stay out: a filtered peak-to-peak over
    ``artifact_ptp_uv`` on any good channel (a blink or a movement) keeps the window out of
    every channel, as do the windows of the next ``ARTIFACT_HOLDOFF_S`` (the high-pass
    turns a blink into a slow undershoot that is still delta power but under the limit),
    and band dB more than ``outlier_mads`` robust SDs from the channel's
    running median keeps it out of that channel. A channel over the peak-to-peak limit in
    nearly every window (``CHRONIC_SHARE``: someone blinking every 2 s, a very large or
    loose site) still rejects its own windows but no longer the other channels', or one
    such site would leave every channel without a baseline. The frozen statistics are
    the median and 1.4826 times the median absolute deviation, so the odd artifact that got
    through moves them little; the spread is floored at ``std_floor_db``.
    """

    def __init__(self, seconds: float, n_channels: int, n_bands: int = len(BANDS), *,
                 hop_s: float = 0.25, window_s: float = 2.0,
                 min_windows: int = MIN_BASELINE_WINDOWS,
                 std_floor_db: float = STD_FLOOR_DB,
                 artifact_ptp_uv: float = ARTIFACT_PTP_UV,
                 outlier_mads: float = OUTLIER_MADS) -> None:
        if not (np.isfinite(seconds) and seconds >= 0):
            raise ValueError(f"baseline seconds must be >= 0, got {seconds}")
        if int(n_channels) < 1 or int(n_bands) < 1 or int(min_windows) < 1:
            raise ValueError(f"need n_channels, n_bands, min_windows >= 1, got "
                             f"{n_channels}, {n_bands}, {min_windows}")
        if not std_floor_db > 0:
            raise ValueError(f"std_floor_db must be positive, got {std_floor_db}")
        if not (hop_s > 0 and window_s > 0 and artifact_ptp_uv > 0 and outlier_mads > 0):
            raise ValueError(f"hop_s, window_s, artifact_ptp_uv and outlier_mads must be "
                             f"positive, got {hop_s}, {window_s}, {artifact_ptp_uv}, "
                             f"{outlier_mads}")
        n_ch, self.n_bands = int(n_channels), int(n_bands)
        self.seconds = float(seconds)
        self.hop_s = float(hop_s)
        self.window_s = float(window_s)
        self.min_windows = int(min_windows)
        self.std_floor_db = float(std_floor_db)
        self.artifact_ptp_uv = float(artifact_ptp_uv)
        self.outlier_mads = float(outlier_mads)
        self.full_windows = max(int(round(self.seconds / self.hop_s)), self.min_windows)
        self.holdoff_windows = int(round(ARTIFACT_HOLDOFF_S / self.hop_s))
        self.elapsed_s = 0.0
        self._kept: list[list[np.ndarray]] = [[] for _ in range(n_ch)]
        self.rejected = np.zeros(n_ch, dtype=int)     # artifact windows left out, per channel
        self._late = np.zeros(n_ch, dtype=bool)       # bad at some point before freezing
        self._last_bad = np.full(n_ch, -np.inf)       # elapsed_s of the newest bad window
        self._clean_run = np.zeros(n_ch, dtype=int)   # eligible windows since then
        self._seen = np.zeros(n_ch, dtype=int)        # for the "just large" test
        self._over = np.zeros(n_ch, dtype=int)
        self._hold = np.zeros(n_ch, dtype=int)        # hold-off windows still to reject
        self._frozen = np.zeros(n_ch, dtype=bool)
        self._mean = np.full((self.n_bands, n_ch), np.nan)
        self._std = np.full((self.n_bands, n_ch), np.nan)

    @property
    def state(self) -> str:
        return "ready" if self.elapsed_s >= self.seconds - 1e-9 else "collecting"

    @property
    def count(self) -> np.ndarray:
        """Windows each channel has kept so far (n_ch,)."""
        return np.array([len(k) for k in self._kept], dtype=int)

    def ready(self) -> np.ndarray:
        """Channels that have a baseline to z-score against, (n_ch,) bool."""
        return self._frozen.copy()

    def add(self, db: np.ndarray, ok: np.ndarray, elapsed_s: float, *,
            ptp: np.ndarray | None = None, settled: bool = True) -> None:
        """Count one window's band dB (n_bands, n_ch) for the channels still collecting."""
        db = np.asarray(db, dtype=float)
        ok = np.asarray(ok, dtype=bool)
        n_ch = self._frozen.size
        if db.shape != (self.n_bands, n_ch) or ok.shape != (n_ch,):
            raise ValueError(f"expected db {(self.n_bands, n_ch)} and ok {(n_ch,)}, got "
                             f"{db.shape} and {ok.shape}")
        elapsed_s = float(elapsed_s)
        self.elapsed_s = max(self.elapsed_s, elapsed_s)
        good = ok & np.isfinite(db).all(axis=0)
        open_ = ~self._frozen
        bad = open_ & ~good
        self._late |= bad
        self._last_bad[bad] = elapsed_s
        self._clean_run[bad] = 0
        for c in np.flatnonzero(bad):
            self._kept[c].clear()
        eligible = open_ & good & settled & (elapsed_s - self._last_bad >= self.window_s - 1e-9)
        self._clean_run[eligible] += 1
        artifact = self._artifacts(db, good & settled, ptp) & eligible
        self.rejected[artifact] += 1
        for c in np.flatnonzero(eligible & ~artifact):
            self._kept[c].append(db[:, c].copy())
        if self.state == "ready":
            count = self.count
            due = open_ & (count >= self.min_windows) & (
                ~self._late | (self._clean_run >= self.full_windows))
            for c in np.flatnonzero(due):
                self._freeze(c)

    def _artifacts(self, db: np.ndarray, good: np.ndarray, ptp: np.ndarray | None) -> np.ndarray:
        """Which channels should leave this window out of their baseline."""
        n_ch = self._frozen.size
        out = np.zeros(n_ch, dtype=bool)
        if ptp is not None:
            ptp = np.asarray(ptp, dtype=float)
            if ptp.shape != (n_ch,):
                raise ValueError(f"expected ptp ({n_ch},), got {ptp.shape}")
            over = good & ~(ptp <= self.artifact_ptp_uv)      # NaN counts as over
            self._seen[good] += 1
            self._over[over] += 1
            chronic = ((self._seen >= CHRONIC_MIN_WINDOWS)
                       & (self._over > CHRONIC_SHARE * self._seen))
            out[:] = (over & ~chronic).any()
            out |= over
            held = self._hold > 0
            self._hold = np.where(out, self.holdoff_windows, np.maximum(self._hold - 1, 0))
            out |= held
        for c in np.flatnonzero(~out):
            kept = self._kept[c]
            if len(kept) < OUTLIER_MIN_WINDOWS:
                continue
            med, spread = self._robust(np.array(kept))
            out[c] = bool(np.any(np.abs(db[:, c] - med) > self.outlier_mads * spread))
        return out

    def _robust(self, rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        med = np.median(rows, axis=0)
        mad = np.median(np.abs(rows - med), axis=0)
        return med, np.maximum(MAD_TO_SD * mad, self.std_floor_db)

    def _freeze(self, c: int) -> None:
        self._mean[:, c], self._std[:, c] = self._robust(np.array(self._kept[c]))
        self._frozen[c] = True
        self._kept[c] = []

    def stats(self) -> tuple[np.ndarray, np.ndarray]:
        """(median, robust SD) dB, each (n_bands, n_ch), NaN for channels without a baseline."""
        return self._mean.copy(), self._std.copy()

    def z(self, db: np.ndarray) -> np.ndarray:
        """(db - median) / robust SD per band and channel; NaN where there is no baseline yet."""
        return (np.asarray(db, dtype=float) - self._mean) / self._std

    def status(self) -> dict:
        return {"state": self.state, "elapsed_s": round(min(self.elapsed_s, self.seconds), 3),
                "seconds": self.seconds}
