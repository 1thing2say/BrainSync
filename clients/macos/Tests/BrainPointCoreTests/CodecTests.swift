import Foundation
import Testing
@testable import BrainPointCore

private func roundTrip(_ m: [String: Any]) throws -> [String: Any] {
    let text = try #require(Codec.encode(m))
    return try #require(Codec.object(text))
}

@Test func helloShape() throws {
    let h = try roundTrip(Codec.hello(token: "secret", instance: "abc", version: "0.1.0", refreshHz: 59.98,
                                      viewport: Viewport(w: 1512, h: 982, dpr: 2)))
    #expect(h["type"] as? String == "hello")
    #expect(h["protocol"] as? Int == 3)
    #expect(h["role"] as? String == "controller")
    #expect(h["mode"] as? String == "point")
    #expect(h["slots"] is NSNull)
    #expect(h["accepted"] as? Bool == true)
    #expect(h["takeover"] as? Bool == false)
    #expect(h["token"] as? String == "secret")
    let c = try #require(h["client"] as? [String: String])
    #expect(c == ["name": "brainpoint-desktop", "kind": "desktop", "version": "0.1.0", "instance": "abc"])
    #expect((h["viewport"] as? [String: Double]) == ["w": 1512, "h": 982, "dpr": 2])
    let anon = try roundTrip(Codec.hello(token: nil, instance: "x", version: "0", refreshHz: 60, viewport: Viewport(w: 1, h: 1, dpr: 1)))
    #expect(anon["token"] is NSNull)
}

@Test func scanAXShape() throws {
    let hints: [[String: Any]] = [["id": 0, "x": 1, "y": 2, "w": 3, "h": 4, "role": "AXButton", "text": "OK",
                                   "tag": "", "actions": ["AXPress"], "enabled": true]]
    let s = try roundTrip(Codec.scanAX(scanID: 4, app: FrontApp(name: "TextEdit", bundleID: "com.apple.TextEdit", pid: 77),
                                       windowTitle: "Untitled", viewport: Viewport(w: 800, h: 600, dpr: 2), hints: hints))
    #expect(s["type"] as? String == "scan")
    #expect(s["source"] as? String == "ax")
    #expect(s["scan_id"] as? Int == 4)
    #expect(s["image"] == nil)
    #expect((s["app"] as? [String: Any])?["bundle_id"] as? String == "com.apple.TextEdit")
    #expect((s["app"] as? [String: Any])?["pid"] as? Int == 77)
    #expect((s["window"] as? [String: String]) == ["title": "Untitled"])
    #expect((s["hints"] as? [[String: Any]])?.first?["enabled"] as? Bool == true)
}

@Test func smallClientMessages() throws {
    #expect(try roundTrip(Codec.onset(layoutID: 7, tClient: 1792809201.512))["layout_id"] as? Int == 7)
    let f = try roundTrip(Codec.frames(refreshHz: 59.98765, dropped: -3))
    #expect(f["refresh_hz"] as? Double == 59.988 && f["dropped"] as? Int == 0)
    #expect(try roundTrip(Codec.ping(pingID: 3, tClient: 1.5))["ping_id"] as? Int == 3)
    for (m, t) in [(Codec.pause(), "pause"), (Codec.resume(), "resume"), (Codec.bye(), "bye")] {
        #expect(try roundTrip(m) as NSDictionary == ["type": t] as NSDictionary)
    }
    #expect(try roundTrip(Codec.gaze(slot: "b1"))["slot"] as? String == "b1")
    #expect(try roundTrip(Codec.gaze(slot: nil))["slot"] is NSNull)
    let c = try roundTrip(Codec.clicked(clickID: 2, ok: false, method: "none", detail: "dry run: would press OK"))
    #expect(c["ok"] as? Bool == false && c["detail"] as? String == "dry run: would press OK" && c["click_id"] as? Int == 2)
}

@Test func encodeRefusesNaN() {
    #expect(Codec.encode(Codec.onset(layoutID: 1, tClient: .nan)) == nil)
    #expect(Codec.encode(Codec.frames(refreshHz: .infinity, dropped: 0)) == nil)
}

private func parsed(_ text: String) throws -> ServerMessage {
    switch Codec.parse(text) {
    case .success(let m): return m
    case .failure(let e): Issue.record("parse failed: \(e)"); throw e
    }
}

