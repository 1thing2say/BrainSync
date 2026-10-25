import AppKit
import BrainPointCore
import QuartzCore

/// The flickering patches over other apps: one borderless, transparent, click-through,
/// non-activating panel per screen, above every normal window and menu, on all Spaces and
/// next to full-screen apps. It never makes BrainPoint the active app.
///
/// Targets come in global points with a top-left origin (AX coordinates) and are converted to
/// each screen's Cocoa coordinates (`cocoaRect`). Each patch is a gray square at luminance L on
/// a black border, with its label below it (above near the bottom edge). Luminance is frame
/// indexed as in clients/js/brainsync-client.js: frame n = round((t - t0) * refreshHz), with t
/// the display link's targetTimestamp (when the frame reaches the screen) and t0 that of the
/// first flickering frame, through `Luminance.value` at `refreshHz`. A target at or above half
/// the refresh would alias and is drawn still, as is everything when `flicker` is false
/// (luminance 0.5).
///
/// Callbacks: `onOnset(layoutID, unix s)` once per layout on its first flickering frame (a
/// pause and restart within the same layout sends none); `onFrames(hz, dropped)` every 5 s
/// while flickering, with the measured refresh (1 / mean frame interval, intervals outside
/// 0.5-1.5 times the median left out) and the running total of dropped frames since this
/// controller was made (intervals over 1.5 periods), as the browser's `frames` message;
/// `onHover(slot)` when the patch under the mouse changes (the panels take no mouse events,
/// so this hit-tests global and local mouse-moved events).
@MainActor
public final class OverlayController {
    public var onOnset: ((Int, Double) -> Void)?
    public var onFrames: ((Double, Int) -> Void)?
    public var onHover: ((String?) -> Void)?

    /// Running total of dropped frames while flickering (what onFrames reports).
    public private(set) var droppedTotal = 0
    /// Frames shown while flickering since this controller was made.
    public private(set) var flickerFrames = 0
    /// The latest refresh estimate (Hz): Core's RefreshMeter over the measuring panel's frame
    /// intervals while flickering, nil until 20 + 120 intervals were seen.
    public var measuredHz: Double? { meter.hz() }

    static let framesEvery: Double = 5.0
    static let stripHeight: CGFloat = 20

    private var panels: [ScreenPanel] = []
    private var targets: [OverlayTarget] = []
    private var flicker = false
    private var refreshHz = 60.0
    private var layoutID = -1
    private var t0: CFTimeInterval?
    private var onsetSent = false
    private var lastMeasureTs: CFTimeInterval?
    private var lastFramesAt: CFTimeInterval?
    private var meter = RefreshMeter()
    private var stripText = ""
    private var stripColor = NSColor.systemGray
    private var hoverSlot: String?
    private var mouseMonitors: [Any] = []
    private var screenToken: NSObjectProtocol?
    private let greys: [CGColor] = (0...255).map { i in
        let v = CGFloat(i) / 255
        return CGColor(srgbRed: v, green: v, blue: v, alpha: 1)
    }

    public init() {}

    // MARK: - public API

    public func show(targets: [OverlayTarget], flicker: Bool, refreshHz: Double, layoutID: Int) {
        ensurePanels()
        let changed = targets != self.targets || layoutID != self.layoutID
            || refreshHz != self.refreshHz
        if layoutID != self.layoutID { onsetSent = false }
        if changed || !flicker || !self.flicker { t0 = nil }
        self.targets = targets
        self.flicker = flicker
        self.refreshHz = refreshHz > 0 ? refreshHz : 60
        self.layoutID = layoutID
        if changed { layoutPatches() }
        drawStill()
        updateRunning()
        startMouse()
        hitTest(at: NSEvent.mouseLocation)
    }

    public func hide() {
        targets = []
        flicker = false
        t0 = nil
        layoutPatches()
        updateRunning()
        stopMouse()
        if hoverSlot != nil { hoverSlot = nil; onHover?(nil) }
    }

    public func setStrip(text: String, color: NSColor) {
        stripText = text
        stripColor = color
        ensurePanels()
        layoutStrip()
        updateRunning()
    }

