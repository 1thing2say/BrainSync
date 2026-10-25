import AppKit
import ApplicationServices
import BrainPointCore

/// Bounds for one screen-scope walk.
public struct ScreenLimits: Sendable {
    /// The focused window's walk (`AXWalker`); it runs first.
    public var window = WalkLimits(maxMs: 140)
    /// The whole walk (window, menus, scroll strips, Dock); what is left after the window walk
    /// is shared by the others, in that order.
    public var maxTotalMs = 195.0
    public var maxMenuItems = 150
    public var maxDockItems = 80
    public var menus = true
    public var scroll = true
    public var dock = true
    public init() {}
}

/// Walks everything BrainPoint offers on the screen that holds the front app's focused window:
///
/// - the focused window's pressable items (`AXWalker`);
/// - the front app's open menus (a menu-bar menu, a context menu, an open submenu) and its
///   top-level menu-bar items (pressing one opens its menu; the next scan sees the menu);
/// - two synthetic "Scroll up" / "Scroll down" strips in the window's largest visible scroll
///   area (`ScreenScope.scrollStrips`), pressed by ElementRegistry with AXScrollUpByPage /
///   AXScrollDownByPage, the vertical scroll bar's AXDecrement / AXIncrement, or a scroll-wheel
///   event, whichever the area offers;
/// - the Dock's app and minimized-window items as the app switcher, only when the Dock shows
///   on that screen, never BrainPoint's own.
///
/// Items outside that screen are dropped. `ScreenScope.assemble` orders them (open menus,
/// window, scroll, menu bar, Dock), caps them at 200 and numbers them. The snapshot's
/// `screenFrame` (global top-left points) is the scan's viewport. Nothing is pressed, focused
/// or activated, and no value of a text input is read.
public enum ScreenWalker {
    public static let dockBundleID = "com.apple.dock"
    /// Dock items offered: apps and minimized windows (not folders, the Trash or separators).
    static let dockSubroles: Set<String> = ["AXApplicationDockItem", "AXMinimizedWindowDockItem"]
    static let maxMenuDepth = 4

    private static let menuBatch: [String] = [
        kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
        kAXHelpAttribute, kAXPositionAttribute, kAXSizeAttribute, kAXEnabledAttribute,
        kAXChildrenAttribute, kAXSelectedAttribute,
    ]
    private static let dockBatch: [String] = [
        kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXPositionAttribute,
        kAXSizeAttribute, kAXURLAttribute,
    ]

    public struct Result {
        public var snapshot: WindowSnapshot
        public var elements: [Int: AXUIElement]
        /// AX nodes read (window walk plus menus, Dock and scroll area).
        public var nodes: Int
        /// The backing scale of the screen holding the window (the scan's `dpr`).
        public var screenScale: Double
    }

    @MainActor
    public static func snapshotWithElements(pid: Int32, limits: ScreenLimits = .init())
        -> (WindowSnapshot, [Int: AXUIElement])? {
        walk(pid: pid, limits: limits).map { ($0.snapshot, $0.elements) }
    }

