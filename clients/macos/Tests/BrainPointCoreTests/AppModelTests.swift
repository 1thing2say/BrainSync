import CoreGraphics
import Foundation
import Testing
@testable import BrainPointCore

private let win = CGRect(x: 100, y: 50, width: 800, height: 600)
private let textEdit = FrontApp(name: "TextEdit", bundleID: "com.apple.TextEdit", pid: 77)
private let screen = Viewport(w: 1512, h: 982, dpr: 2)

private func msg(_ json: String) -> AppEvent {
    guard case .success(let m) = Codec.parse(json) else { fatalError("bad fixture \(json)") }
    return .message(m)
}

private func sent(_ effects: [AppEffect]) -> [[String: Any]] {
    effects.compactMap { if case .send(let t) = $0 { return Codec.object(t) } else { return nil } }
}

private func snapshot() -> WindowSnapshot {
    WindowSnapshot(appName: "TextEdit", bundleID: "com.apple.TextEdit", pid: 77, windowTitle: "Untitled",
                   windowFrame: win,
                   items: [AXItem(id: 0, role: "AXButton", subrole: "", label: "Bold", frame: CGRect(x: 120, y: 70, width: 40, height: 20), enabled: true, actions: ["AXPress"]),
                           AXItem(id: 1, role: "AXCheckBox", subrole: "", label: "Wrap", frame: CGRect(x: 200, y: 70, width: 40, height: 20), enabled: true, actions: ["AXPress"])],
                   walkMs: 9, truncated: false)
}

