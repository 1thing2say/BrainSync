import Foundation

/// One screen of the app's first-run setup (SetupFlow), in the order they come.
public enum SetupStep: String, CaseIterable, Equatable, Sendable {
    case welcome, pair, safety, microphone, practice, done
}

/// The first-run setup's order, kept pure so it is tested here: welcome, pair, safety,
/// microphone, practice, done. A phone that is already paired skips pair. The app fixes the order
/// when the setup opens, so pairing halfway through does not shift the steps.
public struct SetupSteps: Equatable, Sendable {
    /// Where a step sits: `index` from 0, out of `count` steps.
    public struct Progress: Equatable, Sendable {
        public let index: Int
        public let count: Int

        public init(index: Int, count: Int) {
            self.index = index
            self.count = count
        }
    }

    public let steps: [SetupStep]

    public init(paired: Bool) {
        steps = SetupStep.allCases.filter { !(paired && $0 == .pair) }
    }

    public var first: SetupStep { steps[0] }
    public var count: Int { steps.count }

    /// The step after `step`; nil after the last one, or for a step this order skips.
    public func next(after step: SetupStep) -> SetupStep? {
        guard let index = steps.firstIndex(of: step), index + 1 < steps.count else { return nil }
        return steps[index + 1]
    }

    /// The step before `step`; nil on the first one, or for a step this order skips.
    public func back(before step: SetupStep) -> SetupStep? {
        guard let index = steps.firstIndex(of: step), index > 0 else { return nil }
        return steps[index - 1]
    }

    /// For the step indicator ("Step 2 of 5"); nil for a step this order skips.
    public func progress(of step: SetupStep) -> Progress? {
        guard let index = steps.firstIndex(of: step) else { return nil }
        return Progress(index: index, count: steps.count)
    }
}