    @MainActor
    public static func walk(pid: Int32, limits: ScreenLimits = .init()) -> Result? {
        let start = DispatchTime.now().uptimeNanoseconds
        func elapsed() -> Double { Double(DispatchTime.now().uptimeNanoseconds - start) / 1e6 }
        func overBudget() -> Bool { elapsed() > limits.maxTotalMs }

        guard let w = AXWalker.walk(pid: pid, limits: limits.window) else { return nil }
        var kindMs: [String: Double] = ["window": elapsed()]
        var nodes = w.nodes
        var truncated = w.snapshot.truncated
        let windowFrame = w.snapshot.windowFrame

        let (screens, visibles, scales) = screenFrames()
        guard let si = ScreenScope.screenIndex(for: windowFrame, screens: screens) else {
            // No screens (headless): the window-only snapshot.
            return Result(snapshot: w.snapshot, elements: w.elements, nodes: nodes, screenScale: 2)
        }
        let screen = screens[si]

        var entries: [(item: AXItem, payload: AXUIElement)] = w.snapshot.items.compactMap { item in
            w.elements[item.id].map { (item: item, payload: $0) }
        }

        if limits.menus, let app = w.appElement, !overBudget() {
            let t = elapsed()
            var budget = limits.maxMenuItems
            entries += menuEntries(app: app, screen: screen, budget: &budget, nodes: &nodes,
                                   truncated: &truncated, overBudget: overBudget)
            kindMs["menus"] = elapsed() - t
        }
        if limits.scroll, let area = w.scrollArea, !overBudget() {
            let t = elapsed()
            entries += scrollEntries(area: area, window: windowFrame, screen: screen, nodes: &nodes)
            kindMs["scroll"] = elapsed() - t
        }
        if limits.dock, !overBudget() {
            let t = elapsed()
            entries += dockEntries(screen: screen, visible: visibles[si], max: limits.maxDockItems,
                                   nodes: &nodes, truncated: &truncated, overBudget: overBudget)
            kindMs["dock"] = elapsed() - t
        }
        if overBudget() { truncated = true }

        let (ordered, cut) = ScreenScope.assemble(entries, cap: limits.window.maxItems)
        var elements: [Int: AXUIElement] = [:]
        for e in ordered { elements[e.item.id] = e.payload }
        var snap = w.snapshot
        snap.items = ordered.map(\.item)
        snap.truncated = truncated || cut
        snap.screenFrame = screen
        snap.walkMs = elapsed()
        snap.kindMs = kindMs.mapValues { ($0 * 10).rounded() / 10 }
        return Result(snapshot: snap, elements: elements, nodes: nodes, screenScale: scales[si])
    }

    /// The backing scale of the screen whose frame (global top-left points) is `frame`;
    /// the main screen's when none matches.
    @MainActor
    public static func scale(ofScreenFrame frame: CGRect) -> Double {
        let (screens, _, scales) = screenFrames()
        if let i = screens.firstIndex(of: frame) { return scales[i] }
        return Double(NSScreen.main?.backingScaleFactor ?? 2)
    }

    /// Every screen's frame and visible frame in global top-left points, and its scale.
    @MainActor
    static func screenFrames() -> ([CGRect], [CGRect], [Double]) {
        let all = NSScreen.screens
        let primary = all.first { $0.frame.origin == .zero } ?? all.first
        let h = primary?.frame.height ?? 0
        return (all.map { ScreenScope.toTopLeft(cocoa: $0.frame, primaryHeight: h) },
                all.map { ScreenScope.toTopLeft(cocoa: $0.visibleFrame, primaryHeight: h) },
                all.map { Double($0.backingScaleFactor) })
    }

    // MARK: - menus

    private static func menuEntries(app: AXUIElement, screen: CGRect, budget: inout Int, nodes: inout Int,
                                    truncated: inout Bool, overBudget: () -> Bool)
        -> [(item: AXItem, payload: AXUIElement)] {
        var out: [(item: AXItem, payload: AXUIElement)] = []
        var openMenus: [AXUIElement] = []
        AXUIElementSetMessagingTimeout(app, 0.1)
        // Context menus and pop-up menus hang off the application element.
        for c in AX.elements(app, kAXChildrenAttribute) where AX.string(c, kAXRoleAttribute) == "AXMenu" {
            openMenus.append(c)
        }
        if let bar = AX.element(app, kAXMenuBarAttribute) {
            AXUIElementSetMessagingTimeout(bar, 0.1)
            for e in AX.elements(bar, kAXChildrenAttribute) {
                if overBudget() || budget <= 0 { truncated = true; break }
                nodes += 1
                AXUIElementSetMessagingTimeout(e, 0.1)
                guard let v = AX.many(e, menuBatch), let role = v[0] as? String, role == "AXMenuBarItem",
                      let item = menuItem(e, v, kind: .menuBar, screen: screen) else { continue }
                budget -= 1
                out.append((item: item, payload: e))
                if AX.bool(v[9]) == true { openMenus += childMenus(v[8]) }
            }
        }
        for m in openMenus {
            walkMenu(m, depth: 0, screen: screen, out: &out, budget: &budget, nodes: &nodes,
                     truncated: &truncated, overBudget: overBudget)
        }
        return out
    }

