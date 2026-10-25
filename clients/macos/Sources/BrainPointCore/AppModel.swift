import CoreGraphics
import Foundation

// The menu-bar app's state and logic as a pure reducer, testable without AppKit:
//
//     var model = AppModel(ownBundleID: "ai.brainsync.brainpoint")
//     for effect in model.handle(.message(msg), now: uptime) { run(effect) }
//
// The app feeds it events (socket, server messages, permissions, focus, overlay callbacks,
// scan and press results) and carries out the effects it returns (send a frame, walk the front
// window, press an element, show or hide the overlay, speak, log). After every event the
// control gate is decided again; a change comes out as `.gate(from:to:)`, and the overlay plan
// is re-derived, so nothing flickers or presses unless the gate allows it.

public enum ConnState: String, Sendable { case closed, connecting, open }

public struct ScanInfo: Equatable, Sendable {
    public var scanID: Int
    public var app: String
    public var window: String
    public var nHints: Int
    public var walkMs: Double
    public var truncated: Bool
    public var serverMs: Double?
    public var cached: Bool?
    public var ok: Bool?
    public var nCandidates: Int?
    public var summary: String = ""
}

public struct PressInfo: Equatable, Sendable {
    public var clickID: Int
    public var label: String
    public var ok: Bool
    public var method: String
    public var detail: String
}

/// What the overlay should draw: global-point rects, and whether they flicker.
public struct OverlayPlan: Equatable, Sendable {
    public var layoutID: Int
    public var flicker: Bool
    public var refreshHz: Double
    public var targets: [OverlayTarget]
}

public enum AppEvent: Sendable {
    case connecting
    case opened
    case closed(code: Int?, reason: String)
    case message(ServerMessage)
    case axTrusted(Bool)
    case warningAccepted
    case pause(String)                 // who paused: "Esc", "menu"
    case resume
    case safety(String?)               // nil = safe again
    case front(FrontApp?)              // the frontmost app changed (nil: nothing usable)
    case focusChanged                  // any focus or focused-window change
    case focusSettled(gen: Int)        // 0.5 s after the focus change numbered `gen`
    case scanned(scanID: Int, snapshot: WindowSnapshot?, front: FrontApp?, screen: Viewport, why: String?)
    case pressed(clickID: Int, label: String, ok: Bool, method: String, detail: String)
    case onset(layoutID: Int, tClient: Double)
    case frames(hz: Double, dropped: Int)
    case hover(String?)
    case settings(allowFakeControl: Bool, denyList: [String])
    case measuredRefresh(Double)
    case rtt(Int)
    case tick
}

public enum AppEffect: Equatable, Sendable {
    case send(String)                                   // one JSON text frame
    case scan(scanID: Int, settleMs: Int)               // wait, walk the front window, feed .scanned
    case press(clickID: Int, hintID: Int, label: String) // press it, feed .pressed
    case showOverlay(OverlayPlan)
    case hideOverlay
    case gate(from: GateDecision, to: GateDecision)
    case speak(String)
    case note(String)                                   // a verbose line for the log
}

public struct AppModel: Sendable {
    public let ownBundleID: String

    // connection and server state
    public private(set) var conn: ConnState = .closed
    public private(set) var welcome: Welcome?
    public private(set) var phase: PhaseMsg?
    public private(set) var layout: LayoutMsg?
    public private(set) var status: StatusMsg?
    public private(set) var lastError: ErrorMsg?
    public private(set) var lastSelection: SelectionMsg?
    public private(set) var streamingSince: Double?
    public private(set) var rttMs: Int?
    public private(set) var closeNote: String = ""

    // local state
    public private(set) var axTrusted = false
    public private(set) var warningAccepted = false
    public private(set) var userPaused = false
    public private(set) var safetyPause: String?
    public private(set) var allowFakeControl = false
    public private(set) var denyList: Set<String>
    public private(set) var front: FrontApp?
    public private(set) var measuredHz: Double?

