import AppKit
import ApplicationServices
import BrainPointCore

/// Bounds for one walk of a window's accessibility tree.
public struct WalkLimits: Sendable {
    public var maxNodes = 1500
    public var maxDepth = 40
    public var maxMs = 150.0
    public var maxItems = 200
    public init(maxNodes: Int = 1500, maxDepth: Int = 40, maxMs: Double = 150.0, maxItems: Int = 200) {
        self.maxNodes = maxNodes; self.maxDepth = maxDepth; self.maxMs = maxMs; self.maxItems = maxItems
    }
}

/// Walks the focused window of an app and collects its pressable elements.
///
/// Breadth-first from the focused window (falling back to the main window, then the first
/// window), bounded by nodes, depth, time and items; any bound reached sets `truncated`. Each
/// node costs one batched IPC call (role, subrole, title, description, help, position, size,
/// enabled, children); action names are read only for candidate roles. Elements are kept when
/// they are visible: non-zero size, inside the window and inside every enclosing scroll area
/// (a subtree whose frame is outside that clip is not entered). Items are numbered top to
/// bottom, left to right. The value of a text input is never read (see `AXLabel`).
public enum AXWalker {
    private static let batch: [String] = [
        kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
        kAXHelpAttribute, kAXPositionAttribute, kAXSizeAttribute, kAXEnabledAttribute,
        kAXChildrenAttribute, "AXHidden",
    ]
    /// Containers whose visible rows are much smaller than all their children.
    private static let rowContainers: Set<String> = ["AXTable", "AXOutline"]
    private static let clipRoles: Set<String> = ["AXScrollArea"]

    public static func snapshot(pid: Int32, limits: WalkLimits = .init()) -> WindowSnapshot? {
        snapshotWithElements(pid: pid, limits: limits)?.0
    }

    public static func snapshotWithElements(pid: Int32, limits: WalkLimits = .init())
        -> (WindowSnapshot, [Int: AXUIElement])? {
        walk(pid: pid, limits: limits).map { ($0.snapshot, $0.elements) }
    }

    /// One walk with its node count (for bp-probe and logs).
    public struct WalkResult {
        public var snapshot: WindowSnapshot
        public var elements: [Int: AXUIElement]
        /// Nodes visited (one batched attribute read each).
        public var nodes: Int
        /// The largest visible AXScrollArea of the window and its visible frame (cut to the
        /// window and every enclosing scroll area), for the synthetic scroll strips.
        public var scrollArea: AXUIElement?
        public var scrollAreaFrame: CGRect = .null
        /// The app element and the walked window (for the screen-scope walk).
        public var appElement: AXUIElement?
        public var windowElement: AXUIElement?
    }