    private static func walkMenu(_ menu: AXUIElement, depth: Int, screen: CGRect,
                                 out: inout [(item: AXItem, payload: AXUIElement)], budget: inout Int,
                                 nodes: inout Int, truncated: inout Bool, overBudget: () -> Bool) {
        AXUIElementSetMessagingTimeout(menu, 0.1)
        for e in AX.elements(menu, kAXChildrenAttribute) {
            if overBudget() || budget <= 0 { truncated = true; return }
            nodes += 1
            AXUIElementSetMessagingTimeout(e, 0.1)
            guard let v = AX.many(e, menuBatch), (v[0] as? String) == "AXMenuItem" else { continue }
            if let item = menuItem(e, v, kind: .menu, screen: screen) {
                budget -= 1
                out.append((item: item, payload: e))
            }
            // An open submenu: its item is highlighted and its AXMenu child is on screen.
            if AX.bool(v[9]) == true, depth < maxMenuDepth {
                for sub in childMenus(v[8]) {
                    walkMenu(sub, depth: depth + 1, screen: screen, out: &out, budget: &budget,
                             nodes: &nodes, truncated: &truncated, overBudget: overBudget)
                }
            }
        }
    }

    private static func childMenus(_ children: CFTypeRef?) -> [AXUIElement] {
        guard let arr = children as? [AnyObject] else { return [] }
        return arr.compactMap { o -> AXUIElement? in
            guard CFGetTypeID(o) == AXUIElementGetTypeID() else { return nil }
            let c = o as! AXUIElement
            return AX.string(c, kAXRoleAttribute) == "AXMenu" ? c : nil
        }
    }

    /// A menu-bar item or menu item on `screen` with a label and a usable action; separators
    /// (no label) are skipped.
    private static func menuItem(_ e: AXUIElement, _ v: [CFTypeRef?], kind: ItemKind, screen: CGRect) -> AXItem? {
        guard let p = AX.point(v[5]), let sz = AX.size(v[6]) else { return nil }
        let f = CGRect(origin: p, size: sz)
        guard f.width >= 2, f.height >= 2, ScreenScope.onScreen(f, screen: screen) else { return nil }
        let role = v[0] as? String ?? ""
        let subrole = v[1] as? String ?? ""
        let label = AXLabel.make(e, role: role, subrole: subrole, title: v[2] as? String,
                                 description: v[3] as? String, help: v[4] as? String)
        guard !label.isEmpty else { return nil }
        let all = AX.actions(e)
        let usable = AXRoles.usableActions.filter { all.contains($0) }
        guard !usable.isEmpty else { return nil }
        let actions = usable + all.filter { !usable.contains($0) && $0.hasPrefix("AX") }
        return AXItem(id: 0, role: role, subrole: subrole, label: label, frame: f,
                      enabled: AX.bool(v[7]) ?? true, actions: Array(actions.prefix(8)), kind: kind)
    }

    // MARK: - scroll

    /// How a scroll area is scrolled by a page, best first.
    enum ScrollMethod: String {
        case byPage = "AXScrollByPage"   // AXScrollUpByPage / AXScrollDownByPage on the area
        case scrollBar = "AXScrollBar"   // AXDecrement / AXIncrement on its vertical scroll bar
        case wheel = "BPScrollWheel"     // a scroll-wheel event posted to the app over the area
    }

    static func scrollMethod(_ area: AXUIElement) -> (ScrollMethod, AXUIElement?) {
        let acts = AX.actions(area)
        if acts.contains("AXScrollUpByPage") && acts.contains("AXScrollDownByPage") { return (.byPage, nil) }
        if let bar = AX.element(area, kAXVerticalScrollBarAttribute) {
            let b = AX.actions(bar)
            if b.contains("AXIncrement") && b.contains("AXDecrement") { return (.scrollBar, bar) }
            return (.wheel, bar)
        }
        return (.wheel, nil)
    }

    /// The strips of `area` as the walk and the stale check both compute them: the area's AX
    /// frame cut to the window and the screen.
    static func strips(area: AXUIElement, window: CGRect, screen: CGRect) -> (up: CGRect, down: CGRect)? {
        guard let f = AX.frame(area) else { return nil }
        let vis = f.intersection(window).intersection(screen)
        guard !vis.isNull else { return nil }
        return ScreenScope.scrollStrips(area: vis)
    }