    /// A global rect (points, top-left origin from the primary screen's top) in Cocoa
    /// coordinates (bottom-left origin).
    public static func cocoaRect(fromTopLeft r: CGRect) -> CGRect {
        let h = primaryHeight()
        return CGRect(x: r.minX, y: h - r.maxY, width: r.width, height: r.height)
    }

    /// A Cocoa point (e.g. NSEvent.mouseLocation) in global top-left coordinates.
    public static func topLeftPoint(fromCocoa p: CGPoint) -> CGPoint {
        CGPoint(x: p.x, y: primaryHeight() - p.y)
    }

    static func primaryHeight() -> CGFloat {
        // The primary screen (the menu-bar one) is the one at the Cocoa origin.
        let s = NSScreen.screens.first { $0.frame.origin == .zero } ?? NSScreen.screens.first
        return s?.frame.height ?? 0
    }

    // MARK: - panels

    private func ensurePanels() {
        if screenToken == nil {
            screenToken = NotificationCenter.default.addObserver(
                forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated { self?.rebuildPanels() }
            }
        }
        if panels.isEmpty { rebuildPanels() }
    }

    private func rebuildPanels() {
        for p in panels { p.close() }
        panels = NSScreen.screens.enumerated().map { i, screen in
            ScreenPanel(screen: screen, index: i, owner: self)
        }
        t0 = nil
        lastMeasureTs = nil
        layoutPatches()
        layoutStrip()
        updateRunning()
    }

    private func layoutPatches() {
        for p in panels { p.setPatches(targets, greys: greys) }
    }

    private func layoutStrip() {
        let primary = panels.first { $0.screen.frame.origin == .zero } ?? panels.first
        for p in panels { p.setStrip(p === primary ? stripText : "", color: stripColor) }
    }

    /// Links run only on panels with patches while flickering; panels are on screen only while
    /// they have something to show.
    private func updateRunning() {
        let measureIdx = measurePanelIndex()
        for (i, p) in panels.enumerated() {
            p.setVisible(!p.patches.isEmpty || !p.stripEmpty)
            p.setLinkRunning(flicker && (!p.patches.isEmpty || i == measureIdx))
        }
        if !flicker { lastMeasureTs = nil }
    }

    /// The panel whose link measures the refresh: the first one with patches.
    private func measurePanelIndex() -> Int {
        panels.firstIndex { !$0.patches.isEmpty } ?? 0
    }

    private func drawStill() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for p in panels {
            for patch in p.patches { patch.fill.backgroundColor = greys[128] }
        }
        CATransaction.commit()
    }

    // MARK: - per frame

    fileprivate func tick(panel: ScreenPanel, link: CADisplayLink) {
        guard flicker, !targets.isEmpty else { return }
        let t = link.targetTimestamp
        if t0 == nil {
            t0 = t
            lastFramesAt = t
            if !onsetSent {
                onsetSent = true
                let unix = Date().timeIntervalSince1970 + (t - CACurrentMediaTime())
                onOnset?(layoutID, unix)
            }
        }
        guard let t0 else { return }
        if panel.index == measurePanelIndex() {
            if let last = lastMeasureTs, t > last {
                let dt = t - last
                meter.push(dt)
                if FlickerMath.isDropped(interval: dt, refresh: refreshHz) { droppedTotal += 1 }
            }
            lastMeasureTs = t
            flickerFrames += 1
            if let lf = lastFramesAt, t - lf >= Self.framesEvery {
                lastFramesAt = t
                onFrames?(meter.hz() ?? refreshHz, droppedTotal)
            }
        }
        let n = FlickerMath.frameIndex(t: t, t0: t0, refresh: refreshHz)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for patch in panel.patches {
            let tg = patch.target
            var lum = Luminance.still
            if FlickerMath.canShow(freq: tg.freq, refresh: refreshHz) {
                lum = Luminance.value(freq: tg.freq, phase: tg.phase, frame: n, refresh: refreshHz)
            }
            let i = max(0, min(255, Int((lum * 255).rounded())))
            patch.fill.backgroundColor = greys[i]
        }
        CATransaction.commit()
    }

    // MARK: - hover

    private func startMouse() {
        guard mouseMonitors.isEmpty, onHover != nil else { return }
        let mask: NSEvent.EventTypeMask = [.mouseMoved, .leftMouseDragged, .rightMouseDragged]
        if let g = NSEvent.addGlobalMonitorForEvents(matching: mask, handler: { [weak self] _ in
            MainActor.assumeIsolated { self?.hitTest(at: NSEvent.mouseLocation) }
        }) { mouseMonitors.append(g) }
        if let l = NSEvent.addLocalMonitorForEvents(matching: mask, handler: { [weak self] e in
            MainActor.assumeIsolated { self?.hitTest(at: NSEvent.mouseLocation) }
            return e
        }) { mouseMonitors.append(l) }
    }

    private func stopMouse() {
        for m in mouseMonitors { NSEvent.removeMonitor(m) }
        mouseMonitors = []
    }

    private func hitTest(at cocoa: CGPoint) {
        guard onHover != nil else { return }
        let p = Self.topLeftPoint(fromCocoa: cocoa)
        // The last-drawn target wins where patches overlap, as it is on top.
        let slot = targets.last { t in
            let pad = ScreenPanel.border(for: t.rect)
            return t.rect.insetBy(dx: -pad, dy: -pad).contains(p)
        }?.slot
        if slot != hoverSlot {
            hoverSlot = slot
            onHover?(slot)
        }
    }
}

