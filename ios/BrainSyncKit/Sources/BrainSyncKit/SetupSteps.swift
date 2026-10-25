import Foundation

/// One screen of the app's first-run setup (SetupFlow), in the order they come. `looking`: how the
/// user picks on this phone (Look to Speak, c-VEP): left or right card, and the calibration first.
public enum SetupStep: String, CaseIterable, Equatable, Sendable {
    case welcome, pair, safety, microphone, looking, practice, done
}

/// The first-run setup's order, kept pure so it is tested here: welcome, pair, safety,
/// microphone, looking, practice, done. A phone that is already paired skips pair; looking comes
/// only when this phone is the user's screen (the server sent a c-VEP `stimulus`). The app fixes
/// the order when the setup opens, so pairing halfway through does not shift the steps; it may
/// add looking later (`withLookToSpeak`), which never moves a step before it.
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
    /// The pair step is skipped (the phone was paired when the setup opened).
    public let paired: Bool
    /// The looking step is in the order.
    public let lookToSpeak: Bool

    public init(paired: Bool, lookToSpeak: Bool = false) {
        self.paired = paired
        self.lookToSpeak = lookToSpeak
        steps = SetupStep.allCases.filter {
            !(paired && $0 == .pair) && !(!lookToSpeak && $0 == .looking)
        }
    }

    /// The same order with the looking step, once the server turns out to run Look to Speak on
    /// the phone. Pair stays as it was, so the steps before looking keep their places.
    public func withLookToSpeak() -> SetupSteps {
        SetupSteps(paired: paired, lookToSpeak: true)
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