    private static func scrollEntries(area: AXUIElement, window: CGRect, screen: CGRect, nodes: inout Int)
        -> [(item: AXItem, payload: AXUIElement)] {
        nodes += 1
        AXUIElementSetMessagingTimeout(area, 0.1)
        guard let s = strips(area: area, window: window, screen: screen) else { return [] }
        let (method, bar) = scrollMethod(area)
        // At the top (bottom) of the content, "Scroll up" ("Scroll down") is disabled.
        var upOK = true, downOK = true
        if let bar, let n = AX.attr(bar, kAXValueAttribute) as? NSNumber {
            let v = n.doubleValue
            upOK = v > 0.001
            downOK = v < 0.999
        }
        let (upAct, downAct): (String, String) = switch method {
        case .byPage: ("AXScrollUpByPage", "AXScrollDownByPage")
        case .scrollBar: ("AXDecrement", "AXIncrement")
        case .wheel: (ScrollMethod.wheel.rawValue, ScrollMethod.wheel.rawValue)
        }
        return [
            (item: AXItem(id: 0, role: ScreenScope.scrollRole, subrole: ScreenScope.scrollUpTag, label: "Scroll up",
                          frame: s.up, enabled: upOK, actions: [upAct], kind: .scroll), payload: area),
            (item: AXItem(id: 0, role: ScreenScope.scrollRole, subrole: ScreenScope.scrollDownTag, label: "Scroll down",
                          frame: s.down, enabled: downOK, actions: [downAct], kind: .scroll), payload: area),
        ]
    }

    // MARK: - Dock

    static func dockPid() -> Int32? {
        NSRunningApplication.runningApplications(withBundleIdentifier: dockBundleID).first?.processIdentifier
    }

    /// True when a Dock item is BrainPoint's own (by bundle URL or name).
    static func isOwnDockItem(url: URL?, title: String) -> Bool {
        let own = NSRunningApplication.current
        if let u = url?.standardizedFileURL, let b = own.bundleURL?.standardizedFileURL,
           u.path == b.path { return true }
        if let name = own.localizedName, !name.isEmpty, own.bundleURL?.pathExtension == "app",
           title == name { return true }
        return false
    }

    private static func dockEntries(screen: CGRect, visible: CGRect, max: Int, nodes: inout Int,
                                    truncated: inout Bool, overBudget: () -> Bool)
        -> [(item: AXItem, payload: AXUIElement)] {
        guard let pid = dockPid() else { return [] }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.1)
        nodes += 1
        guard let list = AX.elements(app, kAXChildrenAttribute).first(where: {
                  AX.string($0, kAXRoleAttribute) == "AXList" }),
              let listFrame = AX.frame(list),
              ScreenScope.dockVisible(listFrame: listFrame, screen: screen, visibleFrame: visible)
        else { return [] }
        AXUIElementSetMessagingTimeout(list, 0.1)
        var out: [(item: AXItem, payload: AXUIElement)] = []
        for e in AX.elements(list, kAXChildrenAttribute) {
            if overBudget() || out.count >= max { truncated = true; break }
            nodes += 1
            AXUIElementSetMessagingTimeout(e, 0.1)
            guard let v = AX.many(e, dockBatch), (v[0] as? String) == ScreenScope.dockRole else { continue }
            let subrole = v[1] as? String ?? ""
            guard dockSubroles.contains(subrole), let p = AX.point(v[3]), let sz = AX.size(v[4]) else { continue }
            let f = CGRect(origin: p, size: sz)
            guard f.width >= 2, f.height >= 2, ScreenScope.onScreen(f, screen: screen) else { continue }
            let title = AXLabel.clean(v[2] as? String ?? "")
            guard !title.isEmpty, !isOwnDockItem(url: v[5] as? URL, title: title) else { continue }
            let all = AX.actions(e)
            guard all.contains("AXPress") else { continue }
            let actions = ["AXPress"] + all.filter { $0 != "AXPress" && $0.hasPrefix("AX") }
            out.append((item: AXItem(id: 0, role: ScreenScope.dockRole, subrole: subrole, label: title, frame: f,
                                     enabled: true, actions: Array(actions.prefix(8)), kind: .dock),
                        payload: e))
        }
        return out
    }
}
