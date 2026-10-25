// bp-probe: Phase 0 measurements for BrainPoint Desktop (.claude/plans/brainpoint-desktop.plan.md).
//
//   bp-probe flicker [--seconds 60] [--freq 15] [--size 200] [--refresh 60]
//       One flickering square over the front app's window, without activating this process.
//       Prints the measured refresh, dropped frames and whether the front app changed.
//   bp-probe axdump [--app BUNDLE_ID] [--out DIR] [--no-activate]
//       Walks the focused window of the app (default: the front app; brought to the front
//       first, or with --no-activate walked in the background, launched with `open -g` if
//       needed, so the user's focus is never taken) and writes
//       <out>/<bundle id>.json (default out: <repo>/data/axdumps, gitignored).
//       Also walks the screen scope (window, open menus, scroll strips, menu bar, Dock on
//       the window's screen) as a scan would, and prints its counts by kind and walk times.
//   bp-probe press --app com.apple.TextEdit --label LABEL
//       Smoke test of ElementRegistry.press: presses the first item whose label matches.
//       Only for TextEdit with a new untitled document in front.
import AppKit
import ApplicationServices
import BrainPointCore
import BrainPointPlatform

setvbuf(stdout, nil, _IOLBF, 0)

func usage() -> Never {
    print("""
    usage: bp-probe flicker [--seconds 60] [--freq 15] [--size 200] [--refresh 60]
           bp-probe axdump [--app BUNDLE_ID] [--out DIR] [--no-activate]
           bp-probe press --app com.apple.TextEdit --label LABEL
    """)
    exit(2)
}

let args = Array(CommandLine.arguments.dropFirst(2))
guard CommandLine.arguments.count >= 2 else { usage() }
let command = CommandLine.arguments[1]

/// A mutable list a notification block (main queue) appends to.
final class Log: @unchecked Sendable { var lines: [String] = [] }

func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}
func flag(_ name: String) -> Bool { args.contains(name) }
func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data("bp-probe: \(msg)\n".utf8))
    exit(1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // no Dock icon; this process never activates itself

func spinRunLoop(_ seconds: Double) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end { RunLoop.main.run(mode: .default, before: min(end, Date().addingTimeInterval(0.05))) }
}

func frontApp() -> NSRunningApplication? { NSWorkspace.shared.frontmostApplication }

/// Brings the app to the front (launching it if needed) and waits for it, up to 5 s.
func bringToFront(bundleID: String) -> NSRunningApplication {
    if frontApp()?.bundleIdentifier == bundleID, let a = frontApp() { return a }
    if let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first {
        running.activate()
    }
    if frontApp()?.bundleIdentifier != bundleID {
        // Cooperative activation may ignore activate() from a background process; `open` works.
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-b", bundleID]
        try? p.run()
        p.waitUntilExit()
    }
    let end = Date().addingTimeInterval(5)
    while Date() < end {
        if let a = frontApp(), a.bundleIdentifier == bundleID { spinRunLoop(0.4); return a }
        spinRunLoop(0.1)
    }
    fail("\(bundleID) did not come to the front")
}

/// The running app, launched hidden in the background (`open -g`) if needed; never activated.
func launchInBackground(bundleID: String) -> NSRunningApplication {
    if let a = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first { return a }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    p.arguments = ["-g", "-b", bundleID]
    try? p.run()
    p.waitUntilExit()
    let end = Date().addingTimeInterval(8)
    while Date() < end {
        if let a = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first,
           a.isFinishedLaunching {
            spinRunLoop(2.0)   // let it open its first window
            return a
        }
        spinRunLoop(0.2)
    }
    fail("\(bundleID) did not launch")
}

func repoRoot() -> URL {
    var dir = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    while dir.path != "/" {
        if FileManager.default.fileExists(atPath: dir.appendingPathComponent(".git").path) { return dir }
        dir.deleteLastPathComponent()
    }
    return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
}

func rectJSON(_ r: CGRect) -> [String: Double] {
    ["x": Double(r.minX), "y": Double(r.minY), "w": Double(r.width), "h": Double(r.height)]
}