    public static func walk(pid: Int32, limits: WalkLimits = .init()) -> WalkResult? {
        let start = DispatchTime.now().uptimeNanoseconds
        func elapsedMs() -> Double { Double(DispatchTime.now().uptimeNanoseconds - start) / 1e6 }

        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        guard let window = AX.element(app, kAXFocusedWindowAttribute)
                ?? AX.element(app, kAXMainWindowAttribute)
                ?? AX.elements(app, kAXWindowsAttribute).first,
              let wframe = AX.frame(window), wframe.width > 0, wframe.height > 0
        else { return nil }
        let running = NSRunningApplication(processIdentifier: pid)
        let title = AX.string(window, kAXTitleAttribute) ?? ""

        struct Node { let e: AXUIElement; let depth: Int; let clip: CGRect }
        struct Found { let e: AXUIElement; var item: AXItem }
        var queue: [Node] = [Node(e: window, depth: 0, clip: wframe)]
        var head = 0
        var nodes = 0
        var truncated = false
        var found: [Found] = []
        var scrollArea: (AXUIElement, CGRect)? = nil

        while head < queue.count {
            if nodes >= limits.maxNodes || elapsedMs() > limits.maxMs || found.count >= limits.maxItems {
                truncated = true
                break
            }
            let node = queue[head]
            head += 1
            nodes += 1
            boundTimeout(node.e, elapsedMs: elapsedMs(), limits: limits)
            guard let v = AX.many(node.e, batch) else { continue }
            let role = v[0] as? String ?? ""
            let subrole = v[1] as? String ?? ""
            if AX.bool(v[9]) == true { continue }   // AXHidden
            let pos = AX.point(v[5]), size = AX.size(v[6])
            var frame: CGRect? = nil
            if let pos, let size { frame = CGRect(origin: pos, size: size) }
            var clip = node.clip
            if let f = frame, f.width > 0, f.height > 0 {
                // A subtree outside the visible area (scrolled away) is skipped entirely.
                if !f.intersects(node.clip) && node.depth > 0 { continue }
                if clipRoles.contains(role) {
                    clip = clip.intersection(f)
                    if !clip.isNull, clip.width * clip.height > (scrollArea.map { $0.1.width * $0.1.height } ?? 0) {
                        scrollArea = (node.e, clip)
                    }
                }
            }

            if node.depth > 0, let f = frame, isVisible(f, clip: clip),
               let item = candidate(node.e, role: role, subrole: subrole, frame: f, values: v) {
                found.append(Found(e: node.e, item: item))
            }

            // Web content (WebKit, Chromium): ask the web area for its visible controls and
            // links in one call instead of walking thousands of text and group nodes.
            if role == "AXWebArea", let hits = webControls(node.e, limit: 2 * limits.maxItems) {
                for h in hits {
                    if found.count >= limits.maxItems || elapsedMs() > limits.maxMs {
                        truncated = true
                        break
                    }
                    nodes += 1
                    boundTimeout(h, elapsedMs: elapsedMs(), limits: limits)
                    guard let hv = AX.many(h, batch), AX.bool(hv[9]) != true,
                          let p = AX.point(hv[5]), let sz = AX.size(hv[6]) else { continue }
                    let f = CGRect(origin: p, size: sz)
                    guard isVisible(f, clip: clip),
                          let item = candidate(h, role: hv[0] as? String ?? "", subrole: hv[1] as? String ?? "",
                                               frame: f, values: hv) else { continue }
                    found.append(Found(e: h, item: item))
                }
                continue
            }

            if node.depth >= limits.maxDepth {
                if !(v[8] as? [AnyObject] ?? []).isEmpty { truncated = true }
                continue
            }
            var children: [AXUIElement] = []
            if rowContainers.contains(role) {
                children = AX.elements(node.e, kAXVisibleRowsAttribute)
                // Headers and other non-row children (column headers, scroll bars).
                let rows = Set(children.map { CFHash($0) })
                let all = (v[8] as? [AnyObject]) ?? []
                if all.count <= 200 {
                    for o in all where CFGetTypeID(o) == AXUIElementGetTypeID() {
                        let c = o as! AXUIElement
                        if !rows.contains(CFHash(c)) { children.append(c) }
                    }
                }
            } else if let arr = v[8] as? [AnyObject] {
                children = arr.compactMap { o in
                    CFGetTypeID(o) == AXUIElementGetTypeID() ? (o as! AXUIElement) : nil
                }
            }
            for c in children { queue.append(Node(e: c, depth: node.depth + 1, clip: clip)) }
        }

        // Reading order: rows of 8 pt, then left to right.
        found.sort { a, b in
            let ya = (a.item.frame.minY / 8).rounded(.down), yb = (b.item.frame.minY / 8).rounded(.down)
            return ya != yb ? ya < yb : a.item.frame.minX < b.item.frame.minX
        }
        var items: [AXItem] = []
        var elements: [Int: AXUIElement] = [:]
        for (i, f) in found.prefix(limits.maxItems).enumerated() {
            var item = f.item
            item.id = i
            items.append(item)
            elements[i] = f.e
        }
        let snap = WindowSnapshot(
            appName: running?.localizedName ?? "", bundleID: running?.bundleIdentifier ?? "",
            pid: pid, windowTitle: AXLabel.clean(title), windowFrame: wframe, items: items,
            walkMs: elapsedMs(), truncated: truncated)
        return WalkResult(snapshot: snap, elements: elements, nodes: nodes,
                          scrollArea: scrollArea?.0, scrollAreaFrame: scrollArea?.1 ?? .null,
                          appElement: app, windowElement: window)
    }

    /// Makes a Chromium or Electron app build its accessibility tree (it is nearly empty
    /// otherwise) by setting AXManualAccessibility. Does nothing at all for any other app:
    /// Safari, WebKit and native apps are never written to. Only a Chromium app that refuses
    /// AXManualAccessibility gets AXEnhancedUserInterface, which works too but has side
    /// effects: window moves and resizes stop animating, window managers (Rectangle, Magnet)
    /// can misplace its windows, and some apps run slower while it is on. Setting either can
    /// make the app post window notifications; FocusWatcher ignores its window events for
    /// `EnableLog.quietSeconds` after this call. Already-enabled apps are left alone, so
    /// calling it again is cheap. The tree is built asynchronously: walk again after about a
    /// second.
    public static func enableAppAccessibility(pid: Int32) {
        guard isChromiumBased(pid: pid) else { return }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        if AX.bool(AX.attr(app, "AXManualAccessibility")) == true { return }
        EnableLog.shared.note(pid)
        let err = AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        if err == .success { return }
        if AX.bool(AX.attr(app, "AXEnhancedUserInterface")) == true { return }
        _ = AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
    }

