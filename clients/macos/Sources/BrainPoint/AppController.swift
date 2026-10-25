import AppKit
import BrainPointCore
import BrainPointPlatform
import Foundation

/// Wires the pure AppModel to the world: the server connection, the Platform pieces (focus,
/// AX walk and presses, overlay, safety, Esc), the menu bar, the log, speech and notifications.
/// Every input becomes an AppEvent; every AppEffect is carried out here and nowhere else.
@MainActor
final class AppController: NSObject, NSApplicationDelegate {
    static let version = "0.1.0"

    private let options = LaunchOptions.parse()
    private var settings = Settings.load()
    private var model: AppModel
    private let log = EventLog()
    private var conn: Connection!
    private let overlay = OverlayController()
    private let registry = ElementRegistry()
    private var focus: FocusWatcher?
    private var safety: SafetyMonitor?
    private var escape: EscapeMonitor?
    private let statusMenu = StatusMenu()
    private let settingsWindow = SettingsWindowController()
    private let speaker = Speaker()
    private let notifier = Notifier()
    private var tickTimer: Timer?
    private var focusSettle: Timer?
    private var scanTimer: Timer?
    private var retryIn: Double?
    private var accessibilityEnabled: Set<Int32> = []
    private let instance = UUID().uuidString
    private let ownBundleID = Bundle.main.bundleIdentifier ?? "ai.brainsync.brainpoint"

    override init() {
        var s = Settings.load()
        if let v = options.server { s.serverURL = v }
        if let v = options.token { s.token = v }
        if options.allowFakeControl { s.allowFakeControl = true }
        settings = s
        model = AppModel(ownBundleID: Bundle.main.bundleIdentifier ?? "ai.brainsync.brainpoint",
                         denyList: s.denyList, allowFakeControl: s.allowFakeControl)
        super.init()
    }

    private var now: Double { ProcessInfo.processInfo.systemUptime }

    // MARK: launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        log.echo = options.verbose
        log.write("start", ["version": AppController.version, "pid": Int(getpid()), "bundle": ownBundleID,
                            "server": settings.serverURL, "allow_fake_control": settings.allowFakeControl,
                            "deny": settings.denyList, "args": options.summary,
                            "notifications": notifier.available, "log": log.currentFile.path])
        wireMenu()
        wireOverlay()
        let url = Settings.validURL(settings.serverURL) ?? URL(string: Settings.defaultServer)!
        if Settings.validURL(settings.serverURL) == nil {
            note("the server URL \(settings.serverURL) is not a ws:// URL; using \(Settings.defaultServer)")
        }
        conn = Connection(url: url)
        speaker.serverURL = url
        speaker.token = settings.token.isEmpty ? nil : settings.token
        speaker.log = { [weak self] k, f in self?.log.write(k, f) }
        wireConnection()

