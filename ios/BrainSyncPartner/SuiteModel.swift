// The suite's state: one PROTOCOL-v3 connection to brainsync.server (code mode, slots mode), one
// c-VEP flicker driver, and the app the user is in. The phone is the screen the user looks at:
// it draws two squares, never more (slots t0, t1 at lags 0 and 31 frames), and each app decides what
// they mean (SuiteCore, CameraCore, ReelsCore). The server owns training and decoding; this only
// turns its selections into each app's effects and gates the flicker.
//
// Safety (PLAN rule 10): nothing flickers before the warning is accepted in this run; Stop, the
// app leaving the foreground, a lost connection or a non-flicker phase stop it at once.

import Foundation
import Observation
import SwiftUI
import UIKit
import BrainSyncKit

@MainActor
@Observable
final class SuiteModel {
    // MARK: Connection

    private(set) var link: V3Link?
    private(set) var connected = false
    private(set) var welcome: V3Welcome?
    /// What the connection line says ("" when all is well).
    private(set) var note = ""
    /// Why the connection stopped for good (wrong token, replaced, busy, rejected hello).
    private(set) var stopReason: V3Connection.StopReason?

    // MARK: What the server said

    private(set) var phase: V3Phase?
    private(set) var layout: V3Layout?
    private(set) var decision: V3Decision?
    private(set) var training: V3TrainProgress?
    private(set) var trained: V3Trained?
    private(set) var status: V3Status?
    /// The last server error that did not end the connection (refresh, protocol...).
    private(set) var serverError: V3Error?

    // MARK: Session

    /// The flicker warning was accepted in this run of the app (rule 10).
    private(set) var accepted = false
    /// Measuring the refresh before the first connect (the 60 Hz check).
    private(set) var checkingRefresh = false
    /// Stop was pressed (or the app left the foreground): the server holds `pause`.
    private(set) var paused = false
    /// The square the server last picked, briefly ringed so the user sees what happened.
    private(set) var pickedSlot: String?
    /// Fake board only: the square a tap pointed the fake EEG at.
    private(set) var gazeSlot: String?
    /// A link opened from outside the app to another server, waiting for the user's Pair (anyone
    /// can send a link; pairing would hand the headset to that server).
    private(set) var pendingLink: V3Link?

    // MARK: Apps

    private(set) var app: SuiteApp = .home
    private(set) var home = SuiteHomeCore()
    private(set) var camera: CameraCore
    private(set) var reels = ReelsCore(count: ReelContent.count)
    /// Counts up after each photo: the viewfinder's shutter effect.
    private(set) var shutterTrigger = 0
    /// Dim the playing reel while choosing (moving content next to the squares weakens the
    /// response). On by default.
    var dimReels: Bool {
        didSet { UserDefaults.standard.set(dimReels, forKey: Self.dimReelsKey) }
    }

    let driver = CodeFlickerDriver()
    let capture = CameraCapture()
    let photos = PhotoStore()

    @ObservationIgnored private var connection: V3Connection?
    @ObservationIgnored private var pickTask: Task<Void, Never>?
    @ObservationIgnored private var refreshTask: Task<Void, Never>?
    /// Why the suite itself holds the server paused without the user's Stop: "voice" (its screen
    /// has no squares), "settings" (a sheet covers them). The flicker is off while any is held.
    private(set) var holds: Set<String> = []
    /// When the app last changed: a look still held on a square must not act in the new app.
    @ObservationIgnored private var switchedAt = Date.distantPast
    /// The link's takeover applies to its first connect only, never to later reconnects.
    @ObservationIgnored private var takeoverOnce = false

    private static let linkKey = "suiteLink"
    private static let dimReelsKey = "reels.dimWhileChoosing"
    /// How long the picked square stays ringed.
    private static let pickRingS: TimeInterval = 0.6
    /// Selections this soon after an app switch are dropped: the server keeps one layout across
    /// apps, so a look held on the square that opened an app would commit again (after its 1.5 s
    /// cooldown) as that app's action on the same square.
    private static let switchSettleS: TimeInterval = 2.5
    /// The refresh check runs at least this long, so a stale measurement is replaced.
    private static let refreshMinS: TimeInterval = 2.5
    /// The longest refresh check before giving up and trusting the nominal 60 Hz.
    private static let refreshCheckS: TimeInterval = 4

