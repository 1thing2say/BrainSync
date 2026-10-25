import XCTest
@testable import BrainSyncKit

/// c-VEP on the mirror: the same values as ui/view_core.js confidence() and training()
/// (tests/js/view_core.test.js).
final class CodeViewTests: XCTestCase {
    func testConfidenceBarsClipTheCorrelationAndMarkBestAndCommitted() {
        let bars = ConfidenceBar.make(Confidence(trialId: 1, scores: ["s1": 0.42, "s2": 1.4, "s3": -0.03],
                                                 raw: "s1", committed: "s2"))
        XCTAssertEqual(bars["s1"], ConfidenceBar(fraction: 0.42, text: "+0.42", best: true, committed: false))
        XCTAssertEqual(bars["s2"], ConfidenceBar(fraction: 1, text: "+1.40", best: false, committed: true))
        XCTAssertEqual(bars["s3"], ConfidenceBar(fraction: 0, text: "-0.03", best: false, committed: false))
        XCTAssertTrue(ConfidenceBar.make(Confidence(trialId: 1, scores: [:])).isEmpty)
    }

    func testTrainingCard() {
        XCTAssertEqual(TrainingCard.make(Training(slot: "s1", label: "left", cycle: 12, of: 30)),
                       TrainingCard(title: "Training: look at the left square", detail: "cycle 12 of 30",
                                    fraction: 0.4, tone: "look", done: false))
        XCTAssertEqual(TrainingCard.make(Training(slot: "s1", label: "left", cycle: 30, of: 30,
                                                  accuracy: 0.83, chance: 1.0 / 3.0)),
                       TrainingCard(title: "Template ready", detail: "83 % on held-out cycles (chance 33 %)",
                                    fraction: 1, tone: "ok", done: true))
        let failed = TrainingCard.make(Training(slot: "s1", label: "left", cycle: 4, of: 30, message: "interrupted"))
        XCTAssertEqual(failed.title, "Training failed")
        XCTAssertEqual(failed.tone, "bad")
        XCTAssertEqual(TrainingCard.make(Training(slot: "s1", of: 30)).title, "Training: look at the first square")
    }

    func testScreenPlaces() {
        let row = ["s1", "s2", "s3"]
        XCTAssertEqual(row.map { ScreenPlace.name($0, layout: "cvep", slots: row) }, ["Left", "Middle", "Right"])
        XCTAssertEqual(ScreenPlace.name("s9", layout: "cvep", slots: row), "")
        XCTAssertEqual(["s1", "s2", "s3", "s4"].map { ScreenPlace.name($0, layout: "diamond", slots: []) },
                       ["Top", "Left", "Right", "Bottom"])
    }

    func testPageConfigCarriesTheLayout() throws {
        let c = try PageConfig.decode(Data(#"{"paradigm": "code", "layout": "cvep", "slots": ["s1", "s2", "s3"]}"#.utf8))
        XCTAssertEqual(c.paradigm, "code")
        XCTAssertEqual(c.layout, "cvep")
        XCTAssertEqual(c.slots, ["s1", "s2", "s3"])
        let old = try PageConfig.decode(Data("{}".utf8))  // an older server: the f-VEP diamond
        XCTAssertEqual(old.layout, "diamond")
        XCTAssertEqual(old.slots, ["s1", "s2", "s3", "s4"])
    }
}
