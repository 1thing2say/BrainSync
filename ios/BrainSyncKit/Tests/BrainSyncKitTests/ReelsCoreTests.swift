import XCTest
@testable import BrainSyncKit

final class ReelsCoreTests: XCTestCase {
    func testStartsOnFirstReelPlaying() {
        let reels = ReelsCore(count: 3)
        XCTAssertEqual(reels.index, 0)
        XCTAssertFalse(reels.paused)
        XCTAssertEqual(reels.liked, [])
        XCTAssertEqual(reels.labels, ["Like", "Next"])
        XCTAssertEqual(reels.enabled, [true, true])
        XCTAssertEqual(reels.trainingLabel, "Like")
    }

    func testNextLoops() {
        var reels = ReelsCore(count: 3)
        let table: [(ReelsCore.Effect, Int)] = [
            (.scroll(to: 1, direction: .down, wrapped: false), 1),
            (.scroll(to: 2, direction: .down, wrapped: false), 2),
            (.scroll(to: 0, direction: .down, wrapped: true), 0),
            (.scroll(to: 1, direction: .down, wrapped: false), 1),
        ]
        for (effect, index) in table {
            XCTAssertEqual(reels.select("t1"), effect)
            XCTAssertEqual(reels.index, index)
        }
    }

    func testOneReelFeedLoopsOntoItself() {
        var reels = ReelsCore(count: 1)
        XCTAssertEqual(reels.select("t1"), .scroll(to: 0, direction: .down, wrapped: true))
        XCTAssertEqual(reels.index, 0)
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
            XCTAssertEqual(reels.select("t0"), effect)
            XCTAssertEqual(reels.paused, paused)
            XCTAssertEqual(reels.labels[0], label)
            XCTAssertEqual(reels.liked, [0], "a like stays")
        }
    }

    func testMovingPlaysTheNewReelAndKeepsLikes() {
        var reels = ReelsCore(count: 2)
        XCTAssertEqual(reels.select("t0"), .likeAndPause(0))
        XCTAssertEqual(reels.select("t1"), .scroll(to: 1, direction: .down, wrapped: false))
        XCTAssertFalse(reels.paused)
        XCTAssertFalse(reels.isCurrentLiked)
        XCTAssertEqual(reels.labels[0], "Like")
        XCTAssertEqual(reels.select("t0"), .likeAndPause(1))
        XCTAssertEqual(reels.select("t1"), .scroll(to: 0, direction: .down, wrapped: true))
        XCTAssertFalse(reels.paused)
        XCTAssertTrue(reels.isCurrentLiked)
        XCTAssertEqual(reels.labels[0], "Pause")
        XCTAssertEqual(reels.liked, [0, 1])
    }

    func testUnknownSlotsIgnored() {
        var reels = ReelsCore(count: 3)
        _ = reels.select("t0")
        let before = reels
        for slot in ["t2", "t3", "s1", "", "T1"] {
            XCTAssertNil(reels.select(slot), slot)
        }
        XCTAssertEqual(reels, before)
    }
}
