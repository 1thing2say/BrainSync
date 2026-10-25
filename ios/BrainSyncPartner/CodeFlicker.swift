// The c-VEP flicker on the phone (docs/CVEP-STANDARD.md): one CADisplayLink locked to 60 Hz
// paints the registered squares white or black each frame from the code, and reports the onset,
// each cycle's first flip and the frames report the server decodes with (PROTOCOL-v3 section 6).
//
// Why a display link and layers, not SwiftUI state: a bit is one frame (16.7 ms). A SwiftUI
// update per frame would re-run view bodies and could land a frame late; setting a layer's
// background inside the link's callback reaches the screen at that frame's targetTimestamp.
// The frame index comes from that timestamp (FrameClock), so a dropped frame skips its bits
// instead of shifting the code, exactly as the browser and the PsychoPy display do.
//
// Safety (PLAN rule 10): the driver flickers only between start() and stop(); the app calls
// start() only after the flicker warning was accepted. Leaving the foreground (resign active,
// background) stops it at once and calls onSafetyStop so the app can tell the server.

import BrainSyncKit
import QuartzCore
import SwiftUI
import UIKit

@MainActor
@Observable
final class CodeFlickerDriver {
    // MARK: Observable state (updated at most ~10 times a second, never per frame)

    /// The squares are flickering (between start and stop).
    private(set) var isFlickering = false
    /// The layout being flickered (the onset and cycle callbacks carry it).
    private(set) var layoutId: Int?
    /// The measured refresh (Hz), nil until about 140 frames were seen (probe or flicker).
    private(set) var measuredHz: Double?
    /// Late frames while flickering since this driver was made (the frames report's count).
    private(set) var dropped = 0
    /// Cycle starts drawn too late to report.
    private(set) var missedCycles = 0
    /// The frame index on screen now (nil while still); throttled, for a debug readout.
    private(set) var liveFrame: Int?
    /// Cycle starts reported since the last start().
    private(set) var cyclesReported = 0
    /// The display link runs without flicker to measure the refresh.
    private(set) var isProbing = false
    /// The code being drawn (the standard, or the server's welcome code).
    private(set) var code: CodeVep.Code = .standard

    // MARK: Callbacks (main actor)

    /// The first flickering frame of a layout: (layoutId, its flip as Unix seconds).
    @ObservationIgnored var onOnset: ((_ layoutId: Int, _ unixTime: Double) -> Void)?
    /// Each cycle's first flip: (layoutId, cycle index n, Unix seconds). PROTOCOL-v3 `cycle`.
    @ObservationIgnored var onCycle: ((_ layoutId: Int, _ n: Int, _ unixTime: Double) -> Void)?
    /// Every 5 s while flickering: (measured refresh Hz, running dropped count). `frames`.
    @ObservationIgnored var onFrames: ((_ refreshHz: Double, _ dropped: Int) -> Void)?
    /// The flicker was stopped because the app left the foreground (tell the server: pause).
    @ObservationIgnored var onSafetyStop: (() -> Void)?

    // MARK: Internals

    @ObservationIgnored private var clock: FrameClock
    @ObservationIgnored private let nominalHz: Double
    @ObservationIgnored private let toleranceHz: Double
    @ObservationIgnored private var link: CADisplayLink?
    @ObservationIgnored private var squares: [ObjectIdentifier: Square] = [:]
    @ObservationIgnored private var litColor: CGColor = UIColor.white.cgColor
    @ObservationIgnored private let darkColor: CGColor = UIColor.black.cgColor
    @ObservationIgnored private var lastSync: CFTimeInterval = -.infinity
    @ObservationIgnored private var framesSincePublish = 0
    @ObservationIgnored private let observers = FlickerObserverBag()

    /// How often (s) the media-time to Unix-time offset is sampled again.
    private static let resyncEvery: CFTimeInterval = 5
    /// Publish the observable readouts every this many frames (6 = 10 Hz at 60 Hz).
    private static let publishEvery = 6

    private final class Square {
        weak var layer: CALayer?
        var slot: String
        var lag: Int
        var lit: Bool?
        init(layer: CALayer, slot: String, lag: Int) {
            self.layer = layer
            self.slot = slot
            self.lag = lag
        }
    }

