import CoreGraphics
import Foundation
import Testing
@testable import BrainPointCore

// The c-VEP standard (docs/CVEP-STANDARD.md) on the desktop: the code, the frame rule, the
// confidence text, cycle reports, the protocol messages and the app model in code mode.

private let standard = "000001111110101011001101110110100100111000101111001010001100001"

private struct Fixture: Decodable {
    struct Bar: Decodable { let value: Double; let bar: String }
    let bits: String
    let lag_frames: Int
    let cycle_frames: Int
    let n_targets: Int
    let train_cycles: Int
    let frames: Int
    let targets: [[Int]]
    let bars: [Bar]
}

private func loadFixture() throws -> Fixture {
    let url = try #require(Bundle.module.url(forResource: "cvep_fixture", withExtension: "json", subdirectory: "Fixtures"))
    return try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
}

@Test func theStandardBitsAreTheM63Sequence() throws {
    #expect(CodeVEP.standardBits.map(String.init).joined() == standard)
    #expect(CodeVEP.standardBits.count == 63 && CodeVEP.lagFrames == 21 && CodeVEP.nTargets == 3)
    let f = try loadFixture()
    #expect(f.bits == standard, "the Python fixture holds brainsync.vep.live.BITS")
    #expect(f.lag_frames == CodeVEP.lagFrames && f.cycle_frames == CodeVEP.cycleFrames)
    #expect(f.n_targets == CodeVEP.nTargets && f.train_cycles == CodeVEP.trainCycles)
}

@Test func frameRuleMatchesPython() throws {
    let f = try loadFixture()
    #expect(f.targets.count == 3 && f.targets.allSatisfy { $0.count == f.frames })
    for (k, row) in f.targets.enumerated() {
        for (n, want) in row.enumerated() {
            #expect(Int(CodeVEP.bit(lag: k * CodeVEP.lagFrames, frame: n)) == want, "target \(k) frame \(n)")
        }
    }
    // negative frames wrap like Python's %
    #expect(CodeVEP.bit(lag: 21, frame: 0) == CodeVEP.standardBits[42])
}

@Test func barsMatchPython() throws {
    for b in try loadFixture().bars {
        #expect(CodeVEP.bar(b.value) == b.bar, "bar(\(b.value))")
    }
    #expect(CodeVEP.bar(.nan) == "░░░░░░░░")
}

@Test func confidenceTextFollowsTheStandard() {
    #expect(CodeVEP.labelLine("YES", score: 0.42, best: true, width: 4) == "YES  ███░░░░░ +0.42 <")
    #expect(CodeVEP.labelLine("NO", score: 0.11, best: false, width: 4) == "NO   █░░░░░░░ +0.11")
    #expect(CodeVEP.labelLine("HELP", score: -0.03, best: false) == "HELP ░░░░░░░░ -0.03")
    #expect(CodeVEP.labelLine("X", score: .nan, best: false) == "X ░░░░░░░░   -  ")
    #expect(CodeVEP.statusLine(lead: 0.31, committed: "YES", changed: false) == "lead 0.31   choice: YES")
    #expect(CodeVEP.statusLine(lead: 0.31, committed: "YES", changed: true) == "lead 0.31   choice: YES *")
    #expect(CodeVEP.statusLine(lead: nil, committed: nil, changed: false) == "lead  -     no choice yet")
    #expect(CodeVEP.trainedLine(accuracy: 0.83, chance: 0.333) == "template ready: 83 % on held-out cycles (chance 33 %)")
    #expect(CodeVEP.trainedLine(accuracy: nil, chance: nil) == "template ready")
    #expect(CodeVEP.trainLine(text: "", cueLabel: "Look here", cycle: 12, of: 30) == "Look at the Look here patch   cycle 12/30")
    #expect(CodeVEP.trainLine(text: "Look at the YES square", cueLabel: nil, cycle: nil, of: 30)
            == "Look at the YES square   cycle 0/30")
}

@Test func cyclesStartEvery63Frames() {
    #expect([0, 62, 63, 126, 127].map { CodeVEP.cycleStart($0) } == [true, false, true, true, false])
    #expect([0, 62, 63, 130, -1].map { CodeVEP.cycleIndex($0) } == [0, 0, 1, 2, -1])
}