func r1(_ v: Double) -> Double { (v * 10).rounded() / 10 }

// MARK: - flicker

@MainActor
func runFlicker() {
    let seconds = Double(option("--seconds") ?? "60") ?? 60
    let freq = Double(option("--freq") ?? "15") ?? 15
    let size = Double(option("--size") ?? "200") ?? 200
    let refresh = Double(option("--refresh") ?? "60") ?? 60
    let before = frontApp()
    let beforeName = before.map { "\($0.localizedName ?? "?") (\($0.bundleIdentifier ?? "?"))" } ?? "none"
    // Center of the front app's focused window, else of the primary screen (global top-left).
    var center = CGPoint(x: (NSScreen.screens.first?.frame.midX ?? 500),
                         y: (NSScreen.screens.first?.frame.midY ?? 400))
    if let b = before, Permissions.accessibilityTrusted,
       let snap = AXWalker.snapshot(pid: b.processIdentifier, limits: WalkLimits(maxNodes: 1, maxMs: 50)) {
        center = CGPoint(x: snap.windowFrame.midX, y: snap.windowFrame.midY)
    }
    let rect = CGRect(x: center.x - size / 2, y: center.y - size / 2, width: size, height: size)
    print("flicker: \(freq) Hz, \(Int(size)) pt square at \(rectJSON(rect)), \(Int(seconds)) s, front app \(beforeName)")
    print("screens: " + NSScreen.screens.map { "\(Int($0.frame.width))x\(Int($0.frame.height)) @\($0.maximumFramesPerSecond) Hz" }.joined(separator: ", "))

    let overlay = OverlayController()
    var onsetUnix: Double?
    var reports: [(Double, Int)] = []
    overlay.onOnset = { id, t in
        onsetUnix = t
        print(String(format: "onset layout %d at unix %.4f (now %.4f)", id, t, Date().timeIntervalSince1970))
    }
    overlay.onFrames = { hz, dropped in
        reports.append((hz, dropped))
        print(String(format: "frames: %.3f Hz, dropped %d (total)", hz, dropped))
    }
    overlay.setStrip(text: "bp-probe flicker test: \(Int(freq)) Hz", color: .systemOrange)
    overlay.show(targets: [OverlayTarget(slot: "b1", label: "probe \(Int(freq)) Hz", kind: "button",
                                         freq: freq, phase: 0, rect: rect)],
                 flicker: true, refreshHz: refresh, layoutID: 1)

    let frontChanges = Log()
    let started = Date()
    var weWereActive = false
    let token = NSWorkspace.shared.notificationCenter.addObserver(
        forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { note in
        let a = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
        let at = String(format: "%.1f s", Date().timeIntervalSince(started))
        frontChanges.lines.append("\(a?.bundleIdentifier ?? "?") at \(at)")
    }
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        spinRunLoop(0.25)
        if NSApp.isActive { weWereActive = true }
    }
    NSWorkspace.shared.notificationCenter.removeObserver(token)
    let frames = overlay.flickerFrames
    let dropped = overlay.droppedTotal
    let hz = overlay.measuredHz
    overlay.hide()
    overlay.setStrip(text: "", color: .clear)
    spinRunLoop(0.1)
    let after = frontApp()
    let pct = frames > 0 ? 100.0 * Double(dropped) / Double(frames + dropped) : 0
    print("RESULT flicker: frames \(frames), dropped \(dropped) (\(String(format: "%.3f", pct)) %), " +
          "measured \(hz.map { String(format: "%.3f", $0) } ?? "n/a") Hz, " +
          "onset \(onsetUnix != nil ? "yes" : "no"), " +
          "front before \(before?.bundleIdentifier ?? "none") after \(after?.bundleIdentifier ?? "none"), " +
          "front changed \(before?.processIdentifier != after?.processIdentifier || !frontChanges.lines.isEmpty) \(frontChanges.lines), " +
          "bp-probe active \(weWereActive)")
    _ = reports
}

// MARK: - axdump

