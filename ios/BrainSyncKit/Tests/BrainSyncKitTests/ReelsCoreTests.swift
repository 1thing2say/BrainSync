import XCTest
@testable import BrainSyncKit

final class ReelsCoreTests: XCTestCase {
    func testStartsOnFirstReelPlaying() {
        let reels = ReelsCore(count: 3)
        XCTAssertEqual(reels.index, 0)
        XCTAssertFalse(reels.paused)
        XCTAssertEqual(reels.liked, [])
        XCTAssertEqual(reels.labels, ["Up", "Like", "Down"])
        XCTAssertEqual(reels.enabled, [true, true, true])
        XCTAssertEqual(reels.trainingLabel, "Up")
    }

    func testUpAtTopBumps() {
        var reels = ReelsCore(count: 3)
        XCTAssertEqual(reels.select("t0"), .top)
        XCTAssertEqual(reels.index, 0)
    }

    func testDownThenUpThenLoop() {
        var reels = ReelsCore(count: 3)
        let table: [(String, ReelsCore.Effect, Int)] = [
            ("t2", .scroll(to: 1, direction: .down, wrapped: false), 1),
            ("t2", .scroll(to: 2, direction: .down, wrapped: false), 2),
            ("t2", .scroll(to: 0, direction: .down, wrapped: true), 0),
            ("t0", .top, 0),
            ("t2", .scroll(to: 1, direction: .down, wrapped: false), 1),
            ("t0", .scroll(to: 0, direction: .up, wrapped: false), 0),
        ]
        for (slot, effect, index) in table {
            XCTAssertEqual(reels.select(slot), effect, slot)
            XCTAssertEqual(reels.index, index, slot)
        }
    }

    func testOneReelFeedLoopsOntoItself() {
        var reels = ReelsCore(count: 1)
        XCTAssertEqual(reels.select("t2"), .scroll(to: 0, direction: .down, wrapped: true))
        XCTAssertEqual(reels.select("t0"), .top)
    }

    func testLikePausePlay() {
        var reels = ReelsCore(count: 3)
        // (effect, paused after, label after)
        let table: [(ReelsCore.Effect, Bool, String)] = [
            (.likeAndPause(0), true, "Play"),
            (.play(0), false, "Pause"),
            (.pause(0), true, "Play"),   // already liked: only pauses
            (.play(0), false, "Pause"),
        ]
        for (effect, paused, label) in table {
            XCTAssertEqual(reels.select("t1"), effect)
            XCTAssertEqual(reels.paused, paused)
            XCTAssertEqual(reels.labels[1], label)
            XCTAssertEqual(reels.liked, [0], "a like stays")
        }
    }

    func testMovingPlaysTheNewReelAndKeepsLikes() {
        var reels = ReelsCore(count: 3)
        XCTAssertEqual(reels.select("t1"), .likeAndPause(0))
        XCTAssertEqual(reels.select("t2"), .scroll(to: 1, direction: .down, wrapped: false))
        XCTAssertFalse(reels.paused)
        XCTAssertFalse(reels.isCurrentLiked)
        XCTAssertEqual(reels.labels[1], "Like")
        XCTAssertEqual(reels.select("t1"), .likeAndPause(1))
        XCTAssertEqual(reels.select("t0"), .scroll(to: 0, direction: .up, wrapped: false))
        XCTAssertFalse(reels.paused)
        XCTAssertTrue(reels.isCurrentLiked)
        XCTAssertEqual(reels.labels[1], "Pause")
        XCTAssertEqual(reels.liked, [0, 1])
    }

    func testUnknownSlotsIgnored() {
        var reels = ReelsCore(count: 3)
        _ = reels.select("t1")
        let before = reels
        for slot in ["t3", "s1", "", "T1"] {
            XCTAssertNil(reels.select(slot), slot)
        }
        XCTAssertEqual(reels, before)
    }
}
