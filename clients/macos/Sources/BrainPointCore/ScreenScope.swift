import CoreGraphics
import Foundation

/// The pure parts of a screen-scope scan: which screen holds the focused window, where the
/// synthetic scroll strips go, whether the Dock is showing, and how the item groups (open
/// menus, window, scroll strips, menu bar, Dock) are ordered, capped and numbered.
///
/// All rects are global points with a top-left origin (y down from the top of the primary
/// display), like AXPosition.
public enum ScreenScope {
    public static let scrollUpTag = "BPScrollUp"
    public static let scrollDownTag = "BPScrollDown"
    public static let scrollRole = "AXScrollArea"
    public static let dockRole = "AXDockItem"

    /// A Cocoa rect (bottom-left origin, NSScreen.frame) in global top-left points, given the
    /// primary (menu-bar) screen's height.
    public static func toTopLeft(cocoa r: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: r.minX, y: primaryHeight - r.maxY, width: r.width, height: r.height)
    }

    /// The index of the screen that holds most of `window` (top-left points), else the one
    /// containing its center, else the first; nil when there are no screens.
    public static func screenIndex(for window: CGRect, screens: [CGRect]) -> Int? {
        guard !screens.isEmpty else { return nil }
        var best: (Int, CGFloat)? = nil
        for (i, s) in screens.enumerated() {
            let cut = s.intersection(window)
            guard !cut.isNull else { continue }
            let a = cut.width * cut.height
            if a > 0, a > (best?.1 ?? 0) { best = (i, a) }
        }
        if let b = best { return b.0 }
        let c = CGPoint(x: window.midX, y: window.midY)
        return screens.firstIndex { $0.contains(c) } ?? 0
    }

    /// True when at least `minFraction` of `r`'s area lies on `screen`.
    public static func onScreen(_ r: CGRect, screen: CGRect, minFraction: CGFloat = 0.5) -> Bool {
        guard r.width > 0, r.height > 0 else { return false }
        let cut = r.intersection(screen)
        guard !cut.isNull else { return false }
        return cut.width * cut.height >= minFraction * r.width * r.height
    }

    /// The two strips of a scroll area: centered, at most 240 pt wide (half the area), at
    /// most 32 pt high (a sixth of the area), 4 pt inside the top and bottom edges. An area
    /// too small for strips (under 40 x 40 pt) gives nil.
    public static func scrollStrips(area: CGRect) -> (up: CGRect, down: CGRect)? {
        guard area.width >= 40, area.height >= 40,
              [area.minX, area.minY, area.width, area.height].allSatisfy({ $0.isFinite }) else { return nil }
        let w = min(240, area.width / 2)
        let h = min(32, area.height / 6)
        let x = area.midX - w / 2
        let inset: CGFloat = 4
        return (CGRect(x: x, y: area.minY + inset, width: w, height: h),
                CGRect(x: x, y: area.maxY - inset - h, width: w, height: h))
    }

    /// Whether the Dock is showing on `screen`: its item list lies on that screen and outside
    /// the screen's visible frame (the area macOS leaves for windows, which excludes a shown
    /// Dock but not a hidden auto-hide one). Both frames are top-left points.
    public static func dockVisible(listFrame: CGRect, screen: CGRect, visibleFrame: CGRect) -> Bool {
        guard onScreen(listFrame, screen: screen, minFraction: 0.9) else { return false }
        let cut = listFrame.intersection(visibleFrame)
        let overlap = cut.isNull ? 0 : cut.width * cut.height
        return overlap < 0.5 * listFrame.width * listFrame.height
    }

    /// Orders the groups (open menus, window, scroll strips, menu bar, Dock: `ItemKind`'s
    /// order), keeps the order inside each group, caps the total at `cap` and numbers the
    /// items 0, 1, ... (their hint ids). The window keeps at least half of the cap, and
    /// otherwise whatever the other groups leave, so a busy page cannot push the menu bar,
    /// the Dock and the scroll strips out. `truncated` is true when anything was cut.
    /// `payload` rides along (the platform's AXUIElement).
    public static func assemble<T>(_ entries: [(item: AXItem, payload: T)], cap: Int = Hints.maxHints)
        -> (entries: [(item: AXItem, payload: T)], truncated: Bool) {
        var groups: [ItemKind: [(item: AXItem, payload: T)]] = [:]
        for e in entries { groups[e.item.kind, default: []].append(e) }
        let others = entries.count - (groups[.window]?.count ?? 0)
        let windowCap = max(cap / 2, cap - others)
        var out: [(item: AXItem, payload: T)] = []
        var truncated = false
        for kind in ItemKind.allCases {
            var g = groups[kind] ?? []
            if kind == .window, g.count > windowCap {
                g = Array(g.prefix(windowCap))
                truncated = true
            }
            for e in g {
                if out.count >= cap { truncated = true; break }
                out.append(e)
            }
        }
        for i in out.indices { out[i].item.id = i }
        return (out, truncated)
    }
}
