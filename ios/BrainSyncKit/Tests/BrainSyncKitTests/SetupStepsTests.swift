import XCTest
@testable import BrainSyncKit

final class SetupStepsTests: XCTestCase {
    func testFullOrderWhenNotPaired() {
        let s = SetupSteps(paired: false)
        XCTAssertEqual(s.steps, [.welcome, .pair, .safety, .microphone, .practice, .done])
        XCTAssertEqual(s.first, .welcome)
        XCTAssertEqual(s.count, 6)
    }

    func testPairedSkipsPair() {
        let s = SetupSteps(paired: true)
        XCTAssertEqual(s.steps, [.welcome, .safety, .microphone, .practice, .done])
        XCTAssertEqual(s.count, 5)
        XCTAssertEqual(s.next(after: .welcome), .safety)
        XCTAssertEqual(s.back(before: .safety), .welcome)
        // A skipped step has no place in the order.
        XCTAssertNil(s.next(after: .pair))
        XCTAssertNil(s.back(before: .pair))
        XCTAssertNil(s.progress(of: .pair))
    }

    func testNextAndBack() {
        let s = SetupSteps(paired: false)
        XCTAssertEqual(s.next(after: .welcome), .pair)
        XCTAssertEqual(s.next(after: .pair), .safety)
        XCTAssertEqual(s.next(after: .practice), .done)
        XCTAssertNil(s.next(after: .done))
        XCTAssertEqual(s.back(before: .done), .practice)
        XCTAssertEqual(s.back(before: .pair), .welcome)
        XCTAssertNil(s.back(before: .welcome))
    }

    func testProgress() {
        let s = SetupSteps(paired: false)
        XCTAssertEqual(s.progress(of: .welcome), SetupSteps.Progress(index: 0, count: 6))
        XCTAssertEqual(s.progress(of: .safety), SetupSteps.Progress(index: 2, count: 6))
        XCTAssertEqual(s.progress(of: .done), SetupSteps.Progress(index: 5, count: 6))
        let paired = SetupSteps(paired: true)
        XCTAssertEqual(paired.progress(of: .safety), SetupSteps.Progress(index: 1, count: 5))
        XCTAssertEqual(paired.progress(of: .done), SetupSteps.Progress(index: 4, count: 5))
    }

    func testWalkVisitsEveryStepOnceAndBackAgain() {
        for (paired, lookToSpeak) in [(false, false), (true, false), (false, true), (true, true)] {
            let s = SetupSteps(paired: paired, lookToSpeak: lookToSpeak)
            var forward: [SetupStep] = [s.first]
            while let next = s.next(after: forward.last!) { forward.append(next) }
            XCTAssertEqual(forward, s.steps)
            XCTAssertEqual(forward.last, .done)
            var backward: [SetupStep] = [.done]
            while let previous = s.back(before: backward.last!) { backward.append(previous) }
            XCTAssertEqual(backward, s.steps.reversed())
        }
    }

    func testSafetyAlwaysComesBeforeDone() {
        // The warning is read before the setup's last step starts the session (PLAN rule 10).
        for paired in [false, true] {
            for lookToSpeak in [false, true] {
                let steps = SetupSteps(paired: paired, lookToSpeak: lookToSpeak).steps
                XCTAssertLessThan(steps.firstIndex(of: .safety)!, steps.firstIndex(of: .done)!)
                XCTAssertEqual(steps.last, .done)
            }
        }
    }

    func testLookingOnlyForLookToSpeak() {
        XCTAssertFalse(SetupSteps(paired: false).steps.contains(.looking))
        XCTAssertNil(SetupSteps(paired: true).progress(of: .looking))
        let s = SetupSteps(paired: false, lookToSpeak: true)
        XCTAssertEqual(s.steps, [.welcome, .pair, .safety, .microphone, .looking, .practice, .done])
        XCTAssertEqual(s.next(after: .microphone), .looking)
        XCTAssertEqual(s.next(after: .looking), .practice)
        XCTAssertEqual(s.back(before: .practice), .looking)
        let paired = SetupSteps(paired: true, lookToSpeak: true)
        XCTAssertEqual(paired.steps, [.welcome, .safety, .microphone, .looking, .practice, .done])
        XCTAssertEqual(paired.progress(of: .looking), SetupSteps.Progress(index: 3, count: 6))
    }

    func testLookingAfterSafetyBeforePractice() {
        // The calibration is explained after the warning, and before the practice shows it.
        for paired in [false, true] {
            let steps = SetupSteps(paired: paired, lookToSpeak: true).steps
            XCTAssertLessThan(steps.firstIndex(of: .safety)!, steps.firstIndex(of: .looking)!)
            XCTAssertLessThan(steps.firstIndex(of: .looking)!, steps.firstIndex(of: .practice)!)
        }
    }

    func testWithLookToSpeakKeepsEarlierSteps() {
        // Paired halfway (the pair step stays), then the server sends a stimulus: every step up
        // to microphone keeps its place, and looking comes in after it.
        let opened = SetupSteps(paired: false)
        let grown = opened.withLookToSpeak()
        XCTAssertTrue(grown.lookToSpeak)
        XCTAssertFalse(grown.paired)
        XCTAssertEqual(Array(grown.steps.prefix(4)), Array(opened.steps.prefix(4)))
        XCTAssertEqual(grown.next(after: .microphone), .looking)
        XCTAssertEqual(grown.withLookToSpeak(), grown)
        XCTAssertEqual(SetupSteps(paired: true).withLookToSpeak(),
                       SetupSteps(paired: true, lookToSpeak: true))
    }
}