@MainActor
func runAxdump() {
    guard Permissions.accessibilityTrusted else { fail("Accessibility is not trusted for this process") }
    let target: NSRunningApplication
    if let bid = option("--app") {
        target = flag("--no-activate") ? launchInBackground(bundleID: bid) : bringToFront(bundleID: bid)
    } else {
        guard let f = frontApp() else { fail("no front app") }
        target = f
    }
    let pid = target.processIdentifier
    let bid = target.bundleIdentifier ?? "pid\(pid)"
    let chromium = AXWalker.isChromiumBased(pid: pid)
    if chromium {
        AXWalker.enableAppAccessibility(pid: pid)
        spinRunLoop(1.5)   // Chromium builds its tree asynchronously
    }
    // A first walk warms up the app's AX caches; report the second.
    _ = AXWalker.snapshot(pid: pid)
    guard let walk = AXWalker.walk(pid: pid) else { fail("\(bid): no usable window") }
    let snap = walk.snapshot
    let unlimitedWalk = AXWalker.walk(pid: pid, limits: WalkLimits(maxNodes: 100_000, maxDepth: 200,
                                                                    maxMs: 10_000, maxItems: 100_000))
    let unlimited = unlimitedWalk?.snapshot
    let nodes = (visited: walk.nodes, total: unlimitedWalk?.nodes ?? 0)
    let rawTree = nodeCount(pid: pid)
    let pressable = snap.items.filter { $0.enabled && !$0.actions.isEmpty }.count
    let byRole = Dictionary(grouping: snap.items, by: \.role).mapValues(\.count)
        .sorted { $0.value > $1.value }.map { "\($0.key) \($0.value)" }.joined(separator: ", ")
    let json: [String: Any] = [
        "app": ["bundle_id": bid, "name": target.localizedName ?? "", "pid": Int(pid)],
        "window": ["title": snap.windowTitle, "frame": rectJSON(snap.windowFrame)],
        "items": snap.items.map { i in
            ["id": i.id, "role": i.role, "subrole": i.subrole, "label": i.label,
             "frame": rectJSON(i.frame), "enabled": i.enabled, "actions": i.actions] as [String: Any]
        },
        "walk_ms": r1(snap.walkMs), "truncated": snap.truncated, "n_nodes": nodes.visited,
        "chromium": chromium,
        "unlimited": ["items": unlimited?.items.count ?? 0, "walk_ms": r1(unlimited?.walkMs ?? 0),
                      "n_nodes": nodes.total],
        "raw_tree_nodes": rawTree,
        "screen_scope": screenScopeDump(pid: pid),   // ScreenDump.swift: what a scan sends
    ]
    let outDir = option("--out").map { URL(fileURLWithPath: $0) }
        ?? repoRoot().appendingPathComponent("data/axdumps")
    try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
    let file = outDir.appendingPathComponent("\(bid).json")
    do {
        let data = try JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: file)
    } catch { fail("could not write \(file.path): \(error)") }
    let labelled = snap.items.filter { !$0.label.isEmpty }.count
    print("RESULT axdump \(bid): nodes \(nodes.visited), items \(snap.items.count), pressable \(pressable), " +
          "labelled \(labelled), walk \(r1(snap.walkMs)) ms, truncated \(snap.truncated) | unlimited: " +
          "nodes \(nodes.total), items \(unlimited?.items.count ?? 0), \(r1(unlimited?.walkMs ?? 0)) ms | raw tree \(rawTree) nodes")
    print("  window \"\(snap.windowTitle)\" \(rectJSON(snap.windowFrame)); roles: \(byRole)")
    print("  wrote \(file.path)")
}

/// Every node under the focused window (children only, no pruning), for comparison.
func nodeCount(pid: Int32) -> Int {
    let app = AXUIElementCreateApplication(pid)
    var w: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &w) == .success
            || AXUIElementCopyAttributeValue(app, kAXMainWindowAttribute as CFString, &w) == .success,
          let win = w else { return 0 }
    var queue: [AXUIElement] = [win as! AXUIElement]
    var head = 0
    while head < queue.count && head < 100_000 {
        var c: CFTypeRef?
        if AXUIElementCopyAttributeValue(queue[head], kAXChildrenAttribute as CFString, &c) == .success,
           let arr = c as? [AnyObject] {
            for o in arr where CFGetTypeID(o) == AXUIElementGetTypeID() { queue.append(o as! AXUIElement) }
        }
        head += 1
    }
    return head
}

