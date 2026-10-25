import CoreGraphics
import Foundation

/// A snapshot as v3.1 `scan` hints (source "ax"), in points relative to the snapshot's viewport
/// (`WindowSnapshot.viewport`: the screen holding the focused window, or the window itself for
/// a window-only snapshot).
///
/// Each hint is `{id, x, y, w, h, role, text, tag, actions, enabled}`: the item's frame cut to
/// the viewport and rounded to whole points, `text` the label (at most 120 characters, one line),
/// `role` the AX role, `tag` the subrole, at most 8 action names. Items with no area, or with
/// nothing inside the viewport, are skipped; at most 200 hints, in the snapshot's order.
public enum Hints {
    public static let maxHints = 200
    public static let maxText = 120
    public static let maxActions = 8
    public static let maxRole = 40

    public static func make(_ s: WindowSnapshot) -> [[String: Any]] {
        var out: [[String: Any]] = []
        let viewport = s.viewport
        for item in s.items {
            if out.count >= maxHints { break }
            guard let r = Geometry.toWindow(item.frame, window: viewport) else { continue }
            let x = Int(r.minX.rounded()), y = Int(r.minY.rounded())
            let w = Int(r.width.rounded()), h = Int(r.height.rounded())
            guard w > 0, h > 0 else { continue }
            out.append(["id": item.id, "x": x, "y": y, "w": w, "h": h,
                        "role": clip(item.role, maxRole), "text": oneLine(item.label, maxText),
                        "tag": clip(item.subrole, maxRole),
                        "actions": Array(item.actions.prefix(maxActions)).map { clip($0, 40) },
                        "enabled": item.enabled])
        }
        return out
    }

    /// At most `n` Unicode scalars (the server counts Python code points).
    public static func clip(_ s: String, _ n: Int) -> String {
        let scalars = s.unicodeScalars
        if scalars.count <= n { return s }
        var v = String.UnicodeScalarView()
        v.append(contentsOf: scalars.prefix(n))
        return String(v)
    }

    /// Whitespace runs (newlines too) folded to one space, trimmed, then clipped.
    public static func oneLine(_ s: String, _ n: Int) -> String {
        let folded = s.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
        return clip(folded, n)
    }
}
