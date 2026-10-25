import XCTest
@testable import BrainSyncKit

final class MessageTests: XCTestCase {
    func testUnknownType() throws {
        XCTAssertEqual(try ServerMessage.decode(#"{"type": "confetti", "x": 1}"#), .unknown("confetti"))
    }

    func testNotAMessageThrows() {
        XCTAssertThrowsError(try ServerMessage.decode("[1, 2]"))
        XCTAssertThrowsError(try ServerMessage.decode(#"{"no_type": true}"#))
    }

    func testUnknownFieldsIgnored() throws {
        let m = try ServerMessage.decode(#"{"type": "trial", "trial_id": 3, "phase": "flicker", "new": [1]}"#)
        XCTAssertEqual(m, .trial(Trial(trialId: 3, phase: .flicker)))
    }

    func testTrialPaused() throws {
        // STOP ends a trial with phase "paused"; a phase the enum lacks would fail the whole message.
        XCTAssertEqual(try ServerMessage.decode(#"{"type": "trial", "trial_id": 4, "phase": "paused"}"#),
                       .trial(Trial(trialId: 4, phase: .paused)))
    }

    func testStatusDefaults() throws {
        let m = try ServerMessage.decode(#"{"type": "status", "decoder_connected": true}"#)
        XCTAssertEqual(m, .status(UIStatus(decoderConnected: true)))
        if case .status(let s) = m { XCTAssertEqual(s.llm, "off") }
    }

    func testShowPageDefaults() throws {
        let json = #"{"type": "show_page", "list_id": "home", "page": 1, "mode": "page", "slots": [{"slot": "s4", "role": "next", "label": "More options"}]}"#
        let m = try ServerMessage.decode(json)
        XCTAssertEqual(m, .showPage(ShowPage(listId: "home", page: 1, mode: .page,
                                             slots: [UISlot(slot: "s4", role: .next, label: "More options")])))
    }

    func testClientEncoding() throws {
        func check(_ m: ClientMessage, _ expected: [String: Any], line: UInt = #line) throws {
            let actual = try JSONSerialization.jsonObject(with: try m.encoded())
            XCTAssertTrue(jsonEqual(expected, actual), "\(actual)", line: line)
        }
        try check(.stop, ["type": "stop"])
        try check(.micReport(state: .needsTap, restarts: 2), ["type": "mic_report", "state": "needs_tap", "restarts": 2])
        try check(.partnerText(text: "hi there", source: .stt), ["type": "partner_text", "text": "hi there", "source": "stt"])
        XCTAssertEqual(try ClientMessage.decode(try ClientMessage.judgment(utteranceId: "u1", correct: false).encoded()),
                       .judgment(utteranceId: "u1", correct: false))
    }

    func testPageConfig() throws {
        let c = try PageConfig.decode(Data(#"{"tts_rate": 1.1, "keyboard_mode": true, "user_name": "Sam", "future": 1}"#.utf8))
        XCTAssertEqual(c.ttsRate, 1.1)
        XCTAssertTrue(c.keyboardMode)
        XCTAssertEqual(c.userName, "Sam")
        XCTAssertEqual(c.sttMode, "always")
        XCTAssertEqual(c.sttGapS, 1.2)
        XCTAssertEqual(c.appLink, "")
        XCTAssertEqual(try PageConfig.decode(Data("{}".utf8)), PageConfig.defaults)
    }
}
