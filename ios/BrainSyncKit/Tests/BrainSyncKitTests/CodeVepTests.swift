import XCTest
@testable import BrainSyncKit

/// Loads Fixtures/cvep_frames.json (tools/ios_cvep_fixture.py: brainsync/vep/live.py,
/// clients/js/cvep.js and clients/js/brainsync-client.js).
func cvepFixture() throws -> [String: Any] {
    guard let url = Bundle.module.url(forResource: "cvep_frames", withExtension: "json", subdirectory: "Fixtures")
            ?? Bundle.module.url(forResource: "cvep_frames", withExtension: "json") else {
        throw XCTSkip("Fixtures/cvep_frames.json not generated yet (python tools/ios_cvep_fixture.py)")
    }
    let data = try Data(contentsOf: url)
    return try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
}

/// A fixture number; null (a non-finite value in the reference) as NaN.
func fixtureNumber(_ v: Any?) -> Double {
    (v as? NSNumber)?.doubleValue ?? .nan
}

final class CodeVepTests: XCTestCase {
    typealias V = CodeVep

    // MARK: - Against the reference implementations

    func testBitsMatchPythonAndJS() throws {
        let fx = try cvepFixture()
        let bits = try XCTUnwrap(fx["bits"] as? [Int])
        XCTAssertEqual(V.bits.map(Int.init), bits)
        XCTAssertEqual(V.bits.count, V.length)
        XCTAssertEqual(fx["lag_frames"] as? Int, V.lagFrames)
        XCTAssertEqual(fx["cycle_frames"] as? Int, V.cycleFrames)
    }

    func testFrameRuleMatchesReference() throws {
        let lit = try XCTUnwrap(try cvepFixture()["lit"] as? [String: String])
        XCTAssertEqual(Set(lit.keys), ["0", "31"])
        for (lagText, expected) in lit {
            let lag = try XCTUnwrap(Int(lagText))
            let got = (0..<expected.count).map { V.isLit(frame: $0, lag: lag) ? "1" : "0" }.joined()
            XCTAssertEqual(got, expected, "lag \(lag)")
            XCTAssertEqual(expected.count, 190)
        }
    }

    func testBarsMatchJS() throws {
        let rows = try XCTUnwrap(try cvepFixture()["bars"] as? [[String: Any]])
        for row in rows {
            XCTAssertEqual(V.bar(fixtureNumber(row["value"])), row["bar"] as? String, "\(row)")
        }
    }

    func testLabelLinesMatchJS() throws {
        let rows = try XCTUnwrap(try cvepFixture()["label_lines"] as? [[String: Any]])
        for row in rows {
            let line = V.labelLine(label: try XCTUnwrap(row["label"] as? String),
                                   score: fixtureNumber(row["score"]),
                                   best: try XCTUnwrap(row["best"] as? Bool),
                                   width: row["width"] as? Int)
            XCTAssertEqual(line, row["line"] as? String, "\(row)")
        }
    }

    func testStatusLinesMatchJS() throws {
        let rows = try XCTUnwrap(try cvepFixture()["status_lines"] as? [[String: Any]])
        for row in rows {
            let lead = row["lead"] as? NSNumber
            let line = V.statusLine(lead: lead?.doubleValue, committed: row["committed"] as? String,
                                    changed: try XCTUnwrap(row["changed"] as? Bool))
            XCTAssertEqual(line, row["line"] as? String, "\(row)")
        }
    }

    /// A whole decision as ConfidenceState shows it: one label line per slot, then the status.
    func testDecisionsMatchConfidenceState() throws {
        let rows = try XCTUnwrap(try cvepFixture()["decisions"] as? [[String: Any]])
        for row in rows {
            let slots = try XCTUnwrap(row["slots"] as? [String])
            let scores = try XCTUnwrap(row["scores"] as? [String: NSNumber])
            let raw = row["raw"] as? String
            let committed = row["committed"] as? String
            var lines = V.labelLines(labels: slots.map { $0.uppercased() },
                                     scores: slots.map { scores[$0]?.doubleValue },
                                     best: raw.flatMap { slots.firstIndex(of: $0) })
            lines.append(V.statusLine(lead: (row["lead"] as? NSNumber)?.doubleValue,
                                      committed: committed?.uppercased(),
                                      changed: row["changed"] as? Bool ?? false))
            XCTAssertEqual(lines, row["lines"] as? [String], "\(row)")
        }
    }

    func testTrainingLinesMatchConfidenceState() throws {
        let rows = try XCTUnwrap(try cvepFixture()["training"] as? [[String: Any]])
        for row in rows {
            let cycles = try XCTUnwrap(row["cycles"] as? Int)
            let line = V.trainingLine(cue: try XCTUnwrap(row["cue_label"] as? String),
                                      cycle: row["cycle"] as? Int ?? 0,
                                      of: row["of"] as? Int ?? cycles)
            XCTAssertEqual(line, row["line"] as? String, "\(row)")
        }
    }

    func testTrainedLinesMatchConfidenceState() throws {
        let rows = try XCTUnwrap(try cvepFixture()["trained"] as? [[String: Any]])
        for row in rows {
            let line = V.trainedLine(accuracy: (row["accuracy"] as? NSNumber)?.doubleValue,
                                     chance: (row["chance"] as? NSNumber)?.doubleValue)
            XCTAssertEqual(line, row["line"] as? String, "\(row)")
        }
    }