    init() {
        dimReels = UserDefaults.standard.object(forKey: Self.dimReelsKey) as? Bool ?? true
        camera = CameraCore()
        camera = CameraCore(photoCount: photos.count)   // photos exists only once self does
        if let data = UserDefaults.standard.data(forKey: Self.linkKey),
           let saved = try? JSONDecoder().decode(V3Link.self, from: data) {
            link = V3Link(base: saved.base, token: saved.token) ?? saved
        }
        driver.onOnset = { [weak self] layoutId, t in
            self?.send(.onset(layoutId: layoutId, tClient: t))
        }
        driver.onCycle = { [weak self] layoutId, n, t in
            self?.send(.cycle(layoutId: layoutId, n: n, tClient: t))
        }
        driver.onFrames = { [weak self] hz, dropped in
            self?.send(.frames(refreshHz: hz, dropped: dropped))
        }
        driver.onSafetyStop = { [weak self] in
            self?.stopPressed()
        }
    }

    // MARK: Derived state

    /// The server is training the template: the user looks at the cue square.
    var isTraining: Bool { phase?.name == .train }

    /// The labels on the two squares now (one per slot).
    var labels: [String] {
        switch app {
        case .home, .voice: return home.labels
        case .camera: return camera.labels
        case .reels: return reels.labels
        }
    }

    var enabled: [Bool] {
        switch app {
        case .home, .voice: return home.enabled
        case .camera: return camera.enabled
        case .reels: return reels.enabled
        }
    }

    /// What t0 is called while training (the cue square).
    var trainingLabel: String {
        switch app {
        case .home, .voice: return home.trainingLabel
        case .camera: return camera.trainingLabel
        case .reels: return reels.trainingLabel
        }
    }

    /// The square the server asks the user to look at while training (t0).
    var cueSlot: String? { isTraining ? (phase?.cue ?? SuiteSlots.ids[0]) : nil }

    /// The standard's status line (CodeVep): training progress, the decoder's lead and choice,
    /// or the template's score.
    var statusLine: String {
        if isTraining {
            return CodeVep.trainingLine(cue: trainingLabel.uppercased(), cycle: training?.cycle ?? 0,
                                        of: training?.of ?? phase?.cycles ?? CodeVep.trainCycles)
        }
        if let decision, decision.layoutId == layout?.layoutId {
            return CodeVep.statusLine(lead: decision.lead, committed: decision.committed.map(label(of:)),
                                      changed: decision.changed)
        }
        if let trained {
            return CodeVep.trainedLine(accuracy: trained.accuracy, chance: trained.chance)
        }
        return phase?.text ?? ""
    }

    /// The score shown under each square (nil before the first decision of this layout).
    func score(of slot: String) -> Double? {
        guard let decision, decision.layoutId == layout?.layoutId else { return nil }
        return decision.scores[slot]
    }

    /// Whether `slot` was this decision's best target (the `<` mark).
    func isBest(_ slot: String) -> Bool {
        guard let decision, decision.layoutId == layout?.layoutId else { return false }
        return decision.raw == slot
    }

    func label(of slot: String) -> String {
        guard let i = SuiteSlots.index(of: slot), i < labels.count else { return slot }
        return labels[i].uppercased()
    }

    /// Whether the squares flicker now: warning accepted, connected, not paused, a flicker phase
    /// with a layout, and not in Voice (whose screen has no squares).
    var flickerWanted: Bool {
        accepted && connected && !paused && holds.isEmpty && app != .voice
            && welcome?.paradigm == .code
            && phase?.flicker == true && !(layout?.targets.isEmpty ?? true)
    }

    /// The square's lag in frames: the layout's, else its position times the server's code
    /// spacing (the welcome's `lag_frames`), else the two-square standard's (0, 31).
    func lag(of index: Int) -> Int {
        let slot = SuiteSlots.ids[index]
        if let lag = layout?.target(slot)?.lag { return lag }
        if let spacing = welcome?.code?.lagFrames, spacing > 0 { return index * spacing }
        return SuiteSlots.lags[index]
    }