    // scan, overlay and clicks
    public private(set) var pendingScan: Int?
    public private(set) var snapshot: WindowSnapshot?
    public private(set) var snapshotStale = false
    public private(set) var focusGen = 0
    private var snapshotGen = 0
    public private(set) var overlay: OverlayPlan?
    public private(set) var onsetSentFor: Int?
    public private(set) var gazeSlot: String?
    public private(set) var lastScan: ScanInfo?
    public private(set) var lastPress: PressInfo?

    public private(set) var gate: GateDecision
    private var headsetGood: Bool?

    public static let defaultDenyList = ["com.apple.systempreferences", "com.apple.keychainaccess"]

    public init(ownBundleID: String = "ai.brainsync.brainpoint", denyList: [String]? = nil,
                allowFakeControl: Bool = false) {
        self.ownBundleID = ownBundleID
        self.denyList = Set((denyList ?? AppModel.defaultDenyList) + [ownBundleID])
        self.allowFakeControl = allowFakeControl
        gate = GateDecision(state: .off, mayFlicker: false, mayPress: false,
                            reason: "Not connected to the BrainSync server.", headsetLine: "Headset unknown (no server)")
    }

    // MARK: derived

    public var connected: Bool { conn == .open && welcome != nil }

    public var frontAppDenied: String? {
        guard let f = front else { return nil }
        if f.bundleID == ownBundleID { return "BrainPoint itself" }
        return denyList.contains(f.bundleID) ? (f.name.isEmpty ? f.bundleID : f.name) : nil
    }

    /// The refresh the overlay computes luminance at (welcome's nominal rate unless the
    /// measured one is further than the tolerance from it).
    public var lumHz: Double {
        guard let w = welcome else { return measuredHz ?? 60 }
        return FlickerMath.lumHz(measured: measuredHz, nominal: w.refreshHz, tolerance: w.refreshToleranceHz)
    }

    public func gateInputs() -> GateInputs {
        var i = GateInputs()
        i.serverConnected = connected
        i.isController = welcome?.role == "controller"
        i.boardState = status?.board ?? "unknown"
        i.boardOk = status?.ok ?? false
        i.fakeBoard = welcome?.fake ?? false
        i.streamingSince = streamingSince
        i.axTrusted = axTrusted
        i.warningAccepted = warningAccepted
        i.userPaused = userPaused
        i.safetyPause = safetyPause
        i.phase = phase?.phase ?? "wait"
        i.allowFakeControl = allowFakeControl
        i.frontAppDenied = frontAppDenied
        i.serverNote = serverNote
        return i
    }

    /// Why the server holds the session: a non-fatal error while waiting (detect_unavailable,
    /// refresh), else the phase text.
    var serverNote: String? {
        if phase?.phase ?? "wait" == "wait", let e = lastError, !e.fatal {
            return "\(e.code): \(e.message)"
        }
        let t = phase?.text ?? ""
        return t.isEmpty ? nil : t
    }

    // MARK: reducer