@Test func cycleCounterReportsEachCycleOnceAndCatchesADroppedFirstFrame() {
    var c = CycleCounter()
    let p = 1.0 / 60
    var got: [(Int, Double)] = []
    for n in 0...130 where n != 63 {                 // frame 63 was dropped
        if let d = c.due(layoutID: 7, t0: 10, frame: n, t: 10 + Double(n) * p + 0.0001, period: p, cycleFrames: 63) {
            got.append((d.n, d.t))
        }
    }
    #expect(got.map(\.0) == [0, 1, 2])
    #expect(abs(got[0].1 - 10.0001) < 1e-9, "on time: the flip's own time")
    #expect(abs(got[1].1 - (10 + 63 * p)) < 1e-9, "dropped: the time the first frame was due")
    #expect(abs(got[2].1 - (10 + 126 * p + 0.0001)) < 1e-9)
    // a new layout (or a restart of the flicker) counts from 0 again
    #expect(c.due(layoutID: 8, t0: 20, frame: 0, t: 20, period: p, cycleFrames: 63)?.n == 0)
    #expect(c.due(layoutID: 8, t0: 20, frame: 1, t: 20, period: p, cycleFrames: 63) == nil)
    #expect(c.due(layoutID: 8, t0: 30, frame: 0, t: 30, period: p, cycleFrames: 63)?.n == 0)
}

// MARK: protocol

private let codeWelcome = ##"{"type":"welcome","protocol":3,"role":"controller","mode":"point","fake":true,"waveform":"sine","refresh_hz":60,"refresh_tolerance_hz":1,"freqs":[],"max_slots":3,"detect":true,"paradigm":"code","code":{"bits":[0,0,0,0,0,1,1,1,1,1,1,0,1,0,1,0,1,1,0,0,1,1,0,1,1,1,0,1,1,0,1,0,0,1,0,0,1,1,1,0,0,0,1,0,1,1,1,1,0,0,1,0,1,0,0,0,1,1,0,0,0,0,1],"lag_frames":21,"cycle_frames":63,"train_cycles":30,"fill":"#ffffff"}}"##

private func parse(_ s: String) -> ServerMessage {
    guard case .success(let m) = Codec.parse(s) else { fatalError("bad fixture \(s)") }
    return m
}

@Test func parsesACodeWelcome() throws {
    guard case .welcome(let w) = parse(codeWelcome) else { Issue.record("not a welcome"); return }
    #expect(w.paradigm == "code")
    let c = try #require(w.code)
    #expect(c.bits == CodeVEP.standardBits && c.lagFrames == 21 && c.cycleFrames == 63 && c.trainCycles == 30)
    #expect(c.fill == "#ffffff" && c.bitsFromServer && c.isStandard)
    #expect(c.fillRGB == (1, 1, 1))
    guard case .welcome(let f) = parse(#"{"type":"welcome","role":"controller","refresh_hz":60,"freqs":[15,17]}"#) else { return }
    #expect(f.paradigm == nil && f.code == nil)
    guard case .welcome(let bad) = parse(#"{"type":"welcome","role":"controller","refresh_hz":60,"paradigm":"code","code":{"bits":[0,2],"lag_frames":-3,"fill":"red"}}"#) else { return }
    let b = try #require(bad.code)
    #expect(b.bits == CodeVEP.standardBits && !b.bitsFromServer && b.lagFrames == 21 && b.fill == "#ffffff")
}

@Test func parsesCodeLayoutsPhasesAndConfidence() {
    let lay = parse(#"{"type":"layout","layout_id":7,"stage":"pick","targets":[{"slot":"b1","freq":null,"lag":0,"phase":0,"label":"Sign in","kind":"button","rects":[{"x":0.1,"y":0.1,"w":0.1,"h":0.1}]},{"slot":"more","lag":42,"label":"More","kind":"more","rects":[]}]}"#)
    guard case .layout(let l) = lay else { Issue.record("not a layout"); return }
    #expect(l.targets.map(\.lag) == [0, 42] && l.targets.map(\.freq) == [0, 0])
    // a freq target still needs its freq
    if case .success = Codec.parse(#"{"type":"layout","layout_id":1,"targets":[{"slot":"b1","freq":null}]}"#) {
        Issue.record("a target with neither freq nor lag must not parse")
    }
    #expect(parse(#"{"type":"phase","phase":"train","flicker":true,"cue":"b1","cycles":30,"text":"Look at the YES square"}"#)
            == .phase(PhaseMsg(phase: "train", flicker: true, text: "Look at the YES square", cue: "b1", cycles: 30)))
    #expect(parse(#"{"type":"train","cycle":12,"of":30}"#) == .train(TrainMsg(cycle: 12, of: 30)))
    #expect(parse(#"{"type":"trained","accuracy":0.83,"chance":0.333,"cycles":30}"#)
            == .trained(TrainedMsg(accuracy: 0.83, chance: 0.333, cycles: 30)))
    #expect(parse(#"{"type":"decision","layout_id":7,"scores":{"yes":0.42,"no":0.11,"help":-0.03},"raw":"yes","committed":null,"lead":0.31,"changed":false}"#)
            == .decision(DecisionMsg(layoutID: 7, scores: ["yes": 0.42, "no": 0.11, "help": -0.03], raw: "yes",
                                     committed: nil, lead: 0.31, changed: false)))
}

