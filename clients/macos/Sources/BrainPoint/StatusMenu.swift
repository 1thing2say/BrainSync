import AppKit
import BrainPointCore

/// The menu-bar item: an icon plus the short control state, and a menu that says everything
/// (server, headset, control and why, front app, last scan, last press) with Pause / Resume,
/// Settings…, the log folder and Quit.
@MainActor
final class StatusMenu: NSObject, NSMenuDelegate {
    struct Info {
        var model: AppModel
        var serverURL: String
        var connState: Connection.State
        var retryIn: Double?
        var closeNote: String
        var axTrusted: Bool
        var warningAccepted: Bool
        var logFile: String
    }

    var onPause: (() -> Void)?
    var onResume: (() -> Void)?
    var onSettings: (() -> Void)?
    var onGrantAX: (() -> Void)?
    var onReconnect: (() -> Void)?
    var onOpenLog: (() -> Void)?
    var onQuit: (() -> Void)?
    var info: (() -> Info)?

    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()

    override init() {
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        setTitle(.off)
    }

    static func shortName(_ s: ControlState) -> String {
        switch s {
        case .armed: "ARMED"
        case .dryRun: "DRY RUN"
        case .off: "OFF"
        case .paused: "PAUSED"
        case .calibrating: "CALIB"
        case .training: "TRAIN"
        }
    }

    static func color(_ s: ControlState) -> NSColor {
        switch s {
        case .armed: .systemGreen
        case .dryRun: .systemOrange
        case .calibrating: .systemBlue
        case .training: .systemPurple
        case .paused: .systemGray
        case .off: .systemRed
        }
    }

    func setTitle(_ s: ControlState) {
        guard let b = item.button else { return }
        let symbol = s == .armed ? "eye.fill" : (s == .off ? "eye.slash" : "eye")
        b.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "BrainPoint")
        b.imagePosition = .imageLeading
        b.title = " " + StatusMenu.shortName(s)
        b.contentTintColor = s == .armed ? .systemGreen : nil
        b.toolTip = "BrainPoint: " + StatusMenu.shortName(s)
    }

    func menuNeedsUpdate(_ menu: NSMenu) { rebuild() }

    func rebuild() {
        guard let i = info?() else { return }
        let m = i.model
        menu.removeAllItems()
        func line(_ text: String, tip: String? = nil) {
            let it = NSMenuItem(title: text.count > 110 ? String(text.prefix(107)) + "..." : text, action: nil, keyEquivalent: "")
            it.isEnabled = false
            it.toolTip = tip ?? (text.count > 110 ? text : nil)
            menu.addItem(it)
        }
        var server = "Server: \(i.serverURL) - \(i.connState.rawValue)"
        if m.connected { server += m.welcome?.role == "controller" ? " (controller)" : " (viewer)" }
        if i.connState == .closed {
            server += i.retryIn.map { String(format: ", retry in %.0f s", $0) } ?? ", not retrying"
            if !i.closeNote.isEmpty { server += " [\(i.closeNote)]" }
        }
        line(server)
        var headset = "Headset: \(m.gate.headsetLine)"
        if let s = m.status { headset += " - board \(s.board)\(s.ok ? "" : ", not ok")\(s.text.isEmpty ? "" : ": \(s.text)")" }
        if let r = m.rttMs { headset += " - rtt \(r) ms" }
        line(headset)
        line("Control: \(StatusMenu.shortName(m.gate.state)) - \(m.gate.reason)", tip: m.gate.reason)
        if let e = m.lastError { line("Server said: \(e.code): \(e.message)", tip: e.message) }
        if let f = m.front {
            line("Front app: \(f.name) (\(f.bundleID))" + (m.frontAppDenied != nil ? " - denied" : ""))
        } else {
            line("Front app: none")
        }
        if let s = m.lastScan {
            var t = "Last scan \(s.scanID): \(s.nHints) hints in \(s.app)"
            t += String(format: ", walk %.0f ms", s.walkMs)
            if s.truncated { t += " (truncated)" }
            if let ms = s.serverMs { t += String(format: ", server %.0f ms", ms) }
            if s.cached == true { t += ", cached" }
            if s.ok == false { t += ", FAILED" }
            if let c = s.nCandidates { t += ", \(c) candidates" }
            line(t)
        } else {
            line("Last scan: none")
        }
        if let p = m.lastPress {
            line("Last press: \(p.label) - \(p.ok ? "ok" : "refused") \(p.method) \(p.detail)")
        } else {
            line("Last press: none")
        }
        if let sel = m.lastSelection { line("Last selection: \(sel.slot) (layout \(sel.layoutID))") }
        menu.addItem(.separator())
        if !i.axTrusted {
            add("Grant Accessibility Permission… (waiting)", #selector(grantAX))
        }
        if m.userPaused {
            let r = add("Resume", #selector(resume))
            if m.safetyPause != nil { r.isEnabled = false; r.toolTip = "Still unsafe: \(m.safetyPause!)" }
        } else {
            add("Pause (Esc)", #selector(pause))
        }
        if i.connState == .closed { add("Reconnect Now", #selector(reconnect)) }
        add("Settings…", #selector(settings), key: ",")
        add("Open Log Folder", #selector(openLog))
        menu.addItem(.separator())
        add("Quit BrainPoint", #selector(quit), key: "q")
    }

    @discardableResult
    private func add(_ title: String, _ sel: Selector, key: String = "") -> NSMenuItem {
        let it = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        it.target = self
        it.isEnabled = true
        menu.addItem(it)
        return it
    }

    @objc private func pause() { onPause?() }
    @objc private func resume() { onResume?() }
    @objc private func settings() { onSettings?() }
    @objc private func grantAX() { onGrantAX?() }
    @objc private func reconnect() { onReconnect?() }
    @objc private func openLog() { onOpenLog?() }
    @objc private func quit() { onQuit?() }
}