    // MARK: Pairing

    /// A v3 link pasted or opened (`#token=` link, bare URL, brainsync://v3?...). Returns false if
    /// the text is not one.
    @discardableResult
    func pair(text: String) -> Bool {
        guard let newLink = V3Link(string: text) else { return false }
        pair(with: newLink)
        return true
    }

    func pair(with newLink: V3Link) {
        pendingLink = nil
        // Saved without takeover: only this pairing's first connect may take the headset over.
        let saved = V3Link(base: newLink.base, token: newLink.token) ?? newLink
        if let data = try? JSONEncoder().encode(saved) {
            UserDefaults.standard.set(data, forKey: Self.linkKey)
        }
        link = saved
        takeoverOnce = newLink.takeover
        if accepted && !checkingRefresh { connect() }
    }

    /// A link opened from outside the app: pairs at once with no saved server or the same one,
    /// otherwise waits for the user's Pair (`pendingLink`).
    func open(link newLink: V3Link) {
        if let current = link, current.base != newLink.base {
            pendingLink = newLink
            return
        }
        pair(with: newLink)
    }

    func cancelPending() {
        pendingLink = nil
    }

    func forgetLink() {
        disconnect()
        stopReason = nil
        note = ""
        link = nil
        UserDefaults.standard.removeObject(forKey: Self.linkKey)
    }

    // MARK: Start (the warning's button)

    /// The warning was accepted: measure the refresh (the code is one bit a frame, so it must be
    /// 60 Hz), then connect. The hello says `accepted: true` only from here on.
    func acceptWarning() {
        accepted = true
        paused = false
        checkRefreshThenConnect()
    }