        // Accessibility first (the system prompt), then the photosensitivity warning.
        let trusted = Permissions.accessibilityTrusted
        dispatch(.axTrusted(trusted))
        if !trusted {
            note("Accessibility permission missing: asking (waiting for permission)")
            Permissions.promptAccessibility()
        }
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated { self?.askWarning() }
        }
    }

    private func askWarning() {
        if options.acceptWarning {
            note("photosensitivity warning accepted by --accept-warning (developer flag)")
            warningAccepted()
            return
        }
        let a = NSAlert()
        a.alertStyle = .critical
        a.messageText = "Flicker warning"
        a.informativeText = """
            BrainPoint draws flickering squares over the app you control: three white squares \
            that switch between white and black up to 30 times a second (the c-VEP code), or \
            12 to 20 Hz with a frequency server. \
            Flicker between 15 and 25 Hz is the band most likely to trigger seizures in people \
            with photosensitive epilepsy. Do not use BrainPoint if you or anyone watching has \
            epilepsy or is sensitive to flicker.

            Press Esc at any time to stop the flicker at once.
            """
        a.addButton(withTitle: "Accept")
        a.addButton(withTitle: "Quit")
        NSApp.activate(ignoringOtherApps: true)
        if a.runModal() == .alertFirstButtonReturn {
            log.write("warning", ["accepted": true])
            warningAccepted()
        } else {
            log.write("warning", ["accepted": false])
            quit()
        }
    }

    private func warningAccepted() {
        dispatch(.warningAccepted)
        startMonitors()
        dispatch(.measuredRefresh(screenRefresh()))
        conn.open()
    }

    private func startMonitors() {
        let f = FocusWatcher { [weak self] e in self?.focusEvent(e) }
        focus = f
        f.start()
        let s = SafetyMonitor { [weak self] reason in self?.dispatch(.safety(reason)) }
        safety = s
        s.start()
        let e = EscapeMonitor { [weak self] in self?.dispatch(.pause("Esc")) }
        escape = e
        e.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        overlay.hide()
        conn?.close(bye: true)
        log.write("quit")
        log.close()
    }

    private func quit() {
        overlay.hide()
        conn?.close(bye: true)
        NSApp.terminate(nil)
    }

    // MARK: wiring

    private func wireMenu() {
        statusMenu.onPause = { [weak self] in self?.dispatch(.pause("menu")) }
        statusMenu.onResume = { [weak self] in self?.dispatch(.resume) }
        statusMenu.onSettings = { [weak self] in
            guard let self else { return }
            self.settingsWindow.show(self.settings)
        }
        statusMenu.onGrantAX = { Permissions.promptAccessibility() }
        statusMenu.onReconnect = { [weak self] in
            guard let self else { return }
            self.conn.reconnect(to: self.conn.url)
        }
        statusMenu.onOpenLog = { [weak self] in
            guard let self else { return }
            NSWorkspace.shared.open(self.log.dir)
        }
        statusMenu.onQuit = { [weak self] in self?.quit() }
        statusMenu.info = { [weak self] in
            guard let self else { fatalError() }
            return StatusMenu.Info(model: self.model, serverURL: self.conn?.url.absoluteString ?? self.settings.serverURL,
                                   connState: self.conn?.state ?? .closed, retryIn: self.retryIn,
                                   closeNote: self.model.closeNote, axTrusted: self.model.axTrusted,
                                   warningAccepted: self.model.warningAccepted, logFile: self.log.currentFile.path)
        }
        settingsWindow.onSave = { [weak self] s in self?.saveSettings(s) }
    }

    private func wireOverlay() {
        overlay.onOnset = { [weak self] id, t in self?.dispatch(.onset(layoutID: id, tClient: t)) }
        overlay.onCycle = { [weak self] id, n, t in self?.dispatch(.cycle(layoutID: id, n: n, tClient: t)) }
        overlay.onFrames = { [weak self] hz, dropped in self?.dispatch(.frames(hz: hz, dropped: dropped)) }
        overlay.onHover = { [weak self] slot in self?.dispatch(.hover(slot)) }
        overlay.setStrip(text: "BrainPoint: OFF - starting", color: StatusMenu.color(.off))
    }

    private func wireConnection() {
        conn.log = { [weak self] k, f in self?.log.write(k, f) }
        conn.hello = { [weak self] in
            guard let self else { return nil }
            let token = self.settings.token.isEmpty ? nil : self.settings.token
            let hz = self.model.measuredHz ?? self.screenRefresh()
            let msg = Codec.hello(token: token, instance: self.instance, version: AppController.version,
                                  refreshHz: (hz * 1000).rounded() / 1000, viewport: self.screenViewport())
            self.log.write("send", ["type": "hello", "refresh_hz": hz, "token": token == nil ? "none" : "set",
                                    "instance": self.instance])
            return Codec.encode(msg)
        }
        conn.onState = { [weak self] s in
            guard let self else { return }
            switch s {
            case .connecting: self.dispatch(.connecting)
            case .open:
                self.retryIn = nil
                self.dispatch(.display(self.mainScreenFrame()))   // where a training row goes
                self.dispatch(.opened)
            case .closed: break   // onClose carries the code
            }
        }
        conn.onClose = { [weak self] code, reason, wait in
            guard let self else { return }
            self.retryIn = wait
            self.log.write("closed", ["code": code ?? NSNull(), "reason": reason,
                                      "retry_in_s": wait ?? NSNull()])
            self.dispatch(.closed(code: code, reason: reason))
        }
        conn.onText = { [weak self] text in self?.received(text) }
        conn.onRTT = { [weak self] ms in self?.dispatch(.rtt(ms)) }
    }

    private func received(_ text: String) {
        switch Codec.parse(text) {
        case .failure(let e):
            log.write("recv_bad", ["error": e.message])
        case .success(let msg):
            switch msg {
            case .welcome: conn.welcomed()
            case .pong(let p): conn.pong(p)
            case .error(let e) where e.fatal: conn.noteFatal(code: e.code)
            default: break
            }
            if case .status = msg {} else if case .pong = msg {} else if case .ping = msg {} else {
                log.write("recv", ["type": msg.type, "bytes": text.utf8.count])
            }
            dispatch(.message(msg))
        }
    }

    private func saveSettings(_ s: Settings) {
        let old = settings
        settings = s
        s.save()
        log.write("settings", ["server": s.serverURL, "allow_fake_control": s.allowFakeControl,
                               "deny": s.denyList, "token": s.token.isEmpty ? "none" : "set"])
        dispatch(.settings(allowFakeControl: s.allowFakeControl, denyList: s.denyList))
        if (s.serverURL != old.serverURL || s.token != old.token), let url = Settings.validURL(s.serverURL),
           model.warningAccepted {
            speaker.serverURL = url
            speaker.token = s.token.isEmpty ? nil : s.token
            conn.reconnect(to: url)
        }
    }

    // MARK: events

    private func tick() {
        let t = Permissions.accessibilityTrusted
        if t != model.axTrusted { dispatch(.axTrusted(t)) } else { dispatch(.tick) }
    }

    private func focusEvent(_ e: FocusEvent) {
        switch e {
        case .activated(let pid, let bundleID, let name):
            dispatch(.front(FrontApp(name: name, bundleID: bundleID, pid: pid)))
        case .windowChanged:
            dispatch(.focusChanged)
        case .none:
            dispatch(.front(nil))
        }
        let gen = model.focusGen
        focusSettle?.invalidate()
        focusSettle = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.dispatch(.focusSettled(gen: gen)) }
        }
    }

    func dispatch(_ event: AppEvent) {
        let effects = model.handle(event, now: now)
        for e in effects { run(e) }
        // The menu is rebuilt when it opens (menuNeedsUpdate); the title follows gate effects.
    }

    private func run(_ effect: AppEffect) {
        switch effect {
        case .send(let text):
            if !conn.send(text) { log.write("send_dropped", ["why": "socket not open"]) }
            if let o = Codec.object(text) { log.write("send", summary(o)) }
        case .scan(let scanID, let settleMs):
            scanTimer?.invalidate()
            scanTimer = Timer.scheduledTimer(withTimeInterval: Double(settleMs) / 1000, repeats: false) { [weak self] _ in
                MainActor.assumeIsolated { self?.performScan(scanID) }
            }
        case .press(let clickID, let hintID, let label):
            DispatchQueue.main.async { [weak self] in
                MainActor.assumeIsolated { self?.press(clickID: clickID, hintID: hintID, label: label) }
            }
        case .showOverlay(let plan):
            overlay.show(targets: plan.targets, flicker: plan.flicker, refreshHz: plan.refreshHz, layoutID: plan.layoutID,
                         code: plan.code)
            if !plan.status.isEmpty { setStrip(model.gate) }
            log.write("overlay", ["layout_id": plan.layoutID, "flicker": plan.flicker, "refresh_hz": plan.refreshHz,
                                  "code": plan.code != nil, "status": plan.status,
                                  "targets": plan.targets.map { ["slot": $0.slot, "label": $0.label, "freq": $0.freq,
                                                                 "lag": $0.lag.map { $0 as Any } ?? NSNull(),
                                                                 "x": Double($0.rect.minX), "y": Double($0.rect.minY),
                                                                 "w": Double($0.rect.width), "h": Double($0.rect.height)] }])
        case .hideOverlay:
            overlay.hide()
            log.write("overlay", ["hidden": true])
        case .gate(let from, let to):
            statusMenu.setTitle(to.state)
            setStrip(to)
            log.write("gate", ["from": from.state.rawValue, "to": to.state.rawValue, "reason": to.reason,
                               "headset": to.headsetLine, "may_flicker": to.mayFlicker, "may_press": to.mayPress])
            if from.state != to.state {
                notifier.post(title: "BrainPoint: \(StatusMenu.shortName(to.state))", body: to.reason)
            }
        case .speak(let text):
            speaker.say(text)
        case .note(let text):
            log.write("note", ["text": text])
        }
    }

    /// The strip under the menu bar: the gate's state and reason, or in code mode the c-VEP
    /// line (training progress, the decision's confidence, the template), then the headset.
    private func setStrip(_ g: GateDecision) {
        let status = model.overlay?.status ?? ""
        let what = status.isEmpty ? g.reason : status
        overlay.setStrip(text: "BrainPoint: \(StatusMenu.shortName(g.state)) - \(what)  |  \(g.headsetLine)",
                         color: StatusMenu.color(g.state))
    }

    /// A short, token-free log record of an outgoing message.
    private func summary(_ o: [String: Any]) -> [String: Any] {
        var s: [String: Any] = ["type": o["type"] ?? "?"]
        for k in ["scan_id", "layout_id", "click_id", "ok", "method", "detail", "slot", "refresh_hz", "dropped", "source"] {
            if let v = o[k] { s[k] = v }
        }
        if let h = o["hints"] as? [Any] { s["hints"] = h.count }
        if let a = o["app"] { s["app"] = a }
        if let w = o["window"] { s["window"] = w }
        return s
    }

    private func performScan(_ scanID: Int) {
        let screen = screenViewport()
        guard model.axTrusted else {
            registry.clear()
            dispatch(.scanned(scanID: scanID, snapshot: nil, front: nil, screen: screen, why: "Accessibility permission missing"))
            return
        }
        guard let app = NSWorkspace.shared.frontmostApplication else {
            registry.clear()
            dispatch(.scanned(scanID: scanID, snapshot: nil, front: nil, screen: screen, why: "no front app"))
            return
        }
        let front = FrontApp(name: app.localizedName ?? "", bundleID: app.bundleIdentifier ?? "", pid: app.processIdentifier)
        if app.processIdentifier == getpid() {
            registry.clear()
            dispatch(.scanned(scanID: scanID, snapshot: nil, front: front, screen: screen, why: "BrainPoint itself is in front"))
            return
        }
        if model.denyList.contains(front.bundleID) {
            registry.clear()
            dispatch(.scanned(scanID: scanID, snapshot: nil, front: front, screen: screen, why: "\(front.name) is on the deny list"))
            return
        }
        // Once per pid, and only for Chromium/Electron apps: other apps (Safari, native) are
        // never written to.
        if !accessibilityEnabled.contains(front.pid) {
            accessibilityEnabled.insert(front.pid)
            if AXWalker.isChromiumBased(pid: front.pid) {
                AXWalker.enableAppAccessibility(pid: front.pid)
            }
        }
        // Screen scope: the focused window plus the menu bar, open menus, the Dock and the
        // scroll strips on the screen that holds it; that screen is the scan's viewport.
        guard let walk = ScreenWalker.walk(pid: front.pid) else {
            registry.clear()
            dispatch(.scanned(scanID: scanID, snapshot: nil, front: front, screen: screen, why: "no focused window in \(front.name)"))
            return
        }
        let snap = walk.snapshot
        registry.replace(with: snap, elements: walk.elements)
        let vp = Viewport(w: Double(snap.viewport.width), h: Double(snap.viewport.height), dpr: walk.screenScale)
        log.write("walk", ["scan_id": scanID, "nodes": walk.nodes, "ms": (snap.walkMs * 10).rounded() / 10,
                           "kind_ms": snap.kindMs, "truncated": snap.truncated,
                           "kinds": Dictionary(uniqueKeysWithValues: snap.countsByKind.map { ($0.key.rawValue, $0.value) })])
        dispatch(.scanned(scanID: scanID, snapshot: snap, front: front, screen: vp, why: nil))
    }

    private func press(clickID: Int, hintID: Int, label: String) {
        guard model.gate.mayPress else {   // the gate may have closed since the click arrived
            dispatch(.pressed(clickID: clickID, label: label, ok: false, method: "none",
                              detail: "not pressed: \(model.gate.reason)"))
            return
        }
        let r = registry.press(id: hintID)
        log.write("press", ["click_id": clickID, "hint_id": hintID, "label": label, "ok": r.ok, "method": r.method, "detail": r.detail])
        dispatch(.pressed(clickID: clickID, label: label, ok: r.ok, method: r.method, detail: r.detail))
    }

    // MARK: screen

    private func screenRefresh() -> Double {
        let fps = NSScreen.main?.maximumFramesPerSecond ?? 60
        return Double(fps > 0 ? fps : 60)
    }

    private func screenViewport() -> Viewport {
        let s = NSScreen.main ?? NSScreen.screens.first
        let f = s?.frame.size ?? CGSize(width: 1440, height: 900)
        return Viewport(w: Double(f.width), h: Double(f.height), dpr: Double(s?.backingScaleFactor ?? 2))
    }

    /// The main screen in global top-left points (the hello's viewport): the c-VEP training
    /// row, which comes before any scan, is drawn there.
    private func mainScreenFrame() -> CGRect {
        let s = NSScreen.main ?? NSScreen.screens.first
        let f = s?.frame ?? CGRect(x: 0, y: 0, width: 1440, height: 900)
        return OverlayController.cocoaRect(fromTopLeft: f)   // the flip is its own inverse
    }

    private func note(_ text: String) { log.write("note", ["text": text]) }
}
