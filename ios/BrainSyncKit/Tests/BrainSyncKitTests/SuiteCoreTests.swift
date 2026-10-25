import XCTest
@testable import BrainSyncKit

final class SuiteCoreTests: XCTestCase {
    func testSlotsAndLags() {
        XCTAssertEqual(SuiteSlots.ids, ["t0", "t1", "t2"])
        XCTAssertEqual(SuiteSlots.count, 3)
        let table: [(String, Int?, Int?)] = [
            ("t0", 0, 0), ("t1", 1, 21), ("t2", 2, 42),
            ("t3", nil, nil), ("s1", nil, nil), ("", nil, nil), ("T0", nil, nil),
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
        XCTAssertEqual(home.labels, ["Camera", "Reels", "Voice"])
        XCTAssertEqual(home.enabled, [true, true, true])
        XCTAssertEqual(home.trainingLabel, "Camera")
    }

    func testHomeSelect() {
        let table: [(String, SuiteApp?)] = [
            ("t0", .camera), ("t1", .reels), ("t2", .voice), ("t3", nil), ("s1", nil), ("", nil),
        ]
        for (slot, app) in table {
            var home = SuiteHomeCore()
            XCTAssertEqual(home.select(slot), app, slot)
            XCTAssertEqual(home, SuiteHomeCore(), "home holds no state")
        }
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
