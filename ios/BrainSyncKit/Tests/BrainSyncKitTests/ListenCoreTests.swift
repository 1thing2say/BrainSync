import XCTest
@testable import BrainSyncKit

final class ListenCoreTests: XCTestCase {
    typealias LC = ListenCore

    func testIsEcho() {
        let reply = "Yes, let's go outside."
        XCTAssertTrue(LC.isEcho("yes let's go outside", lastSpoken: reply, sinceReopen: 0.5, echoWindow: 4))
        XCTAssertTrue(LC.isEcho("let's go", lastSpoken: reply, sinceReopen: 0.5, echoWindow: 4))
        XCTAssertFalse(LC.isEcho("yes", lastSpoken: reply, sinceReopen: 0.5, echoWindow: 4))
        XCTAssertFalse(LC.isEcho("no I don't think so", lastSpoken: "No.", sinceReopen: 0.5, echoWindow: 4))
        XCTAssertFalse(LC.isEcho("yes let's go outside", lastSpoken: reply, sinceReopen: 5, echoWindow: 4))
        XCTAssertFalse(LC.isEcho("let's go outside today then", lastSpoken: reply, sinceReopen: 0.5, echoWindow: 4))
        XCTAssertFalse(LC.isEcho("anything", lastSpoken: "", sinceReopen: 0, echoWindow: 4))
    }

    func testJoinFinals() {
        XCTAssertEqual(LC.joinFinals(["it might not work right now", "how was your weekend"]),
                       "it might not work right now. how was your weekend")
        XCTAssertEqual(LC.joinFinals(["Hello there.", "are you hungry"]), "Hello there. are you hungry")
        XCTAssertEqual(LC.joinFinals(["  ", "tea"]), "tea")
        XCTAssertEqual(LC.joinFinals([]), "")
    }

    func testMeaningful() {
        XCTAssertFalse(LC.meaningful("um uh"))
        XCTAssertFalse(LC.meaningful("a"))
        XCTAssertTrue(LC.meaningful("tea"))
        XCTAssertTrue(LC.meaningful("um are you okay"))
    }

    func testNorm() {
        XCTAssertEqual(LC.norm("  Yes, let's GO   outside!! "), "yes let's go outside")
        XCTAssertEqual(LC.norm("caf\u{e9} 42"), "caf 42")
    }

    func testTimingMatchesJS() {
        let t = LC.Timing.defaults
        XCTAssertEqual(t.healthyRestart, 0.1)
        XCTAssertEqual(t.quickFirst, 0.5)
        XCTAssertEqual(t.quickMax, 4)
        XCTAssertEqual(t.networkFirst, 1)
        XCTAssertEqual(t.networkMax, 8)
        XCTAssertEqual(t.audioCapture, 3)
        XCTAssertEqual(t.healthyAfter, 1)
        XCTAssertEqual(t.stallAfter, 60)
        XCTAssertEqual(t.watchdog, 2)
        XCTAssertEqual(t.needsTapAfter, 3)
        XCTAssertEqual(t.grace, 3)
        XCTAssertEqual(LC.minConfidence, 0.3)
    }

    // MARK: LineAssembler

    func testLineAfterGap() {
        var a = LC.LineAssembler(gap: 1.2, maxLine: 15)
        a.addFinal("are you hungry", at: 0)
        XCTAssertEqual(a.pending, "are you hungry")
        XCTAssertNil(a.takeLine(at: 1.0))
        XCTAssertEqual(a.dueAt, 1.2)
        XCTAssertEqual(a.takeLine(at: 1.2), "are you hungry")
        XCTAssertEqual(a.pending, "")
        XCTAssertNil(a.takeLine(at: 5))
        a.addFinal("I brought tea", at: 6)
        XCTAssertEqual(a.takeLine(at: 7.2), "I brought tea")
    }

    func testSentencesJoinIntoOneLine() {
        var a = LC.LineAssembler(gap: 1.2, maxLine: 15)
        a.addFinal("would you like", at: 0)
        a.addFinal("some coffee", at: 0.1)
        XCTAssertNil(a.takeLine(at: 1.2)) // 1.1 s since the last final
        XCTAssertEqual(a.takeLine(at: 1.3), "would you like. some coffee")
    }

