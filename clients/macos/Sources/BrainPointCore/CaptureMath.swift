import CoreGraphics
import Foundation

/// The arithmetic behind a `scan.image` (PROTOCOL-v3.md section 2): which part of a display to
/// capture, how big the image is, and where a redacted (password) rect lands in it. All rects
/// in global screen points with a top-left origin unless a name says otherwise.
public enum CaptureMath {
    /// The image size for a `width` x `height` (points or pixels) source scaled so its long edge
    /// is at most `maxEdge`, keeping the aspect ratio. Never scales up; each side at least 1.
    /// Zero, negative or non-finite input gives nil.
    public static func scaledSize(width: Double, height: Double, maxEdge: Int) -> (w: Int, h: Int)? {
        guard width.isFinite, height.isFinite, width > 0, height > 0, maxEdge > 0 else { return nil }
        let long = max(width, height)
        let s = min(1.0, Double(maxEdge) / long)
        let w = max(1, min(maxEdge, Int((width * s).rounded())))
        let h = max(1, min(maxEdge, Int((height * s).rounded())))
        return (w, h)
    }

    /// The part of `rect` that lies on the display whose global frame is `display`, in
    /// display-local points (origin at the display's top-left), the form
    /// `SCStreamConfiguration.sourceRect` takes. Nil when they do not overlap.
    public static func sourceRect(_ rect: CGRect, onDisplay display: CGRect) -> CGRect? {
        let r = rect.standardized.intersection(display)
        guard !r.isNull, r.width >= 1, r.height >= 1 else { return nil }
        return CGRect(x: r.minX - display.minX, y: r.minY - display.minY, width: r.width, height: r.height)
    }

    /// The display (by index into `displays`, global frames) a capture of `rect` should use:
    /// the one holding the rect's centre, else the one with the largest overlap. Nil when the
    /// rect is on no display.
    public static func displayIndex(for rect: CGRect, displays: [CGRect]) -> Int? {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        if let i = displays.firstIndex(where: { $0.contains(c) }) { return i }
        var best: (Int, Double)?
        for (i, d) in displays.enumerated() {
            let x = rect.standardized.intersection(d)
            guard !x.isNull else { continue }
            let area = Double(x.width * x.height)
            if area > 0, area > (best?.1 ?? 0) { best = (i, area) }
        }
        return best?.0
    }

    /// `redact` (global points) in pixels of an image `imageW` x `imageH` that shows exactly
    /// `captured` (global points), with a top-left origin. Cut at the image's edges; nil when
    /// it falls outside. Rounded outwards, so a partly covered pixel is covered.
    public static func imageRect(of redact: CGRect, captured: CGRect, imageW: Int, imageH: Int) -> CGRect? {
        guard captured.width > 0, captured.height > 0, imageW > 0, imageH > 0 else { return nil }
        let r = redact.standardized.intersection(captured)
        guard !r.isNull, r.width > 0, r.height > 0 else { return nil }
        let sx = Double(imageW) / Double(captured.width), sy = Double(imageH) / Double(captured.height)
        let x0 = max(0, (Double(r.minX - captured.minX) * sx).rounded(.down))
        let y0 = max(0, (Double(r.minY - captured.minY) * sy).rounded(.down))
        let x1 = min(Double(imageW), (Double(r.maxX - captured.minX) * sx).rounded(.up))
        let y1 = min(Double(imageH), (Double(r.maxY - captured.minY) * sy).rounded(.up))
        guard x1 > x0, y1 > y0 else { return nil }
        return CGRect(x: x0, y: y0, width: x1 - x0, height: y1 - y0)
    }

    /// A top-left-origin image rect in a bottom-left-origin context (CGContext) of height `imageH`.
    public static func flipped(_ r: CGRect, imageH: Int) -> CGRect {
        CGRect(x: r.minX, y: CGFloat(imageH) - r.maxY, width: r.width, height: r.height)
    }
}
