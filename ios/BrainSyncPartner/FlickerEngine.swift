// The c-VEP flicker on the phone (INTERFACE-v2-draft.md section 9): one CADisplayLink locked to
// 60 Hz (never ProMotion's 120) drives a white layer over each card, frame by frame, outside
// SwiftUI. Cards register their layer by slot; the engine flickers the slots of a trial from the
// server's flicker phase, reports each cycle's first frame (phone time, for cycle_markers) and its
// own measured refresh rate (display_report), and stops at once on stop().
//
// Why it is built this way:
// - The code is in frames, so the frame index comes from the display link's targetTimestamp
//   relative to the onset frame's, rounded to the measured period (CVEPCode.frameIndex), never from
//   counting callbacks: a dropped frame skips one frame of the code instead of shifting the rest.
// - Opacity changes made in the callback are drawn for the frame at targetTimestamp, so that is the
//   presentation time a cycle marker carries (converted from CACurrentMediaTime to Unix seconds).
// - The bridge decodes against the Python code (brainsync.vep.codes); CVEPCode is pinned to it by
//   Fixtures/cvep_stimulus_frames.json. FrameRateCheck refuses to flicker unless the screen really runs at a
//   steady 60 Hz.
// - Safety (PLAN rule 10): stop() darkens every layer in the same transaction, and the engine
//   stops by itself when the app resigns active.
// - Per frame: no allocation except the cycle batch (once per second). Layers are held weakly.

import SwiftUI
import UIKit
import QuartzCore
import BrainSyncKit

@MainActor
final class FlickerEngine {
    /// The refresh check: the measured rate, whether the phone may flicker, and why not.
    struct Report: Equatable {
        var refreshHz: Double
        var ok: Bool
        var message: String
    }

    /// The rate the display link asks for and the check expects (section 9: exactly 60 Hz).
    static let refreshHz: Double = 60

    /// The first frames of these cycles of trial `trialId` were on screen at these phone times
    /// (Unix seconds); `droppedFrames` so far in the trial. Called once per cycle, on the main actor.
    var onCycles: ((_ trialId: Int, _ cycles: [(cycle: Int, phoneTime: Double)], _ droppedFrames: Int) -> Void)?
    /// The refresh check changed (also called once after `startMonitoring`).
    var onReport: ((Report) -> Void)?
    /// The engine stopped trial `trialId` by itself (the app is leaving the foreground). The app
    /// should tell the server (`stop`), as for a tap on the screen.
    var onSelfStop: ((_ trialId: Int) -> Void)?

    /// The last refresh check; nil before the display link has measured anything.
    private(set) var report: Report?
    /// True between `start` and `stop`.
    private(set) var isFlickering = false
    /// Why the last `setStimulus` cannot be drawn; nil when it can (or none was set).
    private(set) var stimulusProblem: CVEPCode.Problem?

    private struct Overlay {
        let slot: String
        weak var layer: CALayer?
        /// The slot's lag while it flickers in the current trial; nil = still (dark).
        var lag: Int?
        /// The opacity last set, to skip unchanged frames.
        var shown: Float
    }

    private var spec: StimulusSpec?
    private var code: CVEPCode?
    private var overlays: [Overlay] = []
    private var link: CADisplayLink?
    private lazy var proxy = LinkProxy(engine: self)

    private var check = FrameRateCheck(expectedHz: FlickerEngine.refreshHz)
    private var verdict: FrameRateCheck.Verdict?
    private var framesSinceVerdict = 0
    private var publishNextReport = false

    // The current trial.
    private var trialId = 0
    private var awaitingOnset = false
    private var onsetTarget: CFTimeInterval = 0
    private var period: Double = 1 / FlickerEngine.refreshHz
    private var lastFrame = -1
    private var droppedFrames = 0
    private var nextCycle = 0

    init() {
        NotificationCenter.default.addObserver(proxy, selector: #selector(LinkProxy.willResignActive),
                                               name: UIApplication.willResignActiveNotification, object: nil)
    }

    /// The code to show (from the server's `stimulus` message).
    func setStimulus(_ spec: StimulusSpec) {
        guard spec != self.spec else { return }  // re-sent after every hello_ack
        if isFlickering { stop() }  // never mix two codes in one trial
        self.spec = spec
        do {
            code = try CVEPCode(spec: spec)
            stimulusProblem = nil
        } catch {
            code = nil
            stimulusProblem = error as? CVEPCode.Problem
        }
    }

    /// The white overlay of `slot`'s card; the engine sets its opacity every frame.
    func register(slot: String, layer: CALayer) {
        overlays.removeAll { $0.layer == nil }
        if let i = overlays.firstIndex(where: { $0.layer === layer }) {
            if overlays[i].slot == slot { return }
            overlays.remove(at: i)
        }
        let lag = isFlickering ? activeLag(of: slot) : nil
        overlays.append(Overlay(slot: slot, layer: layer, lag: lag, shown: 0))
        setOpacity(0, of: layer)
    }

    func unregister(layer: CALayer) {
        overlays.removeAll { $0.layer == nil || $0.layer === layer }
        setOpacity(0, of: layer)
    }