    public mutating func handle(_ event: AppEvent, now: Double) -> [AppEffect] {
        var out: [AppEffect] = []
        switch event {
        case .connecting:
            conn = .connecting
        case .opened:
            conn = .open
            closeNote = ""
        case .closed(let code, let reason):
            conn = .closed
            closeNote = "closed" + (code.map { " (code \($0))" } ?? "") + (reason.isEmpty ? "" : ": \(reason)")
            welcome = nil; phase = nil; layout = nil; status = nil; streamingSince = nil
            pendingScan = nil; onsetSentFor = nil; gazeSlot = nil; rttMs = nil
        case .message(let msg):
            out += onMessage(msg, now: now)
        case .axTrusted(let t):
            if t != axTrusted { out.append(.note(t ? "Accessibility permission granted" : "Accessibility permission missing")) }
            axTrusted = t
        case .warningAccepted:
            warningAccepted = true
        case .pause(let who):
            if !userPaused {
                userPaused = true
                out.append(.note("paused by \(who)"))
                out += send(Codec.pause())
            }
        case .resume:
            if let s = safetyPause {
                out.append(.note("resume refused: still unsafe (\(s))"))
            } else if userPaused {
                userPaused = false
                out.append(.note("resumed"))
                out += send(Codec.resume())
            }
        case .safety(let reason):
            if reason != safetyPause { out.append(.note(reason.map { "safety pause: \($0)" } ?? "safety: clear")) }
            safetyPause = reason
            if reason != nil && !userPaused {
                userPaused = true
                out += send(Codec.pause())
            }
        case .front(let app):
            if app != front {
                front = app
                focusGen += 1
                out.append(.note("front app: " + (app.map { "\($0.name) (\($0.bundleID), pid \($0.pid))" } ?? "none")))
            }
        case .focusChanged:
            focusGen += 1
        case .focusSettled(let gen):
            out += focusSettled(gen)
        case .scanned(let scanID, let snap, let app, let screen, let why):
            out += scanned(scanID, snap, app, screen, why)
        case .pressed(let clickID, let label, let ok, let method, let detail):
            lastPress = PressInfo(clickID: clickID, label: label, ok: ok, method: method, detail: detail)
            out.append(.note("click \(clickID) \(ok ? "pressed" : "not pressed") \(label): \(method) \(detail)"))
            out += send(Codec.clicked(clickID: clickID, ok: ok, method: method, detail: detail))
        case .onset(let id, let t):
            if layout?.layoutID == id, onsetSentFor != id, overlay?.flicker == true, overlay?.layoutID == id {
                onsetSentFor = id
                out += send(Codec.onset(layoutID: id, tClient: t))
            }
        case .frames(let hz, let dropped):
            if hz > 0 { measuredHz = hz }
            out += send(Codec.frames(refreshHz: hz, dropped: dropped))
        case .hover(let slot):
            if welcome?.fake == true, connected, slot != gazeSlot {
                gazeSlot = slot
                out += send(Codec.gaze(slot: slot))
            }
        case .settings(let allow, let deny):
            allowFakeControl = allow
            denyList = Set(deny + [ownBundleID])
        case .measuredRefresh(let hz):
            if hz > 0, hz.isFinite { measuredHz = hz }
        case .rtt(let ms):
            rttMs = ms
        case .tick:
            break
        }
        out += refreshGate(now: now)
        out += syncOverlay()
        return out
    }

    private func send(_ msg: [String: Any]) -> [AppEffect] {
        guard conn == .open else { return [] }
        guard let text = Codec.encode(msg) else { return [.note("could not encode \(msg["type"] ?? "?") (NaN or infinity)")] }
        return [.send(text)]
    }