    /// Measures the refresh (at least `refreshMinS`, so an old reading is replaced), asks for the
    /// camera before anything flickers (a permission alert would otherwise make the app inactive
    /// mid-flicker, which stops and pauses the session), then connects if the screen is at 60 Hz.
    /// Also the "Check again" button after a failed check.
    func checkRefreshThenConnect() {
        refreshTask?.cancel()
        checkingRefresh = true
        driver.startProbe()
        refreshTask = Task { [weak self] in
            await self?.capture.requestAccess()
            let start = Date()
            while let self, !Task.isCancelled {
                let elapsed = Date().timeIntervalSince(start)
                if elapsed >= Self.refreshCheckS { break }
                if elapsed >= Self.refreshMinS && self.driver.measuredHz != nil { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            guard let self, !Task.isCancelled else { return }
            self.driver.stopProbe()
            self.checkingRefresh = false
            if self.driver.refreshProblem == nil && self.connection == nil { self.connect() }
        }
    }

    // MARK: Connection

    /// (Re)connects from scratch. `takeover` replaces the screen that holds the headset (after
    /// `.busy`).
    func connect(takeover: Bool = false) {
        guard let link, accepted else { return }
        disconnect()
        stopReason = nil
        note = "Connecting…"
        let conn = V3Connection(link: link, hello: { [weak self] in
            MainActor.assumeIsolated {
                self?.makeHello() ?? .controller(slots: SuiteSlots.ids, refreshHz: 60)
            }
        })
        conn.onEvent = { [weak self] event in
            // V3Connection calls back on the main queue (its default callbackQueue).
            MainActor.assumeIsolated { self?.handle(event) }
        }
        connection = conn
        conn.start(takeover: takeover || takeoverOnce)
        takeoverOnce = false
    }

    func disconnect() {
        connection?.onEvent = nil
        connection?.stop()
        connection = nil
        setDisconnected()
    }

    private func makeHello() -> V3Hello {
        let bounds = UIScreen.main.bounds
        return .controller(slots: SuiteSlots.ids, refreshHz: driver.measuredHz ?? 60,
                           viewport: V3Viewport(w: bounds.width, h: bounds.height,
                                                dpr: UIScreen.main.scale))
    }

    private func handle(_ event: V3Connection.Event) {
        switch event {
        case .connected(let w):
            connected = true
            welcome = w
            note = w.paradigm == .code ? ""
                : "The server runs the frequency flicker: start it with --paradigm code"
            serverError = nil
            IdleTimer.hold("suite", true)
            if let code = w.code {
                driver.configure(bits: code.bits.map { UInt8(clamping: $0) }, cycleFrames: code.cycleFrames,
                                 lagFrames: code.lagFrames, fill: code.fill)
            } else {
                driver.configure(code: nil)
            }
            // A Stop pressed while the socket was down, Voice or a sheet open: still paused.
            if paused || !holds.isEmpty { connection?.send(.pause) }
        case .message(let message):
            apply(message)
        case .disconnected(let why):
            setDisconnected()
            note = why.isEmpty ? "Server disconnected, reconnecting…"
                               : "Server disconnected (\(why)), reconnecting…"
        case .stopped(let reason):
            setDisconnected()
            connection = nil
            stopReason = reason
            switch reason {
            case .badToken: note = "The link's token is wrong or expired: pair again"
            case .replaced: note = "Another screen took over the headset"
            case .busy: note = "Another screen holds the headset"
            case .rejected(let why): note = "The server refused this phone: \(why)"
            }
        }
        updateFlicker()
    }

    private func setDisconnected() {
        connected = false
        welcome = nil
        phase = nil
        layout = nil
        decision = nil
        gazeSlot = nil     // the server forgets the fake gaze with the connection
        IdleTimer.hold("suite", false)
        driver.stop()
    }

    private func apply(_ message: V3ServerMessage) {
        switch message {
        case .phase(let p):
            phase = p
            if p.name != .train { training = nil }
        case .layout(let l):
            if l.layoutId != layout?.layoutId { decision = nil }
            layout = l
        case .decision(let d):
            decision = d
        case .selection(let s):
            select(s)
        case .train(let t):
            training = t
            trained = nil
        case .trained(let t):
            trained = t
            training = nil
        case .status(let s):
            status = s
        case .error(let e):
            serverError = e
        case .welcome, .pong, .ping, .unknown:
            break
        }
        updateFlicker()
    }

    private func send(_ message: V3ClientMessage) {
        guard connected, let connection else { return }
        connection.send(message)
    }

    // MARK: Flicker

    /// Starts or stops the squares to match `flickerWanted`. The same layout keeps its frame
    /// count across train → run; a pause stops it, and the next start begins a new count (the
    /// server decodes each cycle by its time, from the moment it flickers again).
    private func updateFlicker() {
        if flickerWanted, let layoutId = layout?.layoutId {
            driver.start(layoutId: layoutId)
        } else {
            driver.stop()
        }
    }

    // MARK: Stop / Resume

    /// Stop: the flicker off on the phone at once, then `pause` to the server.
    func stopPressed() {
        paused = true
        driver.stop()
        send(.pause)
        updateFlicker()
    }

    /// Resume after Stop (the server trains again after a pause of 120 s or more).
    func resumePressed() {
        guard accepted else { return }
        paused = false
        if connection == nil {
            checkRefreshThenConnect()
        } else if holds.isEmpty {
            send(.resume)
        }
        updateFlicker()
    }

    /// The suite holds the server paused for `reason` (Voice, a sheet over the squares) or lets
    /// it go; the user's own Stop stays as it is.
    func hold(_ reason: String, _ on: Bool) {
        let was = holds.isEmpty
        if on { holds.insert(reason) } else { holds.remove(reason) }
        if was && !holds.isEmpty && !paused {
            send(.pause)
        } else if !was && holds.isEmpty && !paused {
            send(.resume)
        }
        updateFlicker()
    }

    // MARK: Apps

    /// Opens an app by touch or by a Home selection. Voice pauses the c-VEP session (its screen
    /// has no squares) until the user comes back.
    func open(_ next: SuiteApp) {
        guard next != app else { return }
        leave(app)
        app = next
        home.highlight(next)   // back on Home, Open names the app just used
        decision = nil
        switchedAt = Date()
        if gazeSlot != nil {
            gazeSlot = nil
            send(.gaze(slot: nil))
        }
        switch next {
        case .camera:
            camera.reset()
            capture.start()
        case .voice:
            hold("voice", true)
        case .home, .reels:
            break
        }
        updateFlicker()
    }

    func goHome() { open(.home) }

    private func leave(_ old: SuiteApp) {
        switch old {
        case .camera:
            capture.stop()
        case .voice:
            hold("voice", false)
        case .home, .reels:
            break
        }
    }

    /// The decoder picked a square of the current layout: the current app does its thing.
    private func select(_ s: V3Selection) {
        guard s.layoutId == layout?.layoutId, !paused, holds.isEmpty, app != .voice,
              Date().timeIntervalSince(switchedAt) >= Self.switchSettleS else { return }
        ring(s.slot)
        switch app {
        case .home:
            switch home.select(s.slot) {
            case .open(let next)?: open(next)
            case .highlight?, nil: break   // Home's labels follow `home.current`
            }
        case .camera:
            if let effect = camera.select(s.slot) { perform(effect) }
        case .reels:
            if let effect = reels.select(s.slot) { perform(effect) }
        case .voice:
            break
        }
    }

    private func ring(_ slot: String) {
        pickedSlot = slot
        pickTask?.cancel()
        pickTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.pickRingS * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.pickedSlot = nil
        }
    }

    private func perform(_ effect: CameraCore.Effect) {
        switch effect {
        case .capture:
            Task { [weak self] in
                guard let self, let image = await self.capture.capture() else { return }
                self.shutterTrigger += 1
                if await self.photos.save(image) != nil { self.camera.photoAdded() }
            }
        case .flip:
            // The core only toggles its own idea of the lens; the capture knows whether a flip
            // can happen now, so it decides.
            capture.flip()
        case .delete(let photo, _):
            if !photos.delete(at: photo) {
                // The core already counted the photo gone: start it again from the store.
                camera = CameraCore(photoCount: photos.count, lens: camera.lens)
            }
        case .openMenu, .closeMenu, .openGallery, .showPhoto, .closeGallery, .confirmDelete, .keep:
            break   // the screens follow `camera.screen` and `camera.photoIndex`
        }
    }

    private func perform(_ effect: ReelsCore.Effect) {
        switch effect {
        case .scroll, .likeAndPause, .pause, .play:
            break   // the feed follows `reels.index`, `reels.liked` and `reels.paused`
        }
    }

    // MARK: Touch (the operator's hands, and the fake board)

    /// A tap on a square. With the fake board it points the fake EEG at that square (the server
    /// then picks it like a real look); with a headset a tap does nothing, so a touch never
    /// passes for a look.
    func tapSquare(_ slot: String) {
        guard welcome?.fake == true else { return }
        gazeSlot = gazeSlot == slot ? nil : slot
        send(.gaze(slot: gazeSlot))
    }

    /// The scene changed: reconnect a dead socket and restart the camera when back; stop the
    /// flicker and the camera when leaving (the driver also stops itself on resign active).
    func scenePhaseChanged(_ phase: ScenePhase) {
        switch phase {
        case .active:
            // Not while the 60 Hz check runs (it connects when done) or after it failed.
            if accepted, link != nil, connection == nil, stopReason == nil, !checkingRefresh,
               driver.refreshProblem == nil {
                connect()
            }
            if app == .camera { capture.start() }
            updateFlicker()
        case .background:
            // Leaving the screen always stops the session; Resume brings it back (rule 10).
            if accepted && !paused { stopPressed() }
            driver.stop()
            if app == .camera { capture.stop() }
        default:
            break
        }
    }
}

/// The screen stays awake while either connection (the suite's or Voice's) runs: one owner, so a
/// drop of one never lets the phone lock while the other is in use (a locked phone would stop the
/// flicker and pause the session).
@MainActor
enum IdleTimer {
    private static var reasons: Set<String> = []

    static func hold(_ reason: String, _ on: Bool) {
        if on { reasons.insert(reason) } else { reasons.remove(reason) }
        UIApplication.shared.isIdleTimerDisabled = !reasons.isEmpty
    }
}