    /// True for Electron apps and Chromium browsers (by the frameworks in the app bundle).
    public static func isChromiumBased(pid: Int32) -> Bool {
        guard let url = NSRunningApplication(processIdentifier: pid)?.bundleURL else { return false }
        let fw = url.appendingPathComponent("Contents/Frameworks")
        let names = (try? FileManager.default.contentsOfDirectory(atPath: fw.path)) ?? []
        return names.contains { n in
            n == "Electron Framework.framework" || n.contains("Chromium") || n.hasSuffix("Chrome Framework.framework")
                || n == "Microsoft Edge Framework.framework" || n == "Brave Browser Framework.framework"
        }
    }

    // MARK: - private

    /// Every AX call waits at most what is left of the walk's budget (between 50 and 250 ms),
    /// so a busy app cannot hold one call for the system's default 6 s.
    private static func boundTimeout(_ e: AXUIElement, elapsedMs: Double, limits: WalkLimits) {
        let left = (limits.maxMs - elapsedMs) / 1000
        AXUIElementSetMessagingTimeout(e, Float(min(0.25, max(0.05, left))))
    }

    /// The visible controls and links under a web area, in document order, through the
    /// AXUIElementsForSearchPredicate query VoiceOver uses; nil when the app does not support it.
    private static func webControls(_ area: AXUIElement, limit: Int) -> [AXUIElement]? {
        let params: [String: Any] = [
            "AXSearchKey": ["AXControlSearchKey", "AXLinkSearchKey"],
            "AXResultsLimit": limit,
            "AXDirection": "AXDirectionNext",
            "AXVisibleOnly": true,
            "AXImmediateDescendantsOnly": false,
        ]
        AXUIElementSetMessagingTimeout(area, 0.3)
        var out: CFTypeRef?
        let err = AXUIElementCopyParameterizedAttributeValue(
            area, "AXUIElementsForSearchPredicate" as CFString, params as CFDictionary, &out)
        guard err == .success, let arr = out as? [AnyObject] else { return nil }
        return arr.compactMap { o in
            CFGetTypeID(o) == AXUIElementGetTypeID() ? (o as! AXUIElement) : nil
        }
    }

    private static func isVisible(_ f: CGRect, clip: CGRect) -> Bool {
        guard f.width >= 2, f.height >= 2 else { return false }
        let vis = f.intersection(clip)
        return !vis.isNull && vis.width >= 2 && vis.height >= 2
    }

    private static func candidate(_ e: AXUIElement, role: String, subrole: String, frame: CGRect,
                                  values v: [CFTypeRef?]) -> AXItem? {
        if AXRoles.isSecure(role: role, subrole: subrole) { return nil }
        let isText = AXRoles.focusableText.contains(role)
        guard isText || AXRoles.control.contains(role) || AXRoles.pressOnly.contains(role) else { return nil }
        let all = AX.actions(e)
        let usable = AXRoles.usableActions.filter { all.contains($0) }
        if AXRoles.pressOnly.contains(role) && !all.contains("AXPress") { return nil }
        if isText {
            var settable: DarwinBoolean = false
            guard AXUIElementIsAttributeSettable(e, kAXFocusedAttribute as CFString, &settable) == .success,
                  settable.boolValue else { return nil }
        } else if usable.isEmpty {
            return nil
        }
        let label = AXLabel.make(e, role: role, subrole: subrole, title: v[2] as? String,
                                 description: v[3] as? String, help: v[4] as? String)
        // Known actions first, then the rest, at most 8 (the protocol's cap).
        // Custom actions ("Name:...\nTarget:...") are left out.
        let ordered = usable + all.filter { !usable.contains($0) && $0.hasPrefix("AX") }
        let actions = ordered.prefix(8)
        return AXItem(id: 0, role: role, subrole: subrole, label: label, frame: frame,
                      enabled: AX.bool(v[7]) ?? true, actions: Array(actions))
    }
}

/// When this process last enabled accessibility on each app, so FocusWatcher can ignore the
/// window notifications our own attribute write causes.
public final class EnableLog: @unchecked Sendable {
    public static let shared = EnableLog()
    public static let quietSeconds = 1.5
    private let lock = NSLock()
    private var at: [Int32: Double] = [:]

    func note(_ pid: Int32) {
        lock.lock(); defer { lock.unlock() }
        at[pid] = ProcessInfo.processInfo.systemUptime
    }

    /// True within `quietSeconds` of our last enable call for this pid.
    public func isQuiet(_ pid: Int32) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let t = at[pid] else { return false }
        return ProcessInfo.processInfo.systemUptime - t < Self.quietSeconds
    }
}
