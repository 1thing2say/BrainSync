import XCTest
@testable import BrainSyncKit

final class V3MessagesTests: XCTestCase {
    private func object(_ message: V3ClientMessage) throws -> [String: Any] {
        try XCTUnwrap(try JSONSerialization.jsonObject(with: try message.encoded()) as? [String: Any])
    }

    func testUnknownTypesNeverBreakTheApp() throws {
        XCTAssertEqual(try V3ServerMessage.decode(#"{"type":"scan_request","scan_id":4,"settle_ms":800}"#),
                       .unknown("scan_request"))
        XCTAssertEqual(try V3ServerMessage.decode(#"{"type":"hologram","x":1}"#), .unknown("hologram"))
        XCTAssertEqual(try V3ServerMessage.decode(#"{"type":"ping"}"#), .ping)
        XCTAssertThrowsError(try V3ServerMessage.decode("not json"))
        XCTAssertThrowsError(try V3ServerMessage.decode(#"{"no_type":1}"#))
        XCTAssertThrowsError(try V3ServerMessage.decode(#"[1,2]"#))
    }

    func testAnOlderServersWelcomeGetsTheDefaults() throws {
        let text = #"{"type":"welcome","protocol":3,"role":"controller","mode":"slots","refresh_hz":60.0,"#
            + #""freqs":[15.0,17.0],"max_slots":2,"new_field":{"a":1}}"#
        guard case .welcome(let w) = try V3ServerMessage.decode(text) else { return XCTFail() }
        XCTAssertEqual(w.paradigm, .freq) // no paradigm: freq
        XCTAssertEqual(w.refreshRates, [60])
        XCTAssertEqual(w.refreshToleranceHz, 1)
        XCTAssertNil(w.code)
        XCTAssertFalse(w.fake)
    }

    func testAnUnknownPhaseNeverFlickers() throws {
        guard case .phase(let p) = try V3ServerMessage.decode(#"{"type":"phase","phase":"dance","flicker":true,"text":""}"#)
        else { return XCTFail() }
        XCTAssertNil(p.name)
        XCTAssertFalse(p.flicker)
        guard case .phase(let run) = try V3ServerMessage.decode(#"{"type":"phase","phase":"run","flicker":true,"seconds":null,"text":""}"#)
        else { return XCTFail() }
        XCTAssertEqual(run.name, .run)
        XCTAssertTrue(run.flicker)
        XCTAssertNil(run.seconds)
    }

    func testScoreKeysAreSlotNamesNotRewritten() throws {
        let text = #"{"type":"decision","layout_id":3,"scores":{"myYes":0.4,"no_way":0.1},"raw":"myYes","committed":null,"lead":0.3,"changed":false}"#
        guard case .decision(let d) = try V3ServerMessage.decode(text) else { return XCTFail() }
        XCTAssertEqual(Set(d.scores.keys), ["myYes", "no_way"])
        XCTAssertNil(d.committed)
        XCTAssertEqual(d.raw, "myYes")
    }

    func testHelloOnTheWire() throws {
        var hello = V3Hello.controller(slots: ["t0", "t1", "t2"], refreshHz: 60.02,
                                       viewport: V3Viewport(w: 390, h: 844, dpr: 3))
        hello.token = "tok"
        hello.client.instance = "abc"
        let o = try object(.hello(hello))
        XCTAssertEqual(o["type"] as? String, "hello")
        XCTAssertEqual(o["protocol"] as? Int, 3)
        XCTAssertEqual(o["token"] as? String, "tok")
        XCTAssertEqual(o["role"] as? String, "controller")
        XCTAssertEqual(o["takeover"] as? Bool, false)
        XCTAssertEqual(o["mode"] as? String, "slots")
        XCTAssertEqual(o["slots"] as? [String], ["t0", "t1", "t2"])
        XCTAssertEqual(o["refresh_hz"] as? Double, 60.02)
        XCTAssertEqual(o["accepted"] as? Bool, true)
        XCTAssertEqual((o["viewport"] as? [String: Double])?["dpr"], 3)
        let client = try XCTUnwrap(o["client"] as? [String: String])
        XCTAssertEqual(client, ["name": "brainsync-ios", "kind": "ios", "instance": "abc"])
        XCTAssertNil(o["refreshHz"])

        let viewer = try object(.hello(V3Hello.viewer()))
        XCTAssertEqual(viewer["role"] as? String, "viewer")
        XCTAssertTrue(viewer["slots"] is NSNull)
        XCTAssertTrue(viewer["token"] is NSNull) // --no-auth: no token
    }

    func testClientMessagesOnTheWire() throws {
        XCTAssertEqual(try V3ClientMessage.cycle(layoutId: 7, n: 12, tClient: 5.5).encodedString(),
                       #"{"layout_id":7,"n":12,"t_client":5.5,"type":"cycle"}"#)
        XCTAssertEqual(try V3ClientMessage.onset(layoutId: 7, tClient: 1.25).encodedString(),
                       #"{"layout_id":7,"t_client":1.25,"type":"onset"}"#)
        XCTAssertEqual(try V3ClientMessage.frames(refreshHz: 60, dropped: 0).encodedString(),
                       #"{"dropped":0,"refresh_hz":60,"type":"frames"}"#)
        XCTAssertEqual(try V3ClientMessage.ping(pingId: 1, tClient: 2.5).encodedString(),
                       #"{"ping_id":1,"t_client":2.5,"type":"ping"}"#)
        XCTAssertEqual(try V3ClientMessage.gaze(slot: nil).encodedString(), #"{"slot":null,"type":"gaze"}"#)
        XCTAssertEqual(try V3ClientMessage.gaze(slot: "t2").encodedString(), #"{"slot":"t2","type":"gaze"}"#)
        XCTAssertEqual(try V3ClientMessage.pause.encodedString(), #"{"type":"pause"}"#)
        XCTAssertEqual(try V3ClientMessage.resume.encodedString(), #"{"type":"resume"}"#)
        XCTAssertEqual(try V3ClientMessage.bye.encodedString(), #"{"type":"bye"}"#)
        for m: V3ClientMessage in [.pause, .resume, .bye, .gaze(slot: nil), .cycle(layoutId: 1, n: 2, tClient: 3)] {
            XCTAssertEqual(try V3ClientMessage.decode(try m.encodedString()), m)
        }
        XCTAssertThrowsError(try V3ClientMessage.decode(#"{"type":"scan"}"#))
    }
}