/// Connected to a fake-board server, streaming for a while, phase run, permissions given.
private func runningModel(fake: Bool = true, allowFake: Bool = false) -> AppModel {
    var m = AppModel(allowFakeControl: allowFake)
    _ = m.handle(.axTrusted(true), now: 0)
    _ = m.handle(.warningAccepted, now: 0)
    _ = m.handle(.front(textEdit), now: 0)
    _ = m.handle(.connecting, now: 0)
    _ = m.handle(.opened, now: 0)
    _ = m.handle(msg(#"{"type":"welcome","protocol":3,"role":"controller","mode":"point","fake":\#(fake),"waveform":"sine","refresh_hz":60,"refresh_tolerance_hz":1,"freqs":[15,17,12,20],"max_slots":4,"detect":true}"#), now: 0)
    _ = m.handle(msg(#"{"type":"status","board":"streaming","ok":true,"text":""}"#), now: 1)
    _ = m.handle(msg(#"{"type":"phase","phase":"run","flicker":true,"text":""}"#), now: 1)
    _ = m.handle(.tick, now: 5)
    return m
}

@Test func reachesDryRunWithTheSimulatedBoard() {
    var m = AppModel()
    var gates: [ControlState] = []
    func feed(_ e: AppEvent, _ t: Double) {
        for eff in m.handle(e, now: t) { if case .gate(_, let to) = eff { gates.append(to.state) } }
    }
    feed(.axTrusted(true), 0); feed(.warningAccepted, 0); feed(.front(textEdit), 0)
    feed(.connecting, 0); feed(.opened, 0)
    #expect(m.gate.state == .off && m.gate.reason.contains("Not connected"))
    feed(msg(#"{"type":"welcome","protocol":3,"role":"controller","mode":"point","fake":true,"waveform":"sine","refresh_hz":60,"refresh_tolerance_hz":1,"freqs":[15,17,12,20],"max_slots":4,"detect":true}"#), 0)
    #expect(m.gate.reason.contains("not streaming (board unknown)"))
    feed(msg(#"{"type":"status","board":"streaming","ok":true,"text":""}"#), 1)
    #expect(m.gate.reason.contains("just started"))
    feed(msg(#"{"type":"phase","phase":"calibrate","flicker":false,"seconds":3,"text":"relax"}"#), 2)
    #expect(m.gate.reason.contains("just started"))
    feed(.tick, 3.5)
    #expect(m.gate.state == .calibrating)
    feed(msg(#"{"type":"phase","phase":"run","flicker":true,"text":""}"#), 6)
    #expect(m.gate.state == .dryRun && m.gate.mayFlicker && !m.gate.mayPress)
    #expect(gates.last == .dryRun)
}

@Test func detectUnavailableIsTheReason() {
    var m = runningModel()
    _ = m.handle(msg(#"{"type":"error","code":"detect_unavailable","message":"Point mode needs element detection.","fatal":false}"#), now: 6)
    _ = m.handle(msg(#"{"type":"phase","phase":"wait","flicker":false,"text":""}"#), now: 6)
    #expect(m.gate.state == .off)
    #expect(m.gate.reason.contains("detect_unavailable: Point mode needs element detection"))
}

@Test func scanRequestWalksAndSendsAnAXScan() throws {
    var m = runningModel()
    let e1 = m.handle(msg(#"{"type":"scan_request","scan_id":4,"settle_ms":800}"#), now: 6)
    #expect(e1.contains(.scan(scanID: 4, settleMs: 800)))
    let e2 = m.handle(.scanned(scanID: 4, snapshot: snapshot(), front: textEdit, screen: screen, why: nil), now: 7)
    let scan = try #require(sent(e2).first)
    #expect(scan["type"] as? String == "scan" && scan["source"] as? String == "ax" && scan["scan_id"] as? Int == 4)
    #expect((scan["viewport"] as? [String: Double]) == ["w": 800, "h": 600, "dpr": 2])
    #expect((scan["hints"] as? [Any])?.count == 2)
    #expect(m.lastScan?.nHints == 2)
    // a second, stale result of the same scan is dropped
    #expect(sent(m.handle(.scanned(scanID: 4, snapshot: snapshot(), front: textEdit, screen: screen, why: nil), now: 7)).isEmpty)
    _ = m.handle(msg(#"{"type":"scan_result","scan_id":4,"ok":true,"ms":15,"n_elements":2,"n_candidates":2,"summary":"","elements":[],"cached":true}"#), now: 8)
    #expect(m.lastScan?.cached == true && m.lastScan?.serverMs == 15)
}

@Test func noWindowSendsZeroHints() throws {
    var m = runningModel()
    _ = m.handle(msg(#"{"type":"scan_request","scan_id":5,"settle_ms":0}"#), now: 6)
    let s = try #require(sent(m.handle(.scanned(scanID: 5, snapshot: nil, front: textEdit, screen: screen, why: "no focused window"), now: 6)).first)
    #expect((s["hints"] as? [Any])?.isEmpty == true)
    #expect((s["viewport"] as? [String: Double])?["w"] == 1512)
}

private func withLayout(_ m: inout AppModel) -> [AppEffect] {
    _ = m.handle(msg(#"{"type":"scan_request","scan_id":4,"settle_ms":800}"#), now: 6)
    _ = m.handle(.scanned(scanID: 4, snapshot: snapshot(), front: textEdit, screen: screen, why: nil), now: 6)
    return m.handle(msg(#"{"type":"layout","layout_id":7,"stage":"pick","targets":[{"slot":"b1","freq":15,"phase":0,"label":"Bold","kind":"button","rects":[{"x":0.25,"y":0.5,"w":0.1,"h":0.2}]},{"slot":"more","freq":20,"phase":0,"label":"More","kind":"more","rects":[{"x":0.86,"y":0.84,"w":0.12,"h":0.14}]}]}"#), now: 7)
}

@Test func layoutShowsTheOverlayInScreenPoints() throws {
    var m = runningModel()
    let effects = withLayout(&m)
    guard case .showOverlay(let plan)? = effects.last(where: { if case .showOverlay = $0 { true } else { false } }) else {
        Issue.record("no overlay: \(effects)"); return
    }
    #expect(plan.layoutID == 7 && plan.flicker && plan.refreshHz == 60)
    #expect(plan.targets[0].rect == CGRect(x: 300, y: 350, width: 80, height: 120))
    #expect(plan.targets[0].slot == "b1" && plan.targets[1].kind == "more")
    // onset once per layout
    #expect(sent(m.handle(.onset(layoutID: 7, tClient: 1000.5), now: 7)).first?["layout_id"] as? Int == 7)
    #expect(sent(m.handle(.onset(layoutID: 7, tClient: 1001), now: 7)).isEmpty)
    #expect(sent(m.handle(.onset(layoutID: 6, tClient: 1001), now: 7)).isEmpty)
    // the same state again changes nothing
    #expect(!m.handle(.tick, now: 8).contains(where: { if case .showOverlay = $0 { true } else { false } }))
}

@Test func dryRunRefusesClicksWithTheLabel() throws {
    var m = runningModel()
    _ = withLayout(&m)
    let e = m.handle(msg(#"{"type":"click","click_id":2,"x":0.3,"y":0.6,"element":{"id":0,"label":"Bold","role":"button","hint_id":0,"risky":false}}"#), now: 8)
    #expect(!e.contains(where: { if case .press = $0 { true } else { false } }))
    let c = try #require(sent(e).first)
    #expect(c["type"] as? String == "clicked" && c["ok"] as? Bool == false && c["method"] as? String == "none")
    #expect(c["detail"] as? String == "dry run: would press Bold")
    #expect(m.lastPress?.detail == "dry run: would press Bold")
}

@Test func clickAfterTheServersPauseIsStillPressedOrNamedAsDryRun() throws {
    // The live order (2026-10-24): phase pause ("Working"), then click.
    var m = runningModel(allowFake: true)
    _ = withLayout(&m)
    _ = m.handle(msg(#"{"type":"phase","phase":"pause","flicker":false,"seconds":null,"text":"Working"}"#), now: 8)
    let e = m.handle(msg(#"{"type":"click","click_id":2,"x":0.3,"y":0.6,"element":{"id":0,"label":"","role":"button","hint_id":1,"risky":false}}"#), now: 8)
    #expect(e.contains(.press(clickID: 2, hintID: 1, label: "Wrap")))
    var d = runningModel()
    _ = withLayout(&d)
    _ = d.handle(msg(#"{"type":"phase","phase":"pause","flicker":false,"seconds":null,"text":"Working"}"#), now: 8)
    let e2 = d.handle(msg(#"{"type":"click","click_id":2,"x":0.3,"y":0.6,"element":{"id":0,"label":"Bold","role":"button","hint_id":0,"risky":false}}"#), now: 8)
    #expect(sent(e2).first?["detail"] as? String == "dry run: would press Bold")
}

@Test func armedPressesTheHint() throws {
    var m = runningModel(allowFake: true)
    #expect(m.gate.state == .armed)
    _ = withLayout(&m)
    let e = m.handle(msg(#"{"type":"click","click_id":2,"x":0.3,"y":0.6,"element":{"id":0,"label":"","role":"button","hint_id":1,"risky":false}}"#), now: 8)
    #expect(e.contains(.press(clickID: 2, hintID: 1, label: "Wrap")))
    let r = try #require(sent(m.handle(.pressed(clickID: 2, label: "Wrap", ok: true, method: "AXPress", detail: ""), now: 8)).first)
    #expect(r["ok"] as? Bool == true && r["method"] as? String == "AXPress" && r["click_id"] as? Int == 2)
    // without a hint id, nothing is pressed
    let e2 = m.handle(msg(#"{"type":"click","click_id":3,"x":0.3,"y":0.6,"element":{"label":"X"}}"#), now: 9)
    #expect(sent(e2).first?["detail"] as? String == "not pressed: the click names no hint_id")
}

@Test func escPausesAndHidesAndResumeComesFromTheMenu() throws {
    var m = runningModel()
    _ = withLayout(&m)
    let e = m.handle(.pause("Esc"), now: 8)
    #expect(sent(e).map { $0["type"] as? String } == ["pause"])
    #expect(e.contains(.hideOverlay))
    #expect(m.gate.state == .paused)
    #expect(sent(m.handle(.pause("Esc"), now: 8)).isEmpty)
    // a safety pause blocks resume until it clears
    _ = m.handle(.safety("screen locked"), now: 9)
    #expect(m.gate.reason == "Paused for safety: screen locked.")
    #expect(sent(m.handle(.resume, now: 9)).isEmpty)
    _ = m.handle(.safety(nil), now: 10)
    #expect(m.gate.state == .paused)
    let r = m.handle(.resume, now: 10)
    #expect(sent(r).map { $0["type"] as? String } == ["resume"])
    #expect(m.gate.state == .dryRun)
    #expect(r.contains(where: { if case .showOverlay = $0 { true } else { false } }))
}

@Test func safetyPauseSendsPause() {
    var m = runningModel()
    #expect(sent(m.handle(.safety("secure input"), now: 6)).map { $0["type"] as? String } == ["pause"])
    #expect(m.gate.state == .paused)
}

@Test func focusChangeClearsTheOverlayAndAsksForARescan() {
    var m = runningModel()
    _ = withLayout(&m)
    _ = m.handle(.focusChanged, now: 8)
    let gen = m.focusGen
    _ = m.handle(.focusChanged, now: 8.2)
    #expect(m.handle(.focusSettled(gen: gen), now: 8.5).isEmpty)       // superseded by a newer change
    let e = m.handle(.focusSettled(gen: m.focusGen), now: 8.7)
    #expect(e.contains(.hideOverlay))
    #expect(sent(e).map { $0["type"] as? String } == ["rescan"])
    // the next scan shows targets again
    _ = m.handle(msg(#"{"type":"scan_request","scan_id":9,"settle_ms":0}"#), now: 9)
    _ = m.handle(.scanned(scanID: 9, snapshot: snapshot(), front: textEdit, screen: screen, why: nil), now: 9)
    let l = m.handle(msg(#"{"type":"layout","layout_id":8,"stage":"pick","targets":[{"slot":"b1","freq":15,"phase":0,"label":"Bold","kind":"button","rects":[{"x":0.25,"y":0.5,"w":0.1,"h":0.2}]}]}"#), now: 10)
    #expect(l.contains(where: { if case .showOverlay = $0 { true } else { false } }))
    // a focus change from before the scan does not clear the new targets
    #expect(m.handle(.focusSettled(gen: m.focusGen), now: 10).isEmpty)
}

@Test func focusChangeWhileTheOldWindowIsStillRankedAsksForARescan() {
    var m = runningModel()
    _ = m.handle(msg(#"{"type":"scan_request","scan_id":4,"settle_ms":0}"#), now: 6)
    _ = m.handle(.scanned(scanID: 4, snapshot: snapshot(), front: textEdit, screen: screen, why: nil), now: 6)
    _ = m.handle(msg(#"{"type":"layout","layout_id":2,"stage":"idle","targets":[]}"#), now: 6)
    _ = m.handle(.focusChanged, now: 7)
    let e = m.handle(.focusSettled(gen: m.focusGen), now: 7.5)
    #expect(m.snapshotStale)
    #expect(sent(e).map { $0["type"] as? String } == ["rescan"])
}

@Test func focusChangeWithAScanRequestPendingAsksForNothing() {
    var m = runningModel()
    _ = m.handle(msg(#"{"type":"scan_request","scan_id":4,"settle_ms":0}"#), now: 6)
    _ = m.handle(.scanned(scanID: 4, snapshot: snapshot(), front: textEdit, screen: screen, why: nil), now: 6)
    _ = m.handle(msg(#"{"type":"scan_request","scan_id":5,"settle_ms":300}"#), now: 6.5)
    _ = m.handle(.focusChanged, now: 7)
    #expect(sent(m.handle(.focusSettled(gen: m.focusGen), now: 7.5)).isEmpty)
}

@Test func deniedFrontAppTurnsControlOff() {
    var m = runningModel()
    _ = withLayout(&m)
    let e = m.handle(.front(FrontApp(name: "System Settings", bundleID: "com.apple.systempreferences", pid: 5)), now: 8)
    #expect(m.gate.state == .off && m.gate.reason.contains("System Settings is on the deny list"))
    #expect(e.contains(.hideOverlay))
    _ = m.handle(.front(FrontApp(name: "BrainPoint", bundleID: "ai.brainsync.brainpoint", pid: 6)), now: 8)
    #expect(m.gate.reason.contains("BrainPoint itself"))
}

@Test func gazeOnlyWithTheFakeBoard() {
    var m = runningModel()
    _ = withLayout(&m)
    #expect(sent(m.handle(.hover("b1"), now: 8)).first?["slot"] as? String == "b1")
    #expect(sent(m.handle(.hover("b1"), now: 8)).isEmpty)
    #expect(sent(m.handle(.hover(nil), now: 8)).first?["slot"] is NSNull)
    var real = runningModel(fake: false)
    #expect(sent(real.handle(.hover("b1"), now: 8)).isEmpty)
}

@Test func armedAndHeadsetChangesAreSpoken() {
    var m = runningModel(fake: false)
    #expect(m.gate.state == .armed)
    let e = m.handle(msg(#"{"type":"status","board":"no-data","ok":false,"text":"no samples"}"#), now: 9)
    #expect(e.contains(where: { if case .speak(let t) = $0 { t.hasPrefix("BrainPoint disarmed") } else { false } }))
    #expect(e.contains(where: { if case .speak(let t) = $0 { t.hasPrefix("Headset lost") } else { false } }))
    _ = m.handle(msg(#"{"type":"status","board":"streaming","ok":true,"text":""}"#), now: 10)
    let back = m.handle(.tick, now: 13)
    #expect(back.contains(.speak("BrainPoint armed.")))
}

@Test func closeForgetsServerState() {
    var m = runningModel()
    _ = withLayout(&m)
    let e = m.handle(.closed(code: 1006, reason: ""), now: 9)
    #expect(m.welcome == nil && m.layout == nil && m.phase == nil && m.status == nil)
    #expect(m.gate.state == .off)
    #expect(e.contains(.hideOverlay))
    #expect(sent(m.handle(.pause("Esc"), now: 9)).isEmpty)    // nothing goes out while closed
    #expect(m.userPaused)
}

@Test func welcomeAfterAPauseRepeatsIt() {
    var m = AppModel()
    _ = m.handle(.pause("menu"), now: 0)
    _ = m.handle(.opened, now: 0)
    let e = m.handle(msg(#"{"type":"welcome","protocol":3,"role":"controller","mode":"point","fake":true,"waveform":"sine","refresh_hz":60,"refresh_tolerance_hz":1,"freqs":[15],"max_slots":4,"detect":true}"#), now: 0)
    #expect(sent(e).map { $0["type"] as? String } == ["pause"])
}

@Test func screenScopeScanUsesTheScreenAsViewportAndForTheOverlay() throws {
    var m = runningModel()
    // the window sits on a second display, right of and above the primary's top edge
    let screenFrame = CGRect(x: 1512, y: -198, width: 1920, height: 1080)
    var snap = snapshot()
    snap.windowFrame = CGRect(x: 1700, y: -100, width: 800, height: 600)
    snap.items = [AXItem(id: 0, role: "AXButton", subrole: "", label: "Bold", frame: CGRect(x: 1720, y: -80, width: 40, height: 20), enabled: true, actions: ["AXPress"]),
                  AXItem(id: 1, role: "AXMenuBarItem", subrole: "", label: "File", frame: CGRect(x: 1572, y: -198, width: 40, height: 24), enabled: true, actions: ["AXPress"], kind: .menuBar)]
    snap.screenFrame = screenFrame
    _ = m.handle(msg(#"{"type":"scan_request","scan_id":4,"settle_ms":0}"#), now: 6)
    let e = m.handle(.scanned(scanID: 4, snapshot: snap, front: textEdit, screen: Viewport(w: 1920, h: 1080, dpr: 1), why: nil), now: 6)
    let scan = try #require(sent(e).first)
    #expect((scan["viewport"] as? [String: Double]) == ["w": 1920, "h": 1080, "dpr": 1])
    let hints = try #require(scan["hints"] as? [[String: Any]])
    #expect(hints.count == 2 && hints[0]["x"] as? Int == 208 && hints[0]["y"] as? Int == 118)
    #expect(hints[1]["x"] as? Int == 60 && hints[1]["y"] as? Int == 0 && hints[1]["role"] as? String == "AXMenuBarItem")
    #expect(e.contains(.note("scan 4: TextEdit \"Untitled\", 2 items (window 1, menuBar 1), 2 hints, walk 9 ms")))
    let l = m.handle(msg(#"{"type":"layout","layout_id":7,"stage":"pick","targets":[{"slot":"b1","freq":15,"phase":0,"label":"File","kind":"button","rects":[{"x":0.25,"y":0.5,"w":0.1,"h":0.2}]}]}"#), now: 7)
    guard case .showOverlay(let plan)? = l.last(where: { if case .showOverlay = $0 { true } else { false } }) else {
        Issue.record("no overlay: \(l)"); return
    }
    #expect(plan.targets[0].rect == CGRect(x: 1992, y: 342, width: 192, height: 216))
}
