import Foundation

/// The Reels app's logic: a feed of `count` reels scrolled by looking at three squares.
///
/// - t0 Up: the previous reel. On the first reel nothing moves and the effect is `.top`, so
///   the view can bump the feed to show there is nothing above.
/// - t1 Like / pause: one look at a playing reel likes it and pauses it; one look at a paused
///   reel plays it again, and the like stays. A like cannot be taken back by looking, so a
///   misread never undoes one; the square's label says what the next look does (Like, Pause
///   or Play).
/// - t2 Down: the next reel. After the last one the feed loops back to the first
///   (`wrapped: true`), so Down always moves and the demo never runs out.
///
/// Moving to another reel always plays it: `paused` belongs to the reel on screen.
public struct ReelsCore: SuiteAppCore, Equatable, Sendable {
    public enum Direction: Equatable, Sendable {
        case up, down
    }

    /// What the view animates after a selection.
    public enum Effect: Equatable, Sendable {
        /// Scroll to this reel and play it. `wrapped` is true when Down went from the last reel
        /// to the first.
        case scroll(to: Int, direction: Direction, wrapped: Bool)
        /// Up on the first reel: nothing above, bump the feed.
        case top
        /// The reel was just liked, and it pauses.
        case likeAndPause(Int)
        /// The reel was liked before; it only pauses.
        case pause(Int)
        /// The paused reel plays again.
        case play(Int)
    }

    public let count: Int
    public private(set) var index: Int = 0
    public private(set) var liked: Set<Int> = []
    public private(set) var paused = false

    /// `count`: the number of reels in the feed, at least 1.
    public init(count: Int) {
        precondition(count > 0, "a feed needs at least one reel")
        self.count = count
    }

    public var isCurrentLiked: Bool { liked.contains(index) }

    public var labels: [String] {
        ["Up", paused ? "Play" : (isCurrentLiked ? "Pause" : "Like"), "Down"]
    }

    /// Every square always does something (Up on the first reel bumps).
    public var enabled: [Bool] { [true, true, true] }

    public var trainingLabel: String { "Up" }

    public mutating func select(_ slot: String) -> Effect? {
        switch SuiteSlots.index(of: slot) {
        case 0:
            guard index > 0 else { return .top }
            index -= 1
            paused = false
            return .scroll(to: index, direction: .up, wrapped: false)
        case 1:
            if paused {
                paused = false
                return .play(index)
            }
            paused = true
            return liked.insert(index).inserted ? .likeAndPause(index) : .pause(index)
        case 2:
            let wrapped = index == count - 1
            index = wrapped ? 0 : index + 1
            paused = false
            return .scroll(to: index, direction: .down, wrapped: wrapped)
        default:
            return nil
        }
    }
}