    /// `nominalHz` / `toleranceHz` are the welcome's `refresh_hz` / `refresh_tolerance_hz`.
    init(nominalHz: Double = 60, toleranceHz: Double = 1) {
        self.nominalHz = nominalHz
        self.toleranceHz = toleranceHz
        self.clock = FrameClock(nominalHz: nominalHz, toleranceHz: toleranceHz)
        let center = NotificationCenter.default
        for name in [UIApplication.willResignActiveNotification, UIApplication.didEnterBackgroundNotification] {
            observers.add(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.safetyStop() }
            })
        }
    }

    // MARK: - Configuration

    /// The code to draw: the welcome's `code.bits` and `cycle_frames`. An invalid code (a bit
    /// that is not 0 or 1, or `cycleFrames` != bit count) falls back to the standard, as the
    /// browser does. A new cycle length stops the flicker (its count would be wrong).
    func configure(bits: [UInt8], cycleFrames: Int, lagFrames: Int = CodeVep.lagFrames, fill: String = "#ffffff") {
        configure(code: CodeVep.Code(bits: bits, lagFrames: lagFrames, cycleFrames: cycleFrames,
                                     trainCycles: nil, fill: fill))
    }

    /// The same from a decoded welcome `code` object (nil: the standard).
    func configure(code new: CodeVep.Code?) {
        let next = CodeVep.Code.orStandard(new)
        if next.cycleFrames != code.cycleFrames {
            stop()
            clock = FrameClock(nominalHz: nominalHz, toleranceHz: toleranceHz, cycleFrames: next.cycleFrames)
            measuredHz = nil   // the new clock measures again
        }
        code = next
        litColor = Self.color(hex: next.fill) ?? UIColor.white.cgColor
        repaint()
    }

    // MARK: - Flicker

    /// Flicker on for `layoutId`: frame 0 (the onset) is the next frame the display link draws.
    /// A new layout restarts the count (PROTOCOL-v3: n counts from each layout's first frame).
    /// Refused while the app is not active (nothing flickers in the background).
    func start(layoutId: Int) {
        guard UIApplication.shared.applicationState == .active else { return }
        if isFlickering, self.layoutId == layoutId { return }
        self.layoutId = layoutId
        isFlickering = true
        cyclesReported = 0
        clock.start()
        sync(force: true)
        ensureLink()
    }

    /// Flicker off: every square is drawn still (white) right now, not on the next frame.
    func stop() {
        guard isFlickering || clock.isRunning else { return }
        isFlickering = false
        clock.stop()
        liveFrame = nil
        repaint()
        updateLinkPause()
    }

    /// Run the display link without flicker so `measuredHz` fills in (the 60 Hz check before
    /// the first flicker). The squares stay still.
    func startProbe() {
        isProbing = true
        ensureLink()
    }

    func stopProbe() {
        isProbing = false
        updateLinkPause()
    }

    /// What to tell the user about the measured refresh (ui/flicker_core.js `refreshProblem`),
    /// or nil while it is unknown or within the tolerance of the nominal rate. The code shows
    /// one bit a frame, so the decoder's template holds at that rate only.
    var refreshProblem: String? {
        guard let hz = measuredHz, abs(hz - nominalHz) > toleranceHz else { return nil }
        return "This screen runs at \(Int(hz.rounded())) Hz, not \(Int(nominalHz.rounded())) Hz: turn Low Power Mode off and close other apps."
    }

    // MARK: - Squares

    /// Adds (or updates) a square: its layer is painted from now on, by `lag` frames.
    func register(_ layer: CALayer, slot: String, lag: Int) {
        let key = ObjectIdentifier(layer)
        if let sq = squares[key] {
            sq.slot = slot
            if sq.lag != lag { sq.lag = lag; sq.lit = nil }
        } else {
            squares[key] = Square(layer: layer, slot: slot, lag: lag)
        }
        repaint()
    }

    func unregister(_ layer: CALayer) {
        squares.removeValue(forKey: ObjectIdentifier(layer))
    }

    // MARK: - Frames

    fileprivate func step(_ link: CADisplayLink) {
        let target = link.targetTimestamp
        sync(force: false)
        let tick = clock.tick(target)
        paint(frame: tick.frame)

        if let layoutId {
            if let onset = tick.onset { onOnset?(layoutId, clock.unixTime(media: onset)) }
            for c in tick.cycles {
                cyclesReported += 1
                onCycle?(layoutId, c.n, clock.unixTime(media: c.mediaTime))
            }
        }
        if let report = tick.frames { onFrames?(report.refreshHz, report.dropped) }

        framesSincePublish += 1
        if framesSincePublish >= Self.publishEvery || tick.onset != nil {
            framesSincePublish = 0
            publish(frame: tick.frame)
        }
    }

    private func publish(frame: Int?) {
        if measuredHz != clock.measuredHz { measuredHz = clock.measuredHz }
        if dropped != clock.dropped { dropped = clock.dropped }
        if missedCycles != clock.missedCycles { missedCycles = clock.missedCycles }
        if liveFrame != frame { liveFrame = frame }
    }

    /// Paints every square for `frame` (nil: still, white). Only changed layers are touched.
    private func paint(frame: Int?) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for (key, sq) in squares {
            guard let layer = sq.layer else { squares.removeValue(forKey: key); continue }
            let lit = frame.map { CodeVep.isLit(bits: code.bits, frame: $0, lag: sq.lag) } ?? true
            if sq.lit != lit {
                sq.lit = lit
                layer.backgroundColor = lit ? litColor : darkColor
            }
        }
        CATransaction.commit()
    }

    /// Paints the current state at once (registration, stop, a new code).
    private func repaint() {
        for sq in squares.values { sq.lit = nil }
        paint(frame: isFlickering ? clock.frame : nil)
    }

    private func safetyStop() {
        let was = isFlickering
        stop()
        stopProbe()
        if was { onSafetyStop?() }
    }

    // MARK: - Display link

    private func ensureLink() {
        if link == nil {
            let proxy = LinkProxy()
            proxy.driver = self
            let l = CADisplayLink(target: proxy, selector: #selector(LinkProxy.step(_:)))
            // One bit a frame: lock to 60 Hz (a ProMotion screen would otherwise vary its rate).
            l.preferredFrameRateRange = CAFrameRateRange(minimum: 60, maximum: 60, preferred: 60)
            l.add(to: .main, forMode: .common)
            link = l
        }
        updateLinkPause()
    }

    private func updateLinkPause() {
        guard let link else { return }
        let paused = !(isFlickering || isProbing)
        if link.isPaused != paused { link.isPaused = paused }
    }

    /// Samples media time against Unix time (every `resyncEvery` s, or now when forced).
    private func sync(force: Bool) {
        let media = CACurrentMediaTime()
        guard force || media - lastSync >= Self.resyncEvery else { return }
        clock.syncClock(media: media, unix: Date().timeIntervalSince1970)
        lastSync = media
    }

    private static func color(hex: String) -> CGColor? {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        return UIColor(red: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                       blue: CGFloat(v & 0xff) / 255, alpha: 1).cgColor
    }
}

