import Testing
@testable import BrainPointCore

/// Every condition met, real board: armed.
private func ready() -> GateInputs {
    var i = GateInputs()
    i.serverConnected = true; i.isController = true
    i.boardState = "streaming"; i.boardOk = true; i.fakeBoard = false; i.streamingSince = 0
    i.axTrusted = true; i.warningAccepted = true; i.phase = "run"
    return i
}

@Test func armedWhenEverythingHolds() {
    let d = ControlGate.decide(ready(), now: 10)
    #expect(d.state == .armed && d.mayFlicker && d.mayPress)
    #expect(d.headsetLine == "Headset streaming")
    #expect(d.reason.hasPrefix("Armed"))
}

@Test func fakeBoardIsDryRunUnlessAllowed() {
    var i = ready(); i.fakeBoard = true
    let d = ControlGate.decide(i, now: 10)
    #expect(d.state == .dryRun && d.mayFlicker && !d.mayPress)
    #expect(d.headsetLine == "SIMULATED board")
    #expect(d.reason.contains("dry run"))
    i.allowFakeControl = true
    #expect(ControlGate.decide(i, now: 10).state == .armed)
}

@Test func eachFailingConditionNamesItself() {
    typealias Case = (String, (inout GateInputs) -> Void, ControlState, String)
    let cases: [Case] = [
        ("server", { $0.serverConnected = false }, .off, "Not connected"),
        ("viewer", { $0.isController = false }, .off, "viewer"),
        ("board", { $0.boardState = "no-data" }, .off, "not streaming (board no-data)"),
        ("ok", { $0.boardOk = false }, .off, "reports a problem"),
        ("settle", { $0.streamingSince = 9.5 }, .off, "just started streaming"),
        ("never", { $0.streamingSince = nil }, .off, "just started streaming"),
        ("ax", { $0.axTrusted = false }, .off, "Accessibility"),
        ("warning", { $0.warningAccepted = false }, .off, "photosensitivity"),
        ("safety", { $0.safetyPause = "screen locked" }, .paused, "Paused for safety: screen locked."),
        ("user", { $0.userPaused = true }, .paused, "Resume"),
        ("deny", { $0.frontAppDenied = "System Settings" }, .off, "System Settings is on the deny list"),
        ("wait", { $0.phase = "wait"; $0.serverNote = "detect_unavailable: no key." }, .off, "waiting (phase wait): detect_unavailable: no key."),
        ("stop", { $0.phase = "stop" }, .off, "shutting down"),
        ("calibrate", { $0.phase = "calibrate" }, .calibrating, "Calibrating"),
    ]
    for (name, mutate, state, text) in cases {
        var i = ready(); mutate(&i)
        let d = ControlGate.decide(i, now: 10)
        #expect(d.state == state, "\(name): \(d.state)")
        #expect(d.reason.contains(text), "\(name): \(d.reason)")
        #expect(!d.mayPress, "\(name)")
        #expect(!d.mayFlicker, "\(name)")
        #expect(d.reason.hasSuffix("."), "\(name): one sentence")
    }
}

@Test func aServerPauseHoldsTheFlickerButNotTheClickInFlight() {
    // Point mode sends phase "pause" just before its click: the press must still go through.
    var i = ready(); i.phase = "pause"
    var d = ControlGate.decide(i, now: 10)
    #expect(d.state == .paused && !d.mayFlicker && d.mayPress && !d.dryRun)
    #expect(d.reason.contains("server paused"))
    i.fakeBoard = true                                     // SIMULATED board: still a dry run
    d = ControlGate.decide(i, now: 10)
    #expect(d.state == .paused && !d.mayPress && d.dryRun)
    i.fakeBoard = false; i.userPaused = true               // a local pause blocks the press too
    d = ControlGate.decide(i, now: 10)
    #expect(!d.mayPress && d.reason.contains("Resume"))
}

@Test func firstFailingConditionWins() {
    var i = ready()
    i.axTrusted = false; i.userPaused = true; i.phase = "wait"; i.fakeBoard = true
    #expect(ControlGate.decide(i, now: 10).reason.contains("Accessibility"))
    i.boardState = "connecting"
    #expect(ControlGate.decide(i, now: 10).reason.contains("board connecting"))
    i.serverConnected = false
    #expect(ControlGate.decide(i, now: 10).reason.contains("Not connected"))
}

@Test func headsetLines() {
    var i = ready()
    #expect(ControlGate.headsetLine(i, now: 10) == "Headset streaming")
    i.streamingSince = 9
    #expect(ControlGate.headsetLine(i, now: 10) == "Headset streaming (settling)")
    i.boardState = "no-data"
    #expect(ControlGate.headsetLine(i, now: 10) == "Headset disconnected (no data)")
    i.fakeBoard = true; i.boardState = "connecting"
    #expect(ControlGate.headsetLine(i, now: 10) == "SIMULATED board connecting")
    i.serverConnected = false
    #expect(ControlGate.headsetLine(i, now: 10) == "Headset unknown (no server)")
}
