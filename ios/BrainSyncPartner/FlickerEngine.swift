// The c-VEP flicker on the phone (INTERFACE-v2-draft.md section 9): one CADisplayLink locked to
// 60 Hz (never ProMotion's 120) drives a white layer over each card, frame by frame, outside
// SwiftUI. Cards register their layer by slot; the engine flickers the slots of a trial from the
// server's flicker phase, reports each cycle's first frame (phone time, for cycle_markers) and its
// own measured refresh rate (display_report), and stops at once on stop().
//
// INTERFACE STUB: the screens (LookToSpeakView, AppModel) are written against this API; the engine
// itself (CVEPCode, FrameRateCheck, the display link) fills in the bodies without changing it.

import SwiftUI
import UIKit
import BrainSyncKit

@MainActor
final class FlickerEngine {
    /// The refresh check: the measured rate, whether the phone may flicker, and why not.
    struct Report: Equatable {
        var refreshHz: Double
        var ok: Bool
        var message: String
    }

    /// The first frames of these cycles of trial `trialId` were on screen at these phone times
    /// (Unix seconds); `droppedFrames` so far in the trial. Called once per cycle, on the main actor.
    var onCycles: ((_ trialId: Int, _ cycles: [(cycle: Int, phoneTime: Double)], _ droppedFrames: Int) -> Void)?
    /// The refresh check changed (also called once after `startMonitoring`).
    var onReport: ((Report) -> Void)?

    /// The last refresh check; nil before the display link has measured anything.
    private(set) var report: Report?
    /// True between `start` and `stop`.
    private(set) var isFlickering = false

    /// The code to show (from the server's `stimulus` message).
    func setStimulus(_ spec: StimulusSpec) {}

    /// The white overlay of `slot`'s card; the engine sets its opacity every frame.
    func register(slot: String, layer: CALayer) {}
    func unregister(layer: CALayer) {}

    /// Run the display link (still screen) to measure the refresh rate before any flicker.
    func startMonitoring() {}
    func stopMonitoring() {}

    /// Flicker `slots` from the next frame on (frame 0 = cycle 0, the onset). Refused (returns
    /// false) without a stimulus or while the refresh check fails.
    @discardableResult
    func start(trialId: Int, slots: [String]) -> Bool { false }

    /// Every overlay dark at once.
    func stop() {}
}

/// A card's flicker overlay: a plain UIView whose layer the engine drives. Put it over the card
/// (`.overlay { FlickerOverlay(slot: "s1", engine: engine) }`); it never takes touches.
struct FlickerOverlay: UIViewRepresentable {
    let slot: String
    let engine: FlickerEngine

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        view.backgroundColor = .white
        view.layer.opacity = 0
        engine.register(slot: slot, layer: view.layer)
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {}
}