// MARK: - axloop

/// Repeated walks of a background app, optionally calling enableAppAccessibility before each,
/// to check that walks stay fast and complete. Never activates or presses anything.
@MainActor
func runAxloop() {
    guard let bid = option("--app") else { usage() }
    let n = Int(option("--n") ?? "5") ?? 5
    let enable = flag("--enable")
    let a = launchInBackground(bundleID: bid)
    let pid = a.processIdentifier
    let watcher = FocusWatcher { e in print("  focus event: \(e)") }
    watcher.start()
    for i in 0..<n {
        if enable {
            let t = Date()
            AXWalker.enableAppAccessibility(pid: pid)
            print(String(format: "  enable took %.1f ms", Date().timeIntervalSince(t) * 1000))
        }
        spinRunLoop(0.3)
        guard let w = AXWalker.walk(pid: pid) else { print("walk \(i): no window"); continue }
        print("RESULT walk \(i): items \(w.snapshot.items.count), nodes \(w.nodes), " +
              "\(r1(w.snapshot.walkMs)) ms, truncated \(w.snapshot.truncated)")
    }
    spinRunLoop(0.5)
    watcher.stop()
}

// MARK: - press

@MainActor
func runPress() {
    guard let bid = option("--app"), let label = option("--label") else { usage() }
    guard bid == "com.apple.TextEdit" else { fail("press is limited to com.apple.TextEdit in Phase 0") }
    let target = bringToFront(bundleID: bid)
    guard let (snap, elements) = AXWalker.snapshotWithElements(pid: target.processIdentifier) else {
        fail("no usable window")
    }
    guard snap.windowTitle.hasPrefix("Untitled") else {
        fail("front TextEdit window is \"\(snap.windowTitle)\", not a new untitled document")
    }
    guard let item = snap.items.first(where: { $0.label == label })
            ?? snap.items.first(where: { $0.label.localizedCaseInsensitiveContains(label) }) else {
        print("labels: " + snap.items.map { "\($0.role):\($0.label)" }.joined(separator: " | "))
        fail("no item labelled \(label)")
    }
    let registry = ElementRegistry()
    registry.replace(with: snap, elements: elements)
    let r = registry.press(id: item.id)
    print("RESULT press \(item.role) \"\(item.label)\" (id \(item.id)): ok \(r.ok), method \(r.method), \(r.detail)")
    // The stale check: a fake snapshot whose frame is 10 pt off must be refused.
    var moved = snap
    if let i = moved.items.firstIndex(where: { $0.id == item.id }) { moved.items[i].frame.origin.x += 10 }
    registry.replace(with: moved, elements: elements)
    let stale = registry.press(id: item.id)
    print("RESULT stale check (frame moved 10 pt): ok \(stale.ok), \(stale.detail)")
}

// MARK: - capture