    // MARK: - The standard's examples (docs/CVEP-STANDARD.md section 3)

    func testStandardExample() {
        XCTAssertEqual(V.labelLines(labels: ["YES", "NO", "HELP"], scores: [0.42, 0.11, -0.03], best: 0),
                       ["YES  ███░░░░░ +0.42 <", "NO   █░░░░░░░ +0.11", "HELP ░░░░░░░░ -0.03"])
        XCTAssertEqual(V.statusLine(lead: 0.31, committed: "YES", changed: false), "lead 0.31   choice: YES")
        XCTAssertEqual(V.statusLine(lead: 0.31, committed: "YES", changed: true), "lead 0.31   choice: YES *")
        XCTAssertEqual(V.statusLine(lead: nil, committed: nil, changed: false), "lead  -     no choice yet")
        XCTAssertEqual(V.trainingLine(cue: "YES", cycle: 12, of: 30), "training: keep looking at YES   cycle 12/30")
        XCTAssertEqual(V.trainedLine(accuracy: 0.83, chance: 1.0 / 3),
                       "template ready: 83 % on held-out cycles (chance 33 %)")
    }

    func testMissingScoreShowsDash() {
        XCTAssertEqual(V.labelLines(labels: ["UP", "DOWN"], scores: [nil], best: nil),
                       ["UP   ░░░░░░░░   -  ", "DOWN ░░░░░░░░   -  "])
    }

    // MARK: - JavaScript rounding

    func testToFixedRoundsTiesUpLikeJS() {
        XCTAssertEqual(V.toFixed2(0.125), "0.13")      // printf: 0.12
        XCTAssertEqual(V.toFixed2(-0.125), "-0.13")
        XCTAssertEqual(V.toFixed2(0.375), "0.38")
        XCTAssertEqual(V.toFixed2(1.005), "1.00")      // 1.00499999999999989...: not a tie
        XCTAssertEqual(V.toFixed2(0.42), "0.42")
        XCTAssertEqual(V.toFixed2(-0.0), "0.00")
        XCTAssertEqual(V.toFixed2(2.5), "2.50")
        XCTAssertEqual(V.scoreText(-0.0), "+0.00")     // JS: -0 >= 0
        XCTAssertEqual(V.scoreText(.infinity), "  -  ")
    }

    func testJSRound() {
        XCTAssertEqual(V.jsRound(0.5), 1)
        XCTAssertEqual(V.jsRound(2.5), 3)
        XCTAssertEqual(V.jsRound(-2.5), -2)
        XCTAssertEqual(V.jsRound(0.49999999999999994), 0)
        XCTAssertEqual(V.jsRound(12.5), 13)
    }

    // MARK: - Frame rule and code

    func testFrameRuleAndLags() {
        // Target k shows the sequence k * 31 frames late (two targets at most).
        for n in 0..<200 {
            XCTAssertEqual(V.isLit(frame: n + 31, lag: 31), V.isLit(frame: n, lag: 0))
            XCTAssertEqual(V.bit(target: 1, frame: n + 31), V.bit(target: 0, frame: n))
        }
        XCTAssertFalse(V.isLit(frame: 0, lag: 0))       // the sequence starts 00000 1
        XCTAssertTrue(V.isLit(frame: 5, lag: 0))
        XCTAssertEqual(V.isLit(frame: -1, lag: 0), V.isLit(frame: 62, lag: 0))
        XCTAssertEqual(V.lag(ofTarget: 1), 31)
        XCTAssertEqual(V.targetCount, 2)
        XCTAssertEqual(V.maxTargets, 2)
        XCTAssertTrue(V.isCycleStart(frame: 126))
        XCTAssertFalse(V.isCycleStart(frame: 127))
        XCTAssertEqual(V.cycleIndex(frame: 125), 1)
        XCTAssertEqual(V.cycleIndex(frame: -1), -1)
        XCTAssertEqual(V.bits.filter { $0 == 1 }.count, 32)   // an m-sequence: 2^5 ones
    }

    func testCodeDecodesWelcomeAndFallsBack() throws {
        let json = ##"{"bits": [1, 0, 1, 1], "lag_frames": 1, "cycle_frames": 4, "train_cycles": 6, "fill": "#ffffff"}"##
        let code = try JSONDecoder().decode(V.Code.self, from: Data(json.utf8))
        XCTAssertTrue(code.isValid)
        XCTAssertEqual(code.trainCycles, 6)
        XCTAssertEqual(V.Code.orStandard(code), code)
        XCTAssertTrue(V.isLit(bits: code.bits, frame: 2, lag: 0))
        XCTAssertFalse(V.isLit(bits: code.bits, frame: 2, lag: 1))

        var bad = code
        bad.cycleFrames = 63
        XCTAssertFalse(bad.isValid)
        XCTAssertEqual(V.Code.orStandard(bad), .standard)
        XCTAssertEqual(V.Code.orStandard(nil), .standard)
        bad = code
        bad.bits = [0, 2, 1, 1]
        XCTAssertFalse(bad.isValid)
        XCTAssertTrue(V.Code.standard.isValid)
    }
}
