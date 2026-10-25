import CoreGraphics
import Foundation

/// Conversions between protocol rects (normalized to the scan's viewport: x right, y down, in
/// [0, 1]), viewport-relative points (x, y from the viewport's top-left) and global screen
/// points (top-left origin, what AXPosition reports). The `window:` argument is the viewport
/// rect in global points: `WindowSnapshot.viewport`, which is the frame of the screen holding
/// the focused window (or the window itself for a window-only snapshot). Every conversion
/// clamps: nothing lands outside the viewport, and NaN or infinite values count as 0.
public enum Geometry {
    public static func clamp01(_ v: Double) -> Double {
        guard v.isFinite else { return 0 }
        return min(1, max(0, v))
    }

    /// The normalized rect's edges, NaN and infinity as 0, clamped into [0, 1]:
    /// (left, top, right, bottom). A rect hanging over an edge is cut there.
    static func edges(_ r: NormRect) -> (Double, Double, Double, Double) {
        func f(_ v: Double) -> Double { v.isFinite ? v : 0 }
        let x = f(r.x), y = f(r.y), w = max(0, f(r.w)), h = max(0, f(r.h))
        return (clamp01(x), clamp01(y), clamp01(x + w), clamp01(y + h))
    }

    /// The normalized rect clamped into the unit square.
    public static func clamped(_ r: NormRect) -> NormRect {
        let (l, t, rt, b) = edges(r)
        return NormRect(x: l, y: t, w: rt - l, h: b - t)
    }

    /// A protocol rect in global screen points (top-left origin), inside `window`.
    public static func toScreen(_ r: NormRect, window: CGRect) -> CGRect {
        let w = Double(window.width), h = Double(window.height)
        guard w > 0, h > 0 else { return CGRect(origin: window.origin, size: .zero) }
        let (l, t, rt, b) = edges(r)
        let x0 = Double(window.minX) + l * w, x1 = Double(window.minX) + rt * w
        let y0 = Double(window.minY) + t * h, y1 = Double(window.minY) + b * h
        return CGRect(x: x0, y: y0, width: x1 - x0, height: y1 - y0)
    }

    /// A normalized point (a `click`'s x, y) in global screen points, inside `window`.
    public static func pointToScreen(x: Double, y: Double, window: CGRect) -> CGPoint {
        CGPoint(x: Double(window.minX) + clamp01(x) * Double(window.width),
                y: Double(window.minY) + clamp01(y) * Double(window.height))
    }

    /// A global rect normalized to `window`, cut to the window. A zero-size window gives zero.
    public static func toNorm(_ r: CGRect, window: CGRect) -> NormRect {
        let w = Double(window.width), h = Double(window.height)
        guard w > 0, h > 0 else { return NormRect(x: 0, y: 0, w: 0, h: 0) }
        return clamped(NormRect(x: (Double(r.minX) - Double(window.minX)) / w,
                                y: (Double(r.minY) - Double(window.minY)) / h,
                                w: Double(r.width) / w, h: Double(r.height) / h))
    }

    /// A global rect in window-relative points, cut to the window; nil when nothing of it is
    /// inside (or it has no area).
    public static func toWindow(_ r: CGRect, window: CGRect) -> CGRect? {
        guard r.width > 0, r.height > 0, window.width > 0, window.height > 0,
              [r.minX, r.minY, r.width, r.height].allSatisfy({ $0.isFinite }) else { return nil }
        let rel = r.offsetBy(dx: -window.minX, dy: -window.minY)
        let cut = rel.intersection(CGRect(origin: .zero, size: window.size))
        guard !cut.isNull, cut.width > 0, cut.height > 0 else { return nil }
        return cut
    }
}