@Test func encodesACycleReport() throws {
    let text = try #require(Codec.encode(Codec.cycle(layoutID: 7, n: 12, tClient: 1792809201.512)))
    let o = try #require(Codec.object(text))
    #expect(o["type"] as? String == "cycle" && o["layout_id"] as? Int == 7 && o["n"] as? Int == 12)
    #expect(o["t_client"] as? Double == 1792809201.512)
}

// MARK: app model in code mode

private let display = CGRect(x: 0, y: 0, width: 1512, height: 982)
private let textEdit = FrontApp(name: "TextEdit", bundleID: "com.apple.TextEdit", pid: 77)

private func ev(_ json: String) -> AppEvent { .message(parse(json)) }

private func sentObjects(_ effects: [AppEffect]) -> [[String: Any]] {
    effects.compactMap { if case .send(let t) = $0 { return Codec.object(t) } else { return nil } }
}

private let trainLayout = #"{"type":"layout","layout_id":3,"stage":"train","targets":[{"slot":"b1","freq":null,"lag":0,"label":"Look here","kind":"button","rects":[{"x":0.2,"y":0.4,"w":0.12,"h":0.2}]},{"slot":"b2","freq":null,"lag":21,"label":"Second","kind":"button","rects":[{"x":0.44,"y":0.4,"w":0.12,"h":0.2}]},{"slot":"more","freq":null,"lag":42,"label":"More","kind":"more","rects":[{"x":0.68,"y":0.4,"w":0.12,"h":0.2}]}]}"#

