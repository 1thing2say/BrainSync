import XCTest
@testable import BrainSyncKit

/// Checks the Swift models against messages exactly as the server sends and accepts them
/// (Fixtures/ui_messages.json, generated from app/schemas.py).
final class FixtureTests: XCTestCase {
    private func fixture() throws -> [String: Any] {
        guard let url = Bundle.module.url(forResource: "ui_messages", withExtension: "json", subdirectory: "Fixtures")
                ?? Bundle.module.url(forResource: "ui_messages", withExtension: "json") else {
            throw XCTSkip("Fixtures/ui_messages.json not generated yet")
        }
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func messages(_ key: String) throws -> [[String: Any]] {
        let list = try XCTUnwrap(try fixture()[key] as? [[String: Any]], "fixture has no \(key)")
        XCTAssertFalse(list.isEmpty)
        return list
    }

    private func data(_ object: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: object)
    }

    func testEveryServerMessageDecodes() throws {
        var seen = Set<String>()
        for msg in try messages("server_to_ui") {
            let type = try XCTUnwrap(msg["type"] as? String)
            let decoded = try ServerMessage.decode(try data(msg))
            let encoded: Data
            switch decoded {
            case .showPage(let m): encoded = try JSONEncoder().encode(m)
            case .trial(let m): encoded = try JSONEncoder().encode(m)
            case .highlight(let m): encoded = try JSONEncoder().encode(m)
            case .speak(let m): encoded = try JSONEncoder().encode(m)
            case .listen(let m): encoded = try JSONEncoder().encode(m)
            case .state(let m): encoded = try JSONEncoder().encode(m)
            case .transcript(let m): encoded = try JSONEncoder().encode(m)
            case .status(let m): encoded = try JSONEncoder().encode(m)
            case .unknown(let t):
                XCTFail("\(t) decoded as unknown")
                continue
            }
            seen.insert(type)
            // Every field the server sent came through with its value.
            var expected = msg
            expected.removeValue(forKey: "type")
            let actual = try JSONSerialization.jsonObject(with: encoded)
            XCTAssertTrue(jsonEqual(stripNulls(expected), stripNulls(actual)),
                          "\(type): \(expected) != \(actual)")
        }
        XCTAssertEqual(seen, ["show_page", "trial", "highlight", "speak", "listen", "state", "transcript", "status"])
    }

    func testServerMessageValues() throws {
        for msg in try messages("server_to_ui") {
            switch try ServerMessage.decode(try data(msg)) {
            case .showPage(let p):
                XCTAssertEqual(p.listId, msg["list_id"] as? String)
                XCTAssertEqual(p.slots.count, (msg["slots"] as? [Any])?.count)
                XCTAssertEqual(p.highlightIndex, msg["highlight_index"] as? Int)
                if p.mode == .list { XCTAssertFalse(p.options.isEmpty, "list mode carries options") }
            case .speak(let s):
                XCTAssertEqual(s.utteranceId, msg["utterance_id"] as? String)
                XCTAssertEqual(s.audioUrl, msg["audio_url"] as? String)
            case .listen(let l):
                XCTAssertEqual(l.untilStopped, msg["until_stopped"] as? Bool)
                XCTAssertEqual(l.reason, msg["reason"] as? String)
            default:
                break
            }
        }
    }

    func testFixtureCoversVariants() throws {
        let decoded = try messages("server_to_ui").map { try ServerMessage.decode(try data($0)) }
        var modes = Set<String>()
        var speakWithAudio = false, speakWithout = false
        var listenUntil = false, listenWindow = false, listenOff = false
        for m in decoded {
            switch m {
            case .showPage(let p): modes.insert(p.mode.rawValue)
            case .speak(let s): if s.audioUrl == nil { speakWithout = true } else { speakWithAudio = true }
            case .listen(let l):
                if l.untilStopped { listenUntil = true } else if l.seconds > 0 { listenWindow = true } else { listenOff = true }
            default: break
            }
        }
        XCTAssertEqual(modes, ["page", "confirm", "list"])
        XCTAssertTrue(speakWithAudio && speakWithout)
        XCTAssertTrue(listenUntil && listenWindow && listenOff)
    }

    func testEveryClientMessageRoundTrips() throws {
        var types = Set<String>()
        var micStates = Set<MicState>()
        for msg in try messages("ui_to_server") {
            let type = try XCTUnwrap(msg["type"] as? String)
            types.insert(type)
            let message = try ClientMessage.decode(try data(msg))
            if case .micReport(let state, _) = message { micStates.insert(state) }
            let encoded = try JSONSerialization.jsonObject(with: try message.encoded())
            XCTAssertTrue(jsonEqual(msg, encoded), "\(type): \(msg) != \(encoded)")
        }
        XCTAssertEqual(types, ["tts_done", "partner_text", "key", "stop", "resume", "judgment", "mic_set", "mic_report"])
        XCTAssertEqual(micStates, Set(MicState.allCases))
    }
}

func stripNulls(_ value: Any) -> Any {
    if let d = value as? [String: Any] {
        var out: [String: Any] = [:]
        for (k, v) in d where !(v is NSNull) { out[k] = stripNulls(v) }
        return out
    }
    if let a = value as? [Any] { return a.map(stripNulls) }
    return value
}

/// Order-insensitive JSON equality; numbers compare by value (10 == 10.0).
func jsonEqual(_ a: Any, _ b: Any) -> Bool {
    if let x = a as? [String: Any], let y = b as? [String: Any] {
        return Set(x.keys) == Set(y.keys) && x.allSatisfy { k, v in jsonEqual(v, y[k]!) }
    }
    if let x = a as? [Any], let y = b as? [Any] {
        return x.count == y.count && zip(x, y).allSatisfy { jsonEqual($0, $1) }
    }
    if a is NSNull || b is NSNull { return a is NSNull && b is NSNull }
    if let x = a as? String, let y = b as? String { return x == y }
    if a is String || b is String { return false }
    if let x = a as? NSNumber, let y = b as? NSNumber { return x.doubleValue == y.doubleValue }
    return false
}