/// bp-probe capture [--rect x,y,w,h] [--redact x,y,w,h] [--out file.jpg] [--request]
/// One WindowCapture.capture of the rect (global points; default: the front app's frontmost
/// window), timed cold and warm. Prints the permission state, image size, JPEG bytes and ms.
/// Writes the image only to --out. Prompts for Screen Recording only with --request.
@MainActor
func runCapture() {
    func parseRect(_ s: String) -> CGRect? {
        let v = s.split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
        guard v.count == 4, v[2] > 0, v[3] > 0 else { return nil }
        return CGRect(x: v[0], y: v[1], width: v[2], height: v[3])
    }
    /// The front app's frontmost normal window (layer 0), else the frontmost normal window of
    /// any app (the front app can be loginwindow, which has none), global top-left points.
    func frontWindowFrame() -> (CGRect, String)? {
        guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                    kCGNullWindowID) as? [[String: Any]] else { return nil }
        let normal: [(Int32, String, CGRect)] = info.compactMap { w in
            guard (w[kCGWindowLayer as String] as? Int) == 0,
                  let pid = w[kCGWindowOwnerPID as String] as? Int32,
                  let b = w[kCGWindowBounds as String] as? NSDictionary,
                  let r = CGRect(dictionaryRepresentation: b), r.width > 100, r.height > 100 else { return nil }
            return (pid, w[kCGWindowOwnerName as String] as? String ?? "pid\(pid)", r)
        }
        let pid = frontApp()?.processIdentifier
        guard let hit = normal.first(where: { $0.0 == pid }) ?? normal.first else { return nil }
        return (hit.2, hit.1)
    }
    final class Box: @unchecked Sendable { var done = false; var value: CapturedImage?; var ms = 0.0 }
    func timedCapture(_ rect: CGRect, _ redact: [CGRect]) -> (CapturedImage?, Double) {
        let box = Box()
        Task.detached {
            let t = Date()
            let img = await WindowCapture.capture(rect: rect, redact: redact)
            DispatchQueue.main.async { box.value = img; box.ms = Date().timeIntervalSince(t) * 1000; box.done = true }
        }
        let end = Date().addingTimeInterval(15)
        while !box.done && Date() < end { spinRunLoop(0.01) }
        return (box.value, box.ms)
    }

    print("screen recording permission: \(ScreenCapturePermission.granted ? "granted" : "not granted")")
    if !ScreenCapturePermission.granted {
        if flag("--request") {
            ScreenCapturePermission.request()
            print("requested; granted now: \(ScreenCapturePermission.granted)")
        }
        if !ScreenCapturePermission.granted {
            print("Grant it in System Settings > Privacy & Security > Screen & System Audio Recording " +
                  "for the app running bp-probe (your terminal), then quit and reopen that app. " +
                  "`bp-probe capture --request` shows the system prompt.")
            exit(1)
        }
    }
    let rect: CGRect
    if let s = option("--rect") {
        guard let r = parseRect(s) else { fail("--rect wants x,y,w,h with w, h > 0") }
        rect = r
        print("rect \(rectJSON(r)) (from --rect)")
    } else {
        guard let (r, owner) = frontWindowFrame() else { fail("no front window found; pass --rect x,y,w,h") }
        rect = r
        print("rect \(rectJSON(r)) (front window of \(owner))")
    }
    let redact = option("--redact").flatMap(parseRect).map { [$0] } ?? []

    let (cold, coldMs) = timedCapture(rect, redact)
    guard let img = cold else {
        let session = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
        let locked = (session["CGSSessionScreenIsLocked"] as? Bool) ?? false
        let asleep = CGDisplayIsAsleep(CGMainDisplayID()) != 0
        fail("capture returned nil after \(r1(coldMs)) ms (screen locked \(locked), main display asleep \(asleep); " +
             "ScreenCaptureKit lists no displays while locked or asleep)")
    }
    let (_, warmMs) = timedCapture(rect, redact)
    guard let jpeg = Data(base64Encoded: img.jpegBase64) else { fail("bad base64") }
    var decoded = "?"
    if let src = CGImageSourceCreateWithData(jpeg as CFData, nil),
       let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [String: Any] {
        decoded = "\(props[kCGImagePropertyPixelWidth as String] ?? "?")x\(props[kCGImagePropertyPixelHeight as String] ?? "?")"
    }
    print("RESULT capture: \(img.width)x\(img.height) px (decoded \(decoded)), jpeg \(jpeg.count) bytes, " +
          "base64 \(img.jpegBase64.utf8.count) bytes, cold \(r1(coldMs)) ms, warm \(r1(warmMs)) ms, " +
          "captured \(rectJSON(img.rect)), redact \(redact.count)")
    if let out = option("--out") {
        do { try jpeg.write(to: URL(fileURLWithPath: out)); print("  wrote \(out)") }
        catch { fail("could not write \(out): \(error)") }
    }
}

switch command {
case "capture": runCapture()
case "flicker": runFlicker()
case "axdump": runAxdump()
case "press": runPress()
case "axloop": runAxloop()
default: usage()
}
exit(0)