private func codeModel() -> AppModel {
    var m = AppModel()
    _ = m.handle(.axTrusted(true), now: 0)
    _ = m.handle(.warningAccepted, now: 0)
    _ = m.handle(.front(textEdit), now: 0)
    _ = m.handle(.connecting, now: 0)
    _ = m.handle(.display(display), now: 0)
    _ = m.handle(.opened, now: 0)
    _ = m.handle(ev(codeWelcome), now: 0)
    _ = m.handle(ev(#"{"type":"status","board":"streaming","ok":true,"text":""}"#), now: 1)
    _ = m.handle(.tick, now: 5)
    return m
}

@Test func trainingShowsTheRowOnTheMainScreenWithTheCueAndCountsCycles() throws {
    var m = codeModel()
    _ = m.handle(ev(trainLayout), now: 5)
    let e = m.handle(ev(#"{"type":"phase","phase":"train","flicker":true,"cue":"b1","cycles":30,"text":""}"#), now: 5)
    #expect(m.gate.state == .training && m.gate.mayFlicker && !m.gate.mayPress)
    let plan = try #require(m.overlay)
    #expect(e.contains(.showOverlay(plan)))
    #expect(plan.flicker && plan.code == .standard)
    #expect(plan.targets.map(\.lag) == [0, 21, 42])
    #expect(plan.targets.map(\.cue) == [true, false, false])
    let r = plan.targets[0].rect, want = [0.2 * 1512, 0.4 * 982, 0.12 * 1512, 0.2 * 982]
    #expect(zip([r.minX, r.minY, r.width, r.height].map(Double.init), want).allSatisfy { abs($0 - $1) < 1e-9 },
            "the training row is placed on the main screen")
    #expect(plan.status == "Look at the Look here patch   cycle 0/30")
    _ = m.handle(ev(#"{"type":"train","cycle":12,"of":30}"#), now: 6)
    #expect(m.overlay?.status == "Look at the Look here patch   cycle 12/30")
    let sent = sentObjects(m.handle(.cycle(layoutID: 3, n: 4, tClient: 1792809201.5), now: 6))
    #expect(sent.count == 1 && sent[0]["type"] as? String == "cycle" && sent[0]["n"] as? Int == 4)
    #expect(sentObjects(m.handle(.cycle(layoutID: 2, n: 5, tClient: 1), now: 6)).isEmpty, "another layout's cycle")
    #expect(m.cyclesSent == 1)
    _ = m.handle(ev(#"{"type":"trained","accuracy":0.83,"chance":0.333,"cycles":30}"#), now: 38)
    _ = m.handle(ev(#"{"type":"phase","phase":"run","flicker":true,"text":""}"#), now: 38)
    #expect(m.gate.state == .dryRun)
    #expect(m.overlay?.targets.allSatisfy { !$0.cue } == true)
    #expect(m.overlay?.status == "template ready: 83 % on held-out cycles (chance 33 %)")
}

@Test func decisionsBecomeLabelLinesAndTheStatusLine() throws {
    var m = codeModel()
    _ = m.handle(ev(trainLayout), now: 5)
    _ = m.handle(ev(#"{"type":"phase","phase":"run","flicker":true,"text":""}"#), now: 5)
    _ = m.handle(ev(#"{"type":"decision","layout_id":9,"scores":{"b1":0.9},"raw":"b1","committed":"b1","lead":0.5,"changed":true}"#), now: 6)
    #expect(m.decision == nil, "a decision of another layout is ignored")
    _ = m.handle(ev(#"{"type":"decision","layout_id":3,"scores":{"b1":0.42,"b2":0.11,"more":-0.03},"raw":"b1","committed":"b1","lead":0.31,"changed":true}"#), now: 6)
    let plan = try #require(m.overlay)
    #expect(plan.targets.map(\.label) == ["Look here ███░░░░░ +0.42 <", "Second █░░░░░░░ +0.11", "More ░░░░░░░░ -0.03"])
    #expect(plan.status == "lead 0.31   choice: Look here *")
    // the relabelled plan keeps the same stimulus, so the overlay does not restart its frames
    #expect(zip(plan.targets, plan.targets).allSatisfy { $0.sameStimulus($1) })
    _ = m.handle(ev(trainLayout.replacingOccurrences(of: #""layout_id":3"#, with: #""layout_id":4"#)), now: 7)
    #expect(m.decision == nil && m.overlay?.targets.first?.label == "Look here")
}

@Test func freqServersKeepTheirTargetsAndSendNoCycles() {
    var m = AppModel()
    _ = m.handle(.axTrusted(true), now: 0)
    _ = m.handle(.warningAccepted, now: 0)
    _ = m.handle(.opened, now: 0)
    _ = m.handle(ev(#"{"type":"welcome","role":"controller","fake":true,"refresh_hz":60,"freqs":[15,17,12,20]}"#), now: 0)
    #expect(m.code == nil)
    #expect(sentObjects(m.handle(.cycle(layoutID: 1, n: 0, tClient: 1), now: 1)).isEmpty)
    #expect(m.codeStatus == "")
}

@Test func labelOnlyChangesKeepTheStimulus() {
    let a = OverlayTarget(slot: "b1", label: "Sign in", kind: "button", freq: 0, phase: 0,
                          rect: CGRect(x: 1, y: 2, width: 3, height: 4), lag: 21)
    var b = a
    b.label = "Sign in ███░░░░░ +0.42 <"
    b.cue = true
    #expect(a.sameStimulus(b) && a != b)
    b.lag = 42
    #expect(!a.sameStimulus(b))
}
