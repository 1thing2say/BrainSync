import Foundation

/// Frame-indexed flicker luminance: a bit-exact port of `extension/flicker.js` and
/// `brainsync/stimulus/flicker.py` `luminance()`.
///
/// Frame `n` of a target at `freq` Hz on a `refresh` Hz display shows
/// `0.5 * (1 + depth * sin(2 pi freq n / refresh + phase))` for the sine waveform. The square
/// waveform is high for the first half of every cycle of that same phase. The arithmetic keeps
/// numpy's operation order, and the square waveform uses numpy's float mod (the result takes the
/// divisor's sign), so the values match `Tests/.../Fixtures/flicker_fixture.json`.
public enum Luminance {
    public static let waveforms = ["sine", "square"]
    /// A frame interval longer than this many refresh periods is a dropped frame (flicker.py).
    public static let dropFactor = 1.5
    /// What a still target shows.
    public static let still = 0.5

    /// True when `value` would compute: refresh > 0, a known waveform, depth in (0, 1].
    public static func isValid(refresh: Double, waveform: String = "sine", depth: Double = 1.0) -> Bool {
        refresh > 0 && refresh.isFinite && waveforms.contains(waveform) && depth > 0 && depth <= 1
    }

    /// Luminance in [0.5 - depth/2, 0.5 + depth/2] on frame index `frame`. Arguments that
    /// flicker.js rejects with a RangeError (refresh <= 0, an unknown waveform, depth outside
    /// (0, 1] or NaN) give the still luminance 0.5 here: never a flicker on bad input.
    public static func value(freq: Double, phase: Double, frame: Int, refresh: Double,
                             waveform: String = "sine", depth: Double = 1.0) -> Double {
        guard isValid(refresh: refresh, waveform: waveform, depth: depth) else { return still }
        let n = Double(frame)
        if waveform == "sine" {
            return 0.5 * (1.0 + depth * sin(2.0 * Double.pi * freq * n / refresh + phase))
        }
        let cycles = (freq * n) / refresh + phase / (2.0 * Double.pi)
        let on = pyMod(cycles, 1.0) < 0.5 ? 1.0 : 0.0
        return 0.5 * (1.0 + depth * (2.0 * on - 1.0))
    }

    /// numpy.mod for floats: fmod, then shifted into the divisor's sign.
    public static func pyMod(_ a: Double, _ b: Double) -> Double {
        var m = fmod(a, b)
        if m != 0 && (m < 0) != (b < 0) { m += b }
        return m
    }

    /// 15-25 Hz flicker is the band most likely to trigger photosensitive seizures.
    public static func photosensitive(_ freq: Double) -> Bool { freq >= 15.0 && freq <= 25.0 }
}

/// Frame counting shared with `clients/js/brainsync-client.js` FlickerState.
public enum FlickerMath {
    /// Vsync count since onset `t0` (seconds) at `refresh` Hz: round((t - t0) * refresh).
    public static func frameIndex(t: Double, t0: Double, refresh: Double) -> Int {
        guard refresh > 0, t.isFinite, t0.isFinite else { return 0 }
        return Int(((t - t0) * refresh).rounded())
    }

    /// True when one frame interval (s) spans more than 1.5 refresh periods.
    public static func isDropped(interval: Double, refresh: Double) -> Bool {
        refresh > 0 && interval > Luminance.dropFactor / refresh
    }

    /// The refresh the luminance is computed at: the welcome's nominal `refresh_hz`, or the
    /// measured one when that is further than the tolerance from it, so every frequency stays
    /// true in real time (PROTOCOL-v3 section 3, `layout`).
    public static func lumHz(measured: Double?, nominal: Double, tolerance: Double) -> Double {
        if let m = measured, m > 0, m.isFinite, abs(m - nominal) > tolerance { return m }
        return nominal
    }

    /// A target at or above half the refresh would alias and is drawn still.
    public static func canShow(freq: Double, refresh: Double) -> Bool {
        freq.isFinite && freq > 0 && 2 * freq < refresh
    }
}