    func testMonologueAfterMaxLine() {
        var a = LC.LineAssembler(gap: 1.2, maxLine: 15)
        var lines: [String] = []
        for i in 0..<16 {
            a.addFinal("sentence \(i)", at: Double(i))
            if let line = a.takeLine(at: Double(i) + 0.5) { lines.append(line) }
        }
        XCTAssertEqual(lines.count, 1)
        XCTAssertTrue(lines[0].hasPrefix("sentence 0."))
    }

    func testForce() {
        var a = LC.LineAssembler(gap: 1.2, maxLine: 15)
        XCTAssertNil(a.takeLine(at: 0, force: true))
        a.addFinal("tea", at: 0)
        XCTAssertEqual(a.takeLine(at: 0.1, force: true), "tea")
    }

    func testFillerDropped() {
        var a = LC.LineAssembler(gap: 1.2, maxLine: 15)
        a.addFinal("um uh", at: 0)
        a.addFinal("   ", at: 0.5)
        XCTAssertNil(a.takeLine(at: 2))
        XCTAssertTrue(a.isEmpty)
        XCTAssertNil(a.dueAt)
    }

    // MARK: RestartPolicy

    func testNetworkBackoff() {
        var p = LC.RestartPolicy()
        let gaps = (0..<5).map { _ in p.delay(afterSessionRan: 0.1, error: .network) }
        XCTAssertEqual(gaps, [1, 2, 4, 8, 8])
    }

    func testQuickDeathBackoff() {
        var p = LC.RestartPolicy()
        let gaps = (0..<7).map { _ in p.delay(afterSessionRan: 0, error: nil) }
        XCTAssertEqual(gaps, [0.5, 1, 2, 4, 4, 4, 4])
    }

    func testHealthyEndRestartsAtOnce() {
        var p = LC.RestartPolicy()
        for _ in 0..<5 { XCTAssertEqual(p.delay(afterSessionRan: 2, error: .other), 0.1) }
        XCTAssertEqual(p.delay(afterSessionRan: 8, error: nil), 0.1)
    }

    func testHealthyEndResetsQuickBackoff() {
        var p = LC.RestartPolicy()
        _ = p.delay(afterSessionRan: 0, error: nil)
        _ = p.delay(afterSessionRan: 0, error: nil)
        XCTAssertEqual(p.delay(afterSessionRan: 0, error: nil), 2)
        XCTAssertEqual(p.delay(afterSessionRan: 1, error: nil), 0.1)
        XCTAssertEqual(p.delay(afterSessionRan: 0, error: nil), 0.5)
    }

    func testHeardSpeechCountsAsHealthy() {
        var p = LC.RestartPolicy()
        _ = p.delay(afterSessionRan: 0.1, error: .network)
        _ = p.delay(afterSessionRan: 0.1, error: .network)
        XCTAssertEqual(p.delay(afterSessionRan: 0.2, error: nil, heard: true), 0.1)
        XCTAssertEqual(p.delay(afterSessionRan: 0.1, error: .network), 1)
    }

    func testNoMicRetriesEvery3s() {
        var p = LC.RestartPolicy()
        for _ in 0..<4 { XCTAssertEqual(p.delay(afterSessionRan: 0, error: .noMic), 3) }
    }

    func testDeniedAndUnsupportedNeverRestart() {
        var p = LC.RestartPolicy()
        XCTAssertNil(p.delay(afterSessionRan: 5, error: .denied))
        XCTAssertNil(p.delay(afterSessionRan: 5, error: .unsupported))
    }

    func testResetStartsBackoffOver() {
        var p = LC.RestartPolicy()
        _ = p.delay(afterSessionRan: 0, error: .network)
        _ = p.delay(afterSessionRan: 0, error: nil)
        p.reset()
        XCTAssertEqual(p.delay(afterSessionRan: 0, error: .network), 1)
        XCTAssertEqual(p.delay(afterSessionRan: 0, error: nil), 0.5)
    }
}