@Test func parsesServerMessages() throws {
    let w = try parsed(#"{"type":"welcome","protocol":3,"role":"controller","mode":"point","server":{"name":"brainsync","version":"0.1.0"},"fake":true,"waveform":"sine","refresh_hz":60.0,"refresh_tolerance_hz":1.0,"refresh_rates":[60,90],"freqs":[15,17,12,20],"max_slots":4,"detect":false}"#)
    guard case .welcome(let wm) = w else { Issue.record("not welcome"); return }
    #expect(wm.fake && !wm.detect && wm.freqs == [15, 17, 12, 20] && wm.refreshRates == [60, 90] && wm.serverVersion == "0.1.0")

    #expect(try parsed(#"{"type":"phase","phase":"calibrate","flicker":false,"seconds":10.0,"text":"relax"}"#)
            == .phase(PhaseMsg(phase: "calibrate", flicker: false, seconds: 10, text: "relax")))
    #expect(try parsed(#"{"type":"phase","phase":"run","flicker":true,"seconds":null,"text":""}"#)
            == .phase(PhaseMsg(phase: "run", flicker: true)))

    let l = try parsed(#"{"type":"layout","layout_id":7,"stage":"pick","targets":[{"slot":"b1","freq":15.0,"phase":0.0,"label":"Sign in","kind":"button","rects":[{"x":0.012,"y":0.04,"w":0.09,"h":0.12}]},{"slot":"more","freq":20,"phase":0,"label":"More","kind":"more","rects":[{"x":0.86,"y":0.84,"w":0.12,"h":0.14}]}]}"#)
    #expect(l == .layout(LayoutMsg(layoutID: 7, stage: "pick", targets: [
        LayoutTarget(slot: "b1", freq: 15, phase: 0, label: "Sign in", kind: "button", rects: [NormRect(x: 0.012, y: 0.04, w: 0.09, h: 0.12)]),
        LayoutTarget(slot: "more", freq: 20, phase: 0, label: "More", kind: "more", rects: [NormRect(x: 0.86, y: 0.84, w: 0.12, h: 0.14)])])))
    #expect(try parsed(#"{"type":"layout","layout_id":8,"stage":"idle","targets":[]}"#) == .layout(LayoutMsg(layoutID: 8, stage: "idle", targets: [])))

    guard case .selection(let s) = try parsed(#"{"type":"selection","selection_id":12,"layout_id":7,"slot":"b1","t_server":1792809204.9,"score":1.8,"scores":{"b1":1.8,"more":0.4}}"#) else { Issue.record("selection"); return }
    #expect(s.slot == "b1" && s.score == 1.8 && s.scores == ["b1": 1.8, "more": 0.4])

    #expect(try parsed(#"{"type":"scan_request","scan_id":4,"settle_ms":800}"#) == .scanRequest(ScanRequestMsg(scanID: 4, settleMs: 800)))
    #expect(try parsed(#"{"type":"scan_request","scan_id":5,"settle_ms":999999}"#) == .scanRequest(ScanRequestMsg(scanID: 5, settleMs: 10000)))

    guard case .scanResult(let r) = try parsed(#"{"type":"scan_result","scan_id":4,"ok":true,"ms":12,"n_elements":23,"n_candidates":9,"summary":"Login page","elements":[],"cached":true}"#) else { Issue.record("scan_result"); return }
    #expect(r.ok && r.cached && r.nCandidates == 9 && r.ms == 12 && r.summary == "Login page")

    #expect(try parsed(#"{"type":"click","click_id":2,"x":0.057,"y":0.1,"element":{"id":3,"label":"Sign in","role":"button","hint_id":0,"risky":false}}"#)
            == .click(ClickMsg(clickID: 2, x: 0.057, y: 0.1, element: ClickElement(id: 3, label: "Sign in", role: "button", hintID: 0, risky: false))))
    guard case .click(let noHint) = try parsed(#"{"type":"click","click_id":3,"x":0.5,"y":0.5,"element":{"label":"X","hint_id":null}}"#) else { Issue.record("click"); return }
    #expect(noHint.element.hintID == nil)

    #expect(try parsed(#"{"type":"status","board":"streaming","ok":true,"text":"posterior RMS 14 uV","rtt_ms":38}"#)
            == .status(StatusMsg(board: "streaming", ok: true, text: "posterior RMS 14 uV", rttMs: 38)))
    #expect(try parsed(#"{"type":"status","board":"no-data","ok":false,"text":"","rtt_ms":null}"#) == .status(StatusMsg(board: "no-data", ok: false)))
    #expect(try parsed(#"{"type":"error","code":"detect_unavailable","message":"no key","fatal":false}"#)
            == .error(ErrorMsg(code: "detect_unavailable", message: "no key", fatal: false)))
    guard case .pong(let p) = try parsed(#"{"type":"pong","ping_id":3,"t_client":1792809201.0,"t_server":1792809201.02}"#) else { Issue.record("pong"); return }
    #expect(p.pingID == 3 && p.tServer == 1792809201.02)
    #expect(try parsed(#"{"type":"ping"}"#) == .ping)
    #expect(try parsed(#"{"type":"something_new","x":1}"#) == .unknown("something_new"))
}

@Test func parseRejectsMalformedFrames() {
    for bad in ["not json", "[1,2]", #"{"no":"type"}"#, #"{"type":5}"#,
                #"{"type":"layout","layout_id":"7","targets":[]}"#,
                #"{"type":"layout","layout_id":7,"targets":[{"slot":"b1","freq":true,"rects":[]}]}"#,
                #"{"type":"scan_request","scan_id":1.5}"#,
                #"{"type":"click","click_id":1,"x":"0.5","y":0.5}"#] {
        if case .success(let m) = Codec.parse(bad) { Issue.record("parsed \(bad) as \(m)") }
    }
    // booleans are not numbers and numbers are not booleans
    if case .success(let m) = Codec.parse(#"{"type":"status","board":"streaming","ok":1}"#), case .status(let s) = m {
        #expect(!s.ok)
    } else { Issue.record("status") }
}