// MARK: - one screen

@MainActor
private final class ScreenPanel {
    struct Patch { let target: OverlayTarget; let fill: CALayer }

    let screen: NSScreen
    let index: Int
    private(set) var patches: [Patch] = []
    private(set) var stripEmpty = true
    private let panel: NSPanel
    private let root = CALayer()
    private let patchLayer = CALayer()
    private let stripLayer = CALayer()
    private let stripText = CATextLayer()
    private var link: CADisplayLink?
    private let proxy: LinkProxy
    private var visible = false

    static func border(for r: CGRect) -> CGFloat {
        max(3, (0.06 * min(r.width, r.height)).rounded())
    }

    init(screen: NSScreen, index: Int, owner: OverlayController) {
        self.screen = screen
        self.index = index
        proxy = LinkProxy()
        let p = NSPanel(contentRect: screen.frame, styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = false
        p.ignoresMouseEvents = true
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.becomesKeyOnlyIfNeeded = true
        p.animationBehavior = .none
        // Above menus and pop-ups too, so menu items can carry patches.
        p.level = .screenSaver
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.setFrame(screen.frame, display: false)
        panel = p

        let view = NSView(frame: NSRect(origin: .zero, size: screen.frame.size))
        root.frame = view.bounds
        root.backgroundColor = .clear
        root.addSublayer(patchLayer)
        root.addSublayer(stripLayer)
        patchLayer.frame = root.bounds
        stripLayer.isHidden = true
        stripLayer.addSublayer(stripText)
        view.layer = root          // layer-hosting: we own the layer tree
        view.wantsLayer = true
        p.contentView = view

        proxy.owner = owner
        proxy.panel = self
        let l = view.displayLink(target: proxy, selector: #selector(LinkProxy.step(_:)))
        let maxFps = Float(screen.maximumFramesPerSecond)
        if maxFps > 0 { l.preferredFrameRateRange = CAFrameRateRange(minimum: maxFps, maximum: maxFps, preferred: maxFps) }
        l.isPaused = true
        l.add(to: .main, forMode: .common)
        link = l
    }

    func close() {
        link?.invalidate()
        link = nil
        panel.orderOut(nil)
        panel.close()
    }

    func setVisible(_ v: Bool) {
        guard v != visible else { return }
        visible = v
        if v { panel.orderFrontRegardless() } else { panel.orderOut(nil) }
    }

    func setLinkRunning(_ run: Bool) {
        link?.isPaused = !run
    }

    fileprivate func step(_ link: CADisplayLink, owner: OverlayController) {
        owner.tick(panel: self, link: link)
    }

    /// Screen-local Cocoa rect of a global top-left rect, or nil when off this screen.
    private func local(_ r: CGRect) -> CGRect? {
        let c = OverlayController.cocoaRect(fromTopLeft: r)
        guard c.intersects(screen.frame) else { return nil }
        return c.offsetBy(dx: -screen.frame.minX, dy: -screen.frame.minY)
    }

    func setPatches(_ targets: [OverlayTarget], greys: [CGColor]) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        patchLayer.sublayers?.forEach { $0.removeFromSuperlayer() }
        patches = []
        let scale = screen.backingScaleFactor
        let size = screen.frame.size
        let fontSize = max(13, (0.018 * size.height).rounded())
        let font = NSFont.systemFont(ofSize: fontSize, weight: .semibold)
        for t in targets {
            guard let r = local(t.rect) else { continue }
            let pad = Self.border(for: t.rect)
            let frame = r.insetBy(dx: -pad, dy: -pad)
            let fill = CALayer()
            fill.frame = frame
            fill.backgroundColor = greys[128]
            fill.borderColor = CGColor(gray: 0, alpha: 1)
            fill.borderWidth = pad
            patchLayer.addSublayer(fill)
            patches.append(Patch(target: t, fill: fill))
            guard !t.label.isEmpty else { continue }
            let text = NSAttributedString(string: t.label, attributes: [.font: font, .foregroundColor: NSColor.white])
            let tw = ceil(text.size().width) + 12, th = ceil(text.size().height) + 6
            let gap: CGFloat = 4
            var y = frame.minY - gap - th                  // below (Cocoa y is up)
            if y < 0 { y = frame.maxY + gap }              // else above
            if y + th > size.height { y = max(0, frame.minY + 2) }   // else inside
            let x = max(0, min(size.width - tw, frame.midX - tw / 2))
            let bg = CALayer()
            bg.frame = CGRect(x: x, y: y, width: tw, height: th)
            bg.backgroundColor = CGColor(gray: 0, alpha: 0.8)
            bg.cornerRadius = 3
            let tl = CATextLayer()
            tl.string = text
            tl.contentsScale = scale
            tl.frame = CGRect(x: 6, y: 3, width: tw - 12, height: th - 6)
            bg.addSublayer(tl)
            patchLayer.addSublayer(bg)
        }
        CATransaction.commit()
    }

    func setStrip(_ text: String, color: NSColor) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        stripEmpty = text.isEmpty
        stripLayer.isHidden = text.isEmpty
        if !text.isEmpty {
            // A centered pill just under the menu bar, not across the window's toolbar.
            let vf = screen.visibleFrame.offsetBy(dx: -screen.frame.minX, dy: -screen.frame.minY)
            let font = NSFont.systemFont(ofSize: 12, weight: .semibold)
            let s = NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: NSColor.white])
            let w = min(vf.width - 20, ceil(s.size().width) + 20)
            let h = OverlayController.stripHeight
            stripLayer.frame = CGRect(x: vf.midX - w / 2, y: vf.maxY - h, width: w, height: h)
            stripLayer.backgroundColor = color.withAlphaComponent(0.9).cgColor
            stripLayer.cornerRadius = 5
            stripLayer.maskedCorners = [.layerMinXMinYCorner, .layerMaxXMinYCorner]
            stripText.string = s
            stripText.contentsScale = screen.backingScaleFactor
            stripText.truncationMode = .end
            stripText.frame = CGRect(x: 10, y: (h - ceil(s.size().height)) / 2, width: w - 20,
                                     height: ceil(s.size().height))
        }
        CATransaction.commit()
    }
}

/// The display link's target (it must be an NSObject with an @objc selector).
@MainActor
private final class LinkProxy: NSObject {
    weak var owner: OverlayController?
    weak var panel: ScreenPanel?

    @objc func step(_ link: CADisplayLink) {
        guard let owner, let panel else { return }
        panel.step(link, owner: owner)
    }
}