    private mutating func onMessage(_ msg: ServerMessage, now: Double) -> [AppEffect] {
        var out: [AppEffect] = []
        switch msg {
        case .welcome(let w):
            welcome = w
            lastError = nil
            out.append(.note("welcome: role \(w.role), mode \(w.mode ?? "-"), \(w.fake ? "SIMULATED board" : "real board"), detect \(w.detect), refresh \(w.refreshHz) Hz, freqs \(w.freqs)"))
            if w.role != "controller" { out.append(.note("this client is a viewer: it cannot control the headset")) }
            if userPaused { out += send(Codec.pause()) }   // the server starts every controller in run
        case .phase(let p):
            if p != phase { out.append(.note("phase \(p.phase)\(p.flicker ? " (flicker)" : "")\(p.text.isEmpty ? "" : ": \(p.text)")")) }
            phase = p
        case .layout(let l):
            layout = l
            out.append(.note("layout \(l.layoutID) \(l.stage): " + l.targets.map { "\($0.slot)=\($0.label) \($0.freq) Hz" }.joined(separator: ", ")))
        case .selection(let s):
            lastSelection = s
            let label = layout?.targets.first(where: { $0.slot == s.slot })?.label ?? ""
            out.append(.note("selection \(s.slot) \(label) (layout \(s.layoutID)\(s.score.map { String(format: ", score %.3f", $0) } ?? ""))"))
        case .scanRequest(let r):
            pendingScan = r.scanID
            out.append(.note("scan_request \(r.scanID) (settle \(r.settleMs) ms)"))
            out.append(.scan(scanID: r.scanID, settleMs: r.settleMs))
        case .scanResult(let r):
            if var s = lastScan, s.scanID == r.scanID {
                s.serverMs = r.ms; s.cached = r.cached; s.ok = r.ok; s.nCandidates = r.nCandidates; s.summary = r.summary
                lastScan = s
            }
            out.append(.note("scan_result \(r.scanID): \(r.ok ? "ok" : "FAILED") \(Int(r.ms)) ms\(r.cached ? " (cached)" : ""), \(r.nElements) elements, \(r.nCandidates) candidates\(r.summary.isEmpty ? "" : ": \(r.summary)")\(r.error.map { ", error \($0)" } ?? "")"))
        case .click(let c):
            out += onClick(c)
        case .status(let s):
            if s.board == "streaming" {
                if status?.board != "streaming" || streamingSince == nil { streamingSince = now }
            } else {
                streamingSince = nil
            }
            if status?.board != s.board || status?.ok != s.ok || (status?.text != s.text && !s.ok) {
                out.append(.note("status: board \(s.board), ok \(s.ok)\(s.text.isEmpty ? "" : ", \(s.text)")"))
            }
            status = s
        case .error(let e):
            lastError = e
            out.append(.note("server error \(e.code)\(e.fatal ? " (fatal)" : ""): \(e.message)"))
        case .pong, .ping:
            break
        case .unknown(let t):
            out.append(.note("ignored unknown server message type \(t)"))
        }
        return out
    }

    private func label(forHint id: Int?) -> String {
        guard let id, let s = snapshot else { return "" }
        return s.items.first(where: { $0.id == id })?.label ?? ""
    }

    private mutating func onClick(_ c: ClickMsg) -> [AppEffect] {
        var label = c.element.label
        if label.isEmpty { label = self.label(forHint: c.element.hintID) }
        if label.isEmpty { label = c.element.hintID.map { "hint \($0)" } ?? "element" }
        if gate.mayPress, let hid = c.element.hintID, snapshot != nil, !snapshotStale {
            return [.note("click \(c.clickID): pressing \(label) (hint \(hid))"),
                    .press(clickID: c.clickID, hintID: hid, label: label)]
        }
        let detail: String
        if gate.dryRun {
            detail = "dry run: would press \(label)"
        } else if gate.mayPress && c.element.hintID == nil {
            detail = "not pressed: the click names no hint_id"
        } else if gate.mayPress {
            detail = "not pressed: the front window changed since the scan"
        } else {
            detail = "not pressed: \(gate.reason)"
        }
        lastPress = PressInfo(clickID: c.clickID, label: label, ok: false, method: "none", detail: detail)
        return [.note("click \(c.clickID) refused: \(detail)")]
            + send(Codec.clicked(clickID: c.clickID, ok: false, method: "none", detail: detail))
    }

    private mutating func focusSettled(_ gen: Int) -> [AppEffect] {
        guard gen == focusGen, snapshot != nil, snapshotGen < gen, !snapshotStale else { return [] }
        snapshotStale = true
        var out: [AppEffect] = [.note("focus changed: overlay cleared until the next scan")]
        // Ask the server for a fresh scan (v3.1 rescan; it debounces to one per 0.5 s). Not
        // while a scan_request is pending: that scan walks the new front window anyway. With
        // an idle layout the server may still be ranking the old window, so ask then too.
        if pendingScan == nil, !userPaused {
            out += send(Codec.rescan())
        }
        return out
    }

