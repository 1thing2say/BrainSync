import XCTest
@testable import BrainSyncKit

final class SuiteCoreTests: XCTestCase {
    func testTwoSlotsAtMost() {
        XCTAssertEqual(SuiteSlots.ids, ["t0", "t1"])
        XCTAssertEqual(SuiteSlots.count, 2)
        XCTAssertLessThanOrEqual(SuiteSlots.count, CodeVep.maxTargets)
        XCTAssertEqual(SuiteSlots.lags, [0, 31])
        let table: [(String, Int?, Int?)] = [
            ("t0", 0, 0), ("t1", 1, 31),
            ("t2", nil, nil), ("s1", nil, nil), ("", nil, nil), ("T0", nil, nil),
        ]
        for (slot, index, lag) in table {
            XCTAssertEqual(SuiteSlots.index(of: slot), index, slot)
            XCTAssertEqual(SuiteSlots.lag(of: slot), lag, slot)
        }
    }

    func testAppTitles() {
        XCTAssertEqual(SuiteApp.allCases.map(\.title), ["Home", "Camera", "Reels", "Voice"])
    }

    func testHomeLabelsAndTraining() {
        let home = SuiteHomeCore()
        XCTAssertEqual(home.current, .camera)
        XCTAssertEqual(home.labels, ["Open Camera", "Next"])
        XCTAssertEqual(home.enabled, [true, true])
        XCTAssertEqual(home.trainingLabel, "Open Camera")
    }

    func testHomeNextCyclesAndOpenOpensTheHighlightedApp() {
        var home = SuiteHomeCore()
        let table: [(String, SuiteHomeCore.Effect, String)] = [
            ("t0", .open(.camera), "Open Camera"),
            ("t1", .highlight(.reels), "Open Reels"),
            ("t0", .open(.reels), "Open Reels"),
            ("t1", .highlight(.voice), "Open Voice"),
            ("t1", .highlight(.camera), "Open Camera"),
            ("t1", .highlight(.reels), "Open Reels"),
        ]
        for (slot, effect, label) in table {
            XCTAssertEqual(home.select(slot), effect, slot)
            XCTAssertEqual(home.labels[0], label)
        }
    }

    func testHomeHighlightAndUnknownSlots() {
        var home = SuiteHomeCore()
        home.highlight(.voice)
        XCTAssertEqual(home.current, .voice)
        home.highlight(.home)   // not offered: unchanged
        XCTAssertEqual(home.current, .voice)
        let before = home
        for slot in ["t2", "t3", "s1", ""] {
            XCTAssertNil(home.select(slot), slot)
        }
        XCTAssertEqual(home, before)
    }

    func testEveryCoreHasOneLabelAndFlagPerSlot() {
        func check<C: SuiteAppCore>(_ core: C, _ name: String) {
            XCTAssertEqual(core.labels.count, SuiteSlots.count, name)
            XCTAssertEqual(core.enabled.count, SuiteSlots.count, name)
            XCTAssertEqual(core.trainingLabel, core.labels[0], name)
        }
        check(SuiteHomeCore(), "home")
        check(CameraCore(), "camera")
        check(ReelsCore(count: 3), "reels")
    }
}