    /// Run the display link (still screen) to measure the refresh rate before any flicker.
    func startMonitoring() {
        guard link == nil else { return }
        check.reset()
        verdict = nil
        framesSinceVerdict = 0
        publishNextReport = true
        let link = CADisplayLink(target: proxy, selector: #selector(LinkProxy.tick(_:)))
        let hz = Float(Self.refreshHz)
        link.preferredFrameRateRange = CAFrameRateRange(minimum: hz, maximum: hz, preferred: hz)
        link.add(to: .main, forMode: .common)
        self.link = link
    }

    func stopMonitoring() {
        stop()
        link?.invalidate()
        link = nil
        verdict = nil
    }

    /// Flicker `slots` from the next frame on (frame 0 = cycle 0, the onset). Refused (returns
    /// false) without a stimulus or while the refresh check fails.
    @discardableResult
    func start(trialId: Int, slots: [String]) -> Bool {
        if isFlickering { stop() }
        guard let code, link != nil, let verdict, verdict.ok,
              abs(code.refreshHz - Self.refreshHz) <= 1,
              UIApplication.shared.applicationState == .active else { return false }
        let active = Set(slots.filter { code.lags[$0] != nil })
        guard !active.isEmpty else { return false }
        self.trialId = trialId
        period = verdict.framePeriod
        for i in overlays.indices {
            overlays[i].lag = active.contains(overlays[i].slot) ? code.lags[overlays[i].slot] : nil
        }
        awaitingOnset = true
        lastFrame = -1
        droppedFrames = 0
        nextCycle = 0
        isFlickering = true
        return true
    }

    /// Every overlay dark at once.
    func stop() {
        isFlickering = false
        awaitingOnset = false
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for i in overlays.indices {
            overlays[i].lag = nil
            overlays[i].shown = 0
            overlays[i].layer?.opacity = 0
        }
        CATransaction.commit()
    }

    // MARK: - Display link

    fileprivate func tick(_ link: CADisplayLink) {
        check.add(link.timestamp)
        framesSinceVerdict += 1
        if framesSinceVerdict >= 30 || (verdict == nil && check.intervalCount >= check.minIntervals) {
            evaluateCheck()
        }
        guard isFlickering, let code else { return }

        let target = link.targetTimestamp
        if awaitingOnset {
            onsetTarget = target
            awaitingOnset = false
        }
        let n = CVEPCode.frameIndex(elapsed: target - onsetTarget, framePeriod: period)
        guard n > lastFrame else { return }  // the same frame again
        if lastFrame >= 0 { droppedFrames += FrameRateCheck.droppedFrames(from: lastFrame, to: n) }
        lastFrame = n

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for i in overlays.indices {
            guard let layer = overlays[i].layer else { continue }
            let opacity: Float = overlays[i].lag.map { Float(code.opacity(lag: $0, frame: n)) } ?? 0
            if opacity != overlays[i].shown {
                layer.opacity = opacity
                overlays[i].shown = opacity
            }
        }
        CATransaction.commit()

        let cycleFrames = code.cycleFrames
        guard n >= nextCycle * cycleFrames else { return }
        // Presentation times in Unix seconds: the bridge between the two clocks, taken now.
        let unixMinusMedia = Date().timeIntervalSince1970 - CACurrentMediaTime()
        var cycles: [(cycle: Int, phoneTime: Double)] = []
        while nextCycle * cycleFrames <= n {  // several after a stall: one batch
            let frame = nextCycle * cycleFrames
            cycles.append((cycle: nextCycle, phoneTime: unixMinusMedia + target - Double(n - frame) * period))
            nextCycle += 1
        }
        onCycles?(trialId, cycles, droppedFrames)
    }

    private func evaluateCheck() {
        framesSinceVerdict = 0
        guard let v = check.evaluate() else { return }
        verdict = v
        let new = Report(refreshHz: v.measuredHz, ok: v.ok, message: v.message)
        let changed = report.map { $0.ok != new.ok || $0.message != new.message
                                   || abs($0.refreshHz - new.refreshHz) > 0.5 } ?? true
        guard changed || publishNextReport else { return }
        publishNextReport = false
        report = new
        onReport?(new)
    }

    fileprivate func willResignActive() {
        guard isFlickering else { return }
        stop()
        onSelfStop?(trialId)
    }

    private func activeLag(of slot: String) -> Int? {
        code?.lags[slot]
    }

    private func setOpacity(_ opacity: Float, of layer: CALayer) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        layer.opacity = opacity
        CATransaction.commit()
    }
}

/// The display link's and the notification's target. CADisplayLink retains its target, so the
/// engine is held weakly here; a link whose engine is gone invalidates itself.
@MainActor
private final class LinkProxy: NSObject {
    weak var engine: FlickerEngine?

    init(engine: FlickerEngine) {
        self.engine = engine
    }

    @objc func tick(_ link: CADisplayLink) {
        guard let engine else {
            link.invalidate()
            return
        }
        engine.tick(link)
    }

    @objc func willResignActive() {
        engine?.willResignActive()
    }
}

/// A card's flicker overlay: a plain UIView whose layer the engine drives. Put it over the card
/// (`.overlay { FlickerOverlay(slot: "s1", engine: engine) }`); it never takes touches.
struct FlickerOverlay: UIViewRepresentable {
    let slot: String
    let engine: FlickerEngine

    func makeCoordinator() -> FlickerEngine { engine }

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        view.backgroundColor = .white
        view.layer.opacity = 0
        engine.register(slot: slot, layer: view.layer)
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        engine.register(slot: slot, layer: view.layer)  // a no-op unless the slot changed
    }

    static func dismantleUIView(_ view: UIView, coordinator: FlickerEngine) {
        coordinator.unregister(layer: view.layer)
    }
}