/// The display link's target: CADisplayLink retains its target, so a weak hop keeps the driver
/// free to go away; the link is invalidated on the first frame after it has.
private final class LinkProxy: NSObject {
    weak var driver: CodeFlickerDriver?

    @objc func step(_ link: CADisplayLink) {
        MainActor.assumeIsolated {
            guard let driver else { link.invalidate(); return }
            driver.step(link)
        }
    }
}

/// Removes the driver's notification observers when it goes away.
private final class FlickerObserverBag {
    private var tokens: [NSObjectProtocol] = []

    func add(_ token: NSObjectProtocol) { tokens.append(token) }

    deinit {
        tokens.forEach(NotificationCenter.default.removeObserver)
    }
}

// MARK: - SwiftUI

/// One c-VEP square: a plain view whose layer the driver paints every frame. Size and place it
/// like any view (`.frame`, `.aspectRatio(1, contentMode: .fit)`); cue boxes and labels are the
/// app's overlays. Still (not flickering) it is white.
struct CodeFlickerSquare: UIViewRepresentable {
    /// The slot this square stands for (`t0`, `t1`, `t2`), for the driver's bookkeeping.
    let slot: String
    /// Frames of delay: 0, 21 or 42 (`CodeVep.lag(ofTarget:)`).
    let lag: Int
    let driver: CodeFlickerDriver

    func makeUIView(context: Context) -> CodeSquareView {
        let view = CodeSquareView()
        view.isUserInteractionEnabled = false
        view.isAccessibilityElement = true
        view.accessibilityLabel = "Flicker target \(slot)"
        view.layer.backgroundColor = UIColor.white.cgColor
        view.driver = driver
        driver.register(view.layer, slot: slot, lag: lag)
        return view
    }

    func updateUIView(_ view: CodeSquareView, context: Context) {
        if view.driver !== driver {
            view.driver?.unregister(view.layer)
            view.driver = driver
        }
        view.accessibilityLabel = "Flicker target \(slot)"
        driver.register(view.layer, slot: slot, lag: lag)
    }

    static func dismantleUIView(_ view: CodeSquareView, coordinator: ()) {
        view.driver?.unregister(view.layer)
        view.driver = nil
    }
}

/// The UIView behind `CodeFlickerSquare`.
final class CodeSquareView: UIView {
    weak var driver: CodeFlickerDriver?
}