    private mutating func scanned(_ scanID: Int, _ snap: WindowSnapshot?, _ app: FrontApp?, _ screen: Viewport,
                                  _ why: String?) -> [AppEffect] {
        guard pendingScan == scanID else {
            return [.note("dropped scan \(scanID): the server now waits for \(pendingScan.map(String.init) ?? "no scan")")]
        }
        pendingScan = nil
        snapshotGen = focusGen
        snapshotStale = false
        snapshot = snap
        if let s = snap {
            let hints = Hints.make(s)
            let a = FrontApp(name: s.appName, bundleID: s.bundleID, pid: s.pid)
            // The viewport is the screen holding the window (screen scope), else the window.
            let vp = Viewport(w: max(1, Double(s.viewport.width)), h: max(1, Double(s.viewport.height)), dpr: screen.dpr)
            lastScan = ScanInfo(scanID: scanID, app: s.appName, window: s.windowTitle, nHints: hints.count,
                                walkMs: s.walkMs, truncated: s.truncated)
            let kinds = ItemKind.allCases.compactMap { k in s.countsByKind[k].map { "\(k.rawValue) \($0)" } }
                .joined(separator: ", ")
            return [.note(String(format: "scan %d: %@ \"%@\", %d items (%@), %d hints, walk %.0f ms%@", scanID, s.appName,
                                 s.windowTitle, s.items.count, kinds, hints.count, s.walkMs, s.truncated ? " (truncated)" : ""))]
                + send(Codec.scanAX(scanID: scanID, app: a, windowTitle: s.windowTitle, viewport: vp, hints: hints))
        }
        let a = app ?? FrontApp(name: "", bundleID: "", pid: 0)
        lastScan = ScanInfo(scanID: scanID, app: a.name, window: "", nHints: 0, walkMs: 0, truncated: false)
        return [.note("scan \(scanID): 0 hints (\(why ?? "no usable window"))")]
            + send(Codec.scanAX(scanID: scanID, app: a, windowTitle: "", viewport: screen, hints: []))
    }

    private mutating func refreshGate(now: Double) -> [AppEffect] {
        var out: [AppEffect] = []
        let inputs = gateInputs()
        let next = ControlGate.decide(inputs, now: now)
        if next != gate {
            let prev = gate
            gate = next
            out.append(.gate(from: prev, to: next))
            if prev.state != .armed && next.state == .armed { out.append(.speak("BrainPoint armed.")) }
            if prev.state == .armed && next.state != .armed { out.append(.speak("BrainPoint disarmed. " + next.reason)) }
        }
        if inputs.serverConnected {
            let good = inputs.boardState == "streaming" && inputs.boardOk
            if let was = headsetGood, was != good {
                out.append(.speak(good ? "Headset streaming again." : "Headset lost. " + next.headsetLine + "."))
            }
            if status != nil { headsetGood = good }
        } else {
            headsetGood = nil
        }
        return out
    }

    func desiredOverlay() -> OverlayPlan? {
        guard [.armed, .dryRun, .calibrating].contains(gate.state), let l = layout, !l.targets.isEmpty,
              let s = snapshot, !snapshotStale else { return nil }
        let flicker = gate.mayFlicker && phase?.phase == "run" && phase?.flicker == true
        let targets = l.targets.compactMap { t -> OverlayTarget? in
            guard let r = t.rects.first else { return nil }
            return OverlayTarget(slot: t.slot, label: t.label, kind: t.kind, freq: t.freq, phase: t.phase,
                                 rect: Geometry.toScreen(r, window: s.viewport))
        }
        return targets.isEmpty ? nil : OverlayPlan(layoutID: l.layoutID, flicker: flicker, refreshHz: lumHz, targets: targets)
    }

    private mutating func syncOverlay() -> [AppEffect] {
        let want = desiredOverlay()
        guard want != overlay else { return [] }
        overlay = want
        var out: [AppEffect] = []
        if let w = want {
            out.append(.showOverlay(w))
        } else {
            out.append(.hideOverlay)
            if gazeSlot != nil, welcome?.fake == true {
                gazeSlot = nil
                out += send(Codec.gaze(slot: nil))
            }
        }
        return out
    }
}
