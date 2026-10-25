// The app's state: what the server last said, and the events going back. Mirrors ui/app.js; the
// server stays the owner of every decision (PLAN rule 6). With a c-VEP server (a `stimulus`
// message, INTERFACE-v2-draft.md section 9) this phone is also the user's screen: the model starts
// and stops the FlickerEngine from the server's trial phases, keeps the clock in step with the
// server (clock_ping / clock_pong) and sends the engine's cycle markers and refresh check back.

import Foundation
import Observation
import SwiftUI
import UIKit
import BrainSyncKit

/// One line in the conversation panel.
struct ConversationLine: Identifiable, Equatable {
    let id: Int
    let fromPartner: Bool
    let text: String
}

@MainActor
@Observable
final class AppModel {
    // MARK: Connection

    private(set) var link: ServerLink?
    /// The pair screen is shown on top of a saved link (the Pair button, a bad key).
    var showPairing = false
    /// A link opened from outside the app to a different server than the saved one; nothing
    /// changes until the partner confirms it (RootView's alert).
    private(set) var pendingLink: ServerLink?
    private(set) var connected = false
    /// Why the connection stopped for good: another partner page took over, or the key is wrong.
    private(set) var stopReason: UIConnection.StopReason?
    private(set) var connectionNote = "Connecting…"
    /// /api/config; nil until it is fetched (the accessors below fall back to the web defaults).
    private(set) var config: PageConfig?

    // MARK: What the server said

    private(set) var page: ShowPage?
    /// The trial phase name ("cue", "flicker", "rest", "baseline", "paused"), "" before the first
    /// trial.
    private(set) var phase = ""
    private(set) var pickedSlot: String?
    private(set) var stateName = ""
    private(set) var stateDetail = ""
    private(set) var status: UIStatus?
    private(set) var lines: [ConversationLine] = []
    /// When the current flicker started, for the selection timer.
    private(set) var trialStarted: Date?
    /// The partner's Mute, held by the server (the last listen message).
    private(set) var micMuted = false
    /// The current trial's id (the last `trial` message); nil before the first trial.
    private(set) var trialId: Int?

    // MARK: c-VEP on the phone (INTERFACE-v2-draft.md section 9)

    /// Draws the flicker over the Look to Speak cards, frame by frame outside SwiftUI. Only the
    /// model starts it (`updateFlicker`); anything unsafe stops it at once.
    let flicker = FlickerEngine()
    /// The phone's estimate of the server's clock, from clock_ping / clock_pong.
    private(set) var clock = ClockSync()
    /// The server's c-VEP code. nil: the laptop draws the flicker and this phone mirrors it still.
    private(set) var stimulus: StimulusSpec?
    /// The engine's last refresh check, as sent in display_report (Settings shows it).
    private(set) var displayReport: FlickerEngine.Report?
    /// The engine accepted `start` and has not been stopped since.
    private(set) var flickering = false
    /// Cycle markers not sent because no pong had come back yet: no markers without a clock.
    private(set) var markersWithoutClock = 0
    /// A new calibration score arrived and no page has flickered since: the state line shows it.
    private(set) var calibrationNotice = false
    /// The app is in front (scene phase active).
    private(set) var foreground = true
    /// The Look to Speak screen is up and not covered by the partner tools.
    private(set) var userScreenVisible = false

    // MARK: Local

    /// The flicker warning was accepted in this run of the app (rule 10).
    private(set) var accepted = false
    private(set) var lastSpokenId: String?
    private(set) var judged = ""

    @ObservationIgnored private var connection: UIConnection?
    @ObservationIgnored private var reportedMic = ""
    @ObservationIgnored private var nextLineId = 0
    @ObservationIgnored private var configTask: Task<Void, Never>?
    @ObservationIgnored private var wasInBackground = false
    /// "I understand, start" tapped while the socket was down: resume goes out on the next
    /// connect, if that comes within `pendingResumeS` (an old tap must not restart a session).
    @ObservationIgnored private var pendingResumeAt: Date?
    private static let pendingResumeS: TimeInterval = 15
    /// A reply that ended while the socket was down: its tts_done goes out on the next connect
    /// (the server ignores it unless it is still waiting for that reply).
    @ObservationIgnored private var pendingTtsDone: String?
    /// The last open Listen that arrived before the warning was accepted (the warning is where
    /// the partner learns that the mic listens); applied on accept.
    @ObservationIgnored private var pendingListen: Listen?
    /// The trial the engine flickers now.
    @ObservationIgnored private var flickerTrialId: Int?
    /// A trial stopped here (a tap, Stop, the app leaving the front): it never flickers again on
    /// this phone, whatever the server says late.
    @ObservationIgnored private var suppressedTrialId: Int?
    /// The clock pings while connected to a c-VEP server; nil when none run.
    @ObservationIgnored private var clockTask: Task<Void, Never>?
    @ObservationIgnored private var nextPingId = 0
    /// The engine's display link measures the refresh rate (the Look to Speak screen is up).
    @ObservationIgnored private var monitoring = false
    /// Clock pings right after a connect, `quickPingS` apart, then one every `pingS`.
    private static let quickPings = 5
    private static let quickPingS = 0.2
    private static let pingS = 2.0

    let speaker = Speaker()
    let listener = Listener()

    private static let linkDefaultsKey = "serverLink"

    init() {
        listener.onLine = { [weak self] text in
            self?.send(.partnerText(text: text, source: .stt))
        }
        listener.onStateChange = { [weak self] state in
            self?.reportMic(state)
        }
        listener.prepareSession = { [weak self] in
            self?.speaker.activateSession()
        }
        flicker.onReport = { [weak self] report in
            self?.reportDisplay(report)
        }
        flicker.onCycles = { [weak self] trialId, cycles, droppedFrames in
            self?.sendCycles(trialId: trialId, cycles: cycles, droppedFrames: droppedFrames)
        }
        if let data = UserDefaults.standard.data(forKey: Self.linkDefaultsKey),
           let saved = try? JSONDecoder().decode(ServerLink.self, from: data) {
            link = saved
            connect()
        }
    }

    // MARK: Pairing

    /// brainsync://pair?url=...&key=... (or a web phone link) opened from outside the app. A link
    /// to another server than the saved one waits for the partner's Pair (`pendingLink`): anyone
    /// can send a link, and pairing would send the conversation to that server.
    func open(_ url: URL) {
        guard let newLink = ServerLink(pairURL: url) ?? ServerLink(string: url.absoluteString) else { return }
        if let current = link, !current.sameServer(as: newLink) {
            pendingLink = newLink
            return
        }
        pair(with: newLink)
    }

    /// Cancel on the confirmation: the saved server stays.
    func cancelPending() {
        pendingLink = nil
    }

    /// Pairs with a link typed or pasted on the pair screen. Returns false if it is not a link.
    @discardableResult
    func pair(text: String) -> Bool {
        guard let newLink = ServerLink(string: text) else { return false }
        pair(with: newLink)
        return true
    }

    /// Pairs at once (the pair screen, or Pair on the confirmation of `pendingLink`).
    func pair(with newLink: ServerLink) {
        pendingLink = nil
        if let current = link, !current.sameServer(as: newLink) {
            forgetStimulus()  // another server: it says again whether it runs c-VEP
        }
        if let data = try? JSONEncoder().encode(newLink) {
            UserDefaults.standard.set(data, forKey: Self.linkDefaultsKey)
        }
        link = newLink
        showPairing = false
        connect()
    }

    // MARK: Connection

    /// (Re)connects from scratch; used on pairing, the Reconnect button and coming back to the
    /// foreground with a dead connection. A connect never sends resume (ui/app.js): the server
    /// re-sends its state, and a reconnect after STOP must never restart the flicker.
    func connect() {
        guard let link else { return }
        connection?.onEvent = nil
        connection?.stop()
        stopReason = nil
        connected = false
        connectionNote = "Connecting…"
        let conn = UIConnection(link: link)
        conn.onEvent = { [weak self] event in
            // UIConnection calls back on the main queue (its default callbackQueue).
            MainActor.assumeIsolated {
                self?.handle(event)
            }
        }
        connection = conn
        conn.start()
        fetchConfig()
    }

    private func fetchConfig() {
        guard let link else { return }
        configTask?.cancel()
        configTask = Task { [weak self] in
            do {
                let (data, response) = try await URLSession.shared.data(from: link.configURL)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
                let decoded = try JSONDecoder().decode(PageConfig.self, from: data)
                self?.applyConfig(decoded)
            } catch {
                // Keep the defaults; the next connect tries again.
            }
        }
    }

    private func applyConfig(_ newConfig: PageConfig) {
        config = newConfig
        let rate = Double(newConfig.ttsRate)
        speaker.rate = rate > 0 ? rate : 1.0
        let gap = Double(newConfig.sttGapS)
        let maxLine = Double(newConfig.sttMaxLineS)
        let echo = Double(newConfig.sttEchoS)
        listener.configure(gap: gap > 0 ? gap : 1.2, maxLine: maxLine > 0 ? maxLine : 15,
                           echo: echo > 0 ? echo : 4)
    }

    private func handle(_ event: UIConnection.Event) {
        switch event {
        case .connected:
            connected = true
            stopReason = nil
            connectionNote = ""
            reportedMic = ""  // tell the new connection what the mic is doing
            IdleTimer.hold("voice", true)
            fetchConfig()
            if let id = pendingTtsDone {
                pendingTtsDone = nil
                send(.ttsDone(utteranceId: id))
            }
            if let at = pendingResumeAt {
                pendingResumeAt = nil
                if accepted && Date().timeIntervalSince(at) < Self.pendingResumeS { send(.resume) }
            }
            // c-VEP: a fresh clock (the server may have restarted), the refresh check again.
            clock.reset()
            if let report = flicker.report ?? displayReport { sendReport(report) }
            if stimulus != nil { startClockPings() }
            updateFlicker()
        case .message(let message):
            apply(message)
        case .disconnected(let why):
            setDisconnected()
            connectionNote = why.isEmpty ? "Server disconnected, reconnecting…"
                                         : "Server disconnected (\(why)), reconnecting…"
        case .stopped(let reason):
            setDisconnected()
            stopReason = reason
            speaker.cancel()
            pendingResumeAt = nil
            pendingTtsDone = nil
            switch reason {
            case .replaced: connectionNote = "Replaced by another partner page"
            case .badKey: connectionNote = "Link expired — pair again"
            }
        }
    }

    private func setDisconnected() {
        connected = false
        IdleTimer.hold("voice", false)
        stopClockPings()
        updateFlicker()  // no server: dark at once (draft section 9)
        // The server re-sends the mic state when we are back.
        pendingListen = nil
        listener.stop("disconnected")
    }

    func send(_ message: ClientMessage) {
        guard connected, let connection else { return }
        connection.send(message)
    }

    // MARK: Server messages (ui/app.js handle())

    private func apply(_ message: ServerMessage) {
        switch message {
        case .showPage(let msg):
            page = msg
            updateFlicker()
        case .trial(let msg):
            trialId = msg.trialId
            setPhase(String(describing: msg.phase))
        case .highlight(let msg):
            pickedSlot = msg.slot
        case .speak(let msg):
            speakOut(msg)
        case .listen(let msg):
            listenFor(msg)
        case .state(let msg):
            applyState(msg)
        case .transcript(let msg):
            addLine(fromPartner: String(describing: msg.who) == "partner", text: msg.text)
        case .status(let msg):
            if let score = msg.calibrationAccuracy, score != status?.calibrationAccuracy {
                calibrationNotice = true
            }
            status = msg
        case .stimulus(let spec):
            stimulus = spec
            flicker.setStimulus(spec)
            if connected && clockTask == nil { startClockPings() }
            updateFlicker()
        case .clockPong(let pong):
            clock.add(tSend: pong.tPhone, tReceive: Date().timeIntervalSince1970, tServer: pong.tServer)
        case .unknown:
            break
        }
    }

    private func setPhase(_ next: String) {
        if next == "flicker" && phase != "flicker" { trialStarted = Date() }
        if next != "flicker" { trialStarted = nil }
        if next == "cue" { pickedSlot = nil }
        if next == "flicker" && page?.mode != .calibrate { calibrationNotice = false }
        phase = next
        updateFlicker()
    }

    private func applyState(_ msg: StateMessage) {
        stateName = msg.name
        stateDetail = msg.detail
        if msg.name != "SPEAKING" && speaker.isSpeaking {
            // The server leaves SPEAKING only after tts_done or its own max_speak_s timeout, so a
            // reply still playing here never reported its end (background, a stalled clip, an
            // interruption, STOP): silence it, or the mic stays shut until the next reply.
            speaker.cancel()
        }
        if msg.name == "PAUSED" || msg.name == "IDLE" {
            pendingTtsDone = nil
        }
    }

    private func speakOut(_ msg: Speak) {
        lastSpokenId = msg.utteranceId
        judged = ""
        pendingTtsDone = nil
        listener.stop("speaking")  // never listen while the phone talks
        listener.heardSpoken(msg.text)
        let audio = msg.audioUrl.flatMap { path -> URL? in
            path.isEmpty ? nil : link?.audioURL(path)
        }
        speaker.speak(text: msg.text, utteranceId: msg.utteranceId, audioURL: audio) { [weak self] id in
            guard let self else { return }
            if self.connected {
                self.send(.ttsDone(utteranceId: id))
            } else {
                self.pendingTtsDone = id  // sent on the next connect
            }
        }
    }

    // MARK: Listening (the server owns the mic state; ui/app.js listenFor)

    private func listenFor(_ msg: Listen) {
        micMuted = msg.muted
        let opens = msg.untilStopped || msg.seconds > 0
        if !opens {
            pendingListen = nil
            listener.stop(msg.muted ? "muted" : msg.reason)
            return
        }
        guard accepted else {
            pendingListen = msg  // opened once the warning is accepted
            return
        }
        if speaker.isSpeaking {
            // The server never opens the mic while it is SPEAKING (orchestrator._send_ui asserts
            // it), so a reply still playing here missed its end: the server has moved on.
            speaker.cancel()
        }
        if msg.untilStopped {
            listener.want(delay: msg.delayS)
        } else {
            listener.window(seconds: msg.seconds, delay: msg.delayS)
        }
    }

    private static let reportable: Set<String> = [
        "listening", "needs_tap", "denied", "nomic", "offline", "unsupported", "error", "stopped",
        "hidden", "muted", "speaking", "paused",
    ]

    private func reportMic(_ state: String) {
        let value = state == "sent" ? "listening" : state
        guard Self.reportable.contains(value), value != reportedMic, connected,
              let micState = MicState(rawValue: value) else { return }
        send(.micReport(state: micState, restarts: listener.restarts))
        reportedMic = value
    }

    func toggleMute() {
        send(.micSet(muted: !micMuted))
    }

    /// The "Restart mic" / "Listen" button (ui/app.js #ptt).
    func restartMic() {
        if speaker.isSpeaking { return }
        if sttMode == "always" && listener.untilStopped && listener.kick() { return }
        if micMuted { return }
        if listener.wanted && !listener.untilStopped {
            listener.stop("stopped")
        } else {
            listener.window(seconds: listenWindowS, delay: 0)
        }
    }

    // MARK: Controls

    /// "I understand, start": the first tap, so the permission prompts come from it too.
    func acceptWarning() {
        accepted = true
        speaker.activateSession()
        listener.requestPermissions()
        if let pending = pendingListen {
            pendingListen = nil
            listenFor(pending)
        }
        if connected {
            send(.resume)
        } else {
            pendingResumeAt = Date()  // sent on the next connect
        }
        updateFlicker()
    }

    /// STOP works before the warning is accepted too (a relaunch during a flickering session).
    /// The phone's own flicker goes dark first, without waiting for the server.
    func stopPressed() {
        stopFlickerNow()
        pendingResumeAt = nil
        send(.stop)
    }

    func resumePressed() {
        guard accepted else { return }
        speaker.activateSession()
        send(.resume)
    }

    var resumeEnabled: Bool {
        // ui/app.js: only when the server says Resume would work (resume_ready).
        accepted && connected && (status?.resumeReady ?? false)
    }

    func sendTyped(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        send(.partnerText(text: trimmed, source: .typed))
    }

    /// Keyboard mode: a tap on a box is that slot's key ("1" = s1).
    func tapSlot(_ slot: String) {
        guard keyboardMode, phase == "flicker" else { return }
        send(.key(String(slot.dropFirst())))
    }

    var keyboardMode: Bool { status?.keyboardMode ?? false }

    /// Settings' "Test the speaker": the phone's own voice says a fixed sentence. Not a reply
    /// (rule 2 is about replies), and only while no reply plays and the mic is off, so it never
    /// cuts a reply short and the mic never hears it as the partner.
    var canTestSpeaker: Bool { !speaker.isSpeaking && !listener.wanted }

    func testSpeaker() {
        guard canTestSpeaker else { return }
        speaker.speak(text: "This is the speaker test.", utteranceId: "",
                      audioURL: nil) { _ in }
    }

    /// Correct / Wrong for the last spoken reply (keys C / W on the laptop).
    func judge(correct: Bool) {
        guard lastSpokenId != nil, connected else { return }
        send(.key(correct ? "c" : "w"))
        judged = correct ? "Marked correct" : "Marked wrong"
    }

    func forgetLink() {
        connection?.onEvent = nil
        connection?.stop()
        connection = nil
        connected = false
        stopClockPings()
        updateFlicker()
        showPairing = true
        pendingListen = nil
        pendingResumeAt = nil
        pendingTtsDone = nil
        listener.stop("disconnected")
        speaker.cancel()
        IdleTimer.hold("voice", false)
    }

    /// Back from the pair screen to the saved server.
    func cancelPairing() {
        showPairing = false
        if !connected { connect() }
    }

    // MARK: App lifecycle

    func scenePhaseChanged(_ phase: ScenePhase) {
        switch phase {
        case .active:
            foreground = true
            speaker.activateSession()
            if wasInBackground, link != nil, !showPairing, stopReason == nil, !connected {
                connect()  // a socket that died in the background
            }
            wasInBackground = false
            listener.resumeFromBackground()
            updateFlicker()
        case .background:
            leaveForeground(pauseSession: true)
            wasInBackground = true
            // iOS stops the microphone in the background: say so (mic_report hidden), like a
            // hidden browser tab.
            listener.suspend()
        case .inactive:
            leaveForeground(pauseSession: false)
        @unknown default:
            break
        }
    }

    /// The app leaves the front (draft section 9): the flicker stops at once and Stop goes to the
    /// server if it was flickering. In the background the user's screen is gone, so a running
    /// c-VEP session is paused too; inactive (Control Center, the app switcher) only stops a
    /// flicker in progress.
    private func leaveForeground(pauseSession: Bool) {
        foreground = false
        let wasFlickering = flickering
        stopFlickerNow()
        let running = !["", "IDLE", "PAUSED"].contains(stateName)
        if wasFlickering || (pauseSession && isCodeParadigm && running) {
            pendingResumeAt = nil
            send(.stop)
        }
    }

    // MARK: c-VEP flicker (INTERFACE-v2-draft.md section 9)

    /// This phone is the user's screen (the server sent a c-VEP stimulus).
    var isCodeParadigm: Bool { stimulus != nil }

    /// A tap anywhere stops the flicker: while the server's trial flickers, or the engine does.
    var tapStops: Bool { isCodeParadigm && (flickering || phase == "flicker") }

    /// The Look to Speak screen appeared (true) or went away or was covered (false). The engine
    /// measures the refresh rate from the moment the screen is up, before any flicker.
    func setUserScreen(visible: Bool) {
        if visible && !monitoring {
            monitoring = true
            flicker.startMonitoring()
        }
        guard visible != userScreenVisible else { return }
        userScreenVisible = visible
        updateFlicker()
    }

    /// The Look to Speak screen is gone for good (another screen replaced it).
    func userScreenClosed() {
        setUserScreen(visible: false)
        if monitoring {
            monitoring = false
            flicker.stopMonitoring()
        }
    }

    /// A tap anywhere on the flickering screen: dark at once, then Stop to the server.
    func tapToStop() {
        stopPressed()
    }

    /// Settings > Recalibrate: the server runs the calibration trial before the next page.
    func recalibrate() {
        send(.calibrate)
    }

    /// Starts or stops the engine from what the server said: it flickers only in a trial's flicker
    /// phase, with the warning accepted, a page, a connection, the app in front and the Look to
    /// Speak screen up. Anything else stops it at once.
    private func updateFlicker() {
        let wanted = isCodeParadigm && accepted && connected && foreground && userScreenVisible
            && phase == "flicker" && trialId != nil && trialId != suppressedTrialId
            && (displayReport?.ok ?? true)
        if wanted, let page, let trialId {
            if flickering && flickerTrialId == trialId { return }
            flickering = flicker.start(trialId: trialId, slots: page.slots.map(\.slot))
            flickerTrialId = flickering ? trialId : nil
        } else if flickering || flicker.isFlickering {
            flicker.stop()
            flickering = false
            flickerTrialId = nil
        }
    }

    /// Every overlay dark now; a trial that was flickering never flickers again here. (A trial
    /// still in its cue is not marked: a glance at Control Center must not cost the next look.)
    private func stopFlickerNow() {
        if trialId != nil && (flickering || phase == "flicker") { suppressedTrialId = trialId }
        flicker.stop()
        flickering = false
        flickerTrialId = nil
    }

    private func reportDisplay(_ report: FlickerEngine.Report) {
        displayReport = report
        sendReport(report)
        if !report.ok { updateFlicker() }  // the engine refuses too; the server pauses on it
    }

    private func sendReport(_ report: FlickerEngine.Report) {
        send(.displayReport(refreshHz: report.refreshHz, ok: report.ok, message: report.message))
    }

    /// The engine drew the first frame of these cycles: their times on the server's clock go out
    /// as cycle_markers. Without a pong yet there is no clock, so nothing is sent (and counted).
    private func sendCycles(trialId: Int, cycles: [(cycle: Int, phoneTime: Double)], droppedFrames: Int) {
        guard clock.offset != nil else {
            markersWithoutClock += cycles.count
            return
        }
        let markers = cycles.compactMap { item in
            clock.serverTime(item.phoneTime).map { CycleMarker(cycle: item.cycle, tUnix: $0) }
        }
        guard !markers.isEmpty else { return }
        send(.cycleMarkers(trialId: trialId, markers: markers, droppedFrames: droppedFrames))
    }

    /// Five quick pings right after a connect, then one every two seconds while connected.
    private func startClockPings() {
        clockTask?.cancel()
        clockTask = Task { [weak self] in
            var sent = 0
            while !Task.isCancelled {
                guard let self, self.connected else { return }
                self.nextPingId += 1
                self.send(.clockPing(id: self.nextPingId, tPhone: Date().timeIntervalSince1970))
                sent += 1
                let wait = sent < Self.quickPings ? Self.quickPingS : Self.pingS
                try? await Task.sleep(for: .seconds(wait))
            }
        }
    }

    private func stopClockPings() {
        clockTask?.cancel()
        clockTask = nil
    }

    /// Another server: no stimulus until it sends one, a fresh clock, nothing flickering.
    private func forgetStimulus() {
        stimulus = nil
        clock.reset()
        stopClockPings()
        calibrationNotice = false
        updateFlicker()
    }

    // MARK: Helpers

    private func addLine(fromPartner: Bool, text: String) {
        nextLineId += 1
        lines.append(ConversationLine(id: nextLineId, fromPartner: fromPartner, text: text))
    }

    /// What the partner said last, big above the replies (nil before they said anything).
    var lastPartnerLine: String? {
        lines.last(where: { $0.fromPartner })?.text
    }

    var userName: String {
        let name = (config?.userName ?? "").trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? "User" : name
    }

    var sttMode: String {
        let mode = config?.sttMode ?? ""
        return mode.isEmpty ? "always" : mode
    }

    var listenWindowS: Double {
        let value = Double(config?.listenWindowS ?? 0)
        return value > 0 ? value : 10
    }

    var maxTrialS: Double {
        let value = Double(config?.maxTrialS ?? 0)
        return value > 0 ? value : 10
    }

    var banner: Banner {
        Banner.make(name: stateName, detail: stateDetail, mode: page.map { String(describing: $0.mode) })
    }

    /// The Look to Speak screen's state line, or the connection note while not connected.
    var stateLine: StateLine {
        guard connected else {
            let note = connectionNote.isEmpty ? "Connecting…" : connectionNote
            return StateLine(text: note, tone: stopReason == nil ? .warn : .stop)
        }
        return StateLine.make(name: stateName, detail: stateDetail,
                              mode: page.map { String(describing: $0.mode) },
                              accuracy: status?.calibrationAccuracy, notice: calibrationNotice)
    }

    /// The sentence being spoken now, or nil.
    var speakingSentence: String? {
        guard stateName == "SPEAKING" else { return nil }
        let text = stateDetail.isEmpty ? speaker.speakingText : stateDetail
        return text.isEmpty ? nil : text
    }

    var health: Health? {
        status.map { Health.make($0) }
    }
}

// MARK: - Pure view helpers (ports of ui/view_core.js)

enum Tone {
    case idle, cue, look, picked, say, warn, stop

    /// The banner's accent (ui/style.css .banner.tone-*): its left edge and its text.
    var color: Color {
        switch self {
        case .idle: return Palette.muted
        case .cue: return Palette.text
        case .look: return Palette.look
        case .picked: return Palette.pick
        case .say: return Palette.say
        case .warn: return Palette.warn
        case .stop: return Palette.danger
        }
    }
}

struct Banner: Equatable {
    let text: String
    let tone: Tone

    private static let table: [String: (String, Tone)] = [
        "IDLE": ("Waiting to start", .idle),
        "BASELINE": ("Baseline: keep still", .idle),
        "PREPARING": ("Finding replies…", .idle),
        "MENU_CUE": ("Read the options", .cue),
        "STIMULATING": ("Look at a reply, or More options", .look),
        "CONFIRMING": ("Say it, or Back?", .look),
        "SELECTED": ("Selected", .picked),
        "SPEAKING": ("Speaking", .say),
        "REST": ("Rest", .idle),
        "NOT_CAUGHT": ("Didn't catch that: look again", .warn),
        "PAUSED": ("Paused", .stop),
        "CALIBRATING": ("Calibrating: look at the left card", .look),
    ]

    static func make(name: String, detail: String, mode: String?) -> Banner {
        if name.isEmpty { return Banner(text: "Connecting…", tone: .idle) }
        let entry: (String, Tone) = table[name] ?? (name, .idle)
        var text = entry.0
        let tone = entry.1
        if name == "STIMULATING" && mode == "list" { text = "Look at SAY or NEXT" }
        if name == "STIMULATING" && mode == "halves" { text = "Look at the left or right card" }
        if name == "STIMULATING" && mode == "calibrate" { text = "Calibrating: look at the left card" }
        if !detail.isEmpty && name != "SPEAKING" { text += " — \(detail)" }
        return Banner(text: text, tone: tone)
    }
}

/// The Look to Speak screen's state line, in the still centre (ui/view_core.js status): empty
/// while the user reads, looks, confirms or hears the reply, so nothing new appears between the
/// cards; otherwise what is going on. A calibration score shows until the first page flickers,
/// and stays (as a warning) while it is below `lowScore`.
struct StateLine: Equatable {
    let text: String
    let tone: Tone

    static let empty = StateLine(text: "", tone: .idle)
    /// draft section 9: a calibration below this still runs, but the screen says so.
    static let lowScore = 0.7
    static let quiet: Set<String> = ["MENU_CUE", "STIMULATING", "CONFIRMING", "SELECTED", "SPEAKING",
                                     "REST", "CALIBRATING"]

    static func make(name: String, detail: String, mode: String?, accuracy: Double?,
                     notice: Bool) -> StateLine {
        if name == "NOT_CAUGHT" { return StateLine(text: "Didn't catch that", tone: .warn) }
        if !quiet.contains(name) {
            let banner = Banner.make(name: name, detail: detail, mode: mode)
            return StateLine(text: banner.text, tone: banner.tone)
        }
        if let accuracy, notice || accuracy < lowScore {
            let percent = Int((accuracy * 100).rounded())
            return accuracy < lowScore
                ? StateLine(text: "Calibrated: \(percent) % (low: Recalibrate in Settings)", tone: .warn)
                : StateLine(text: "Calibrated: \(percent) %", tone: .idle)
        }
        return .empty
    }
}

struct Health: Equatable {
    enum Level { case ok, warn, bad }
    let level: Level
    let text: String
    let flags: [String]

    static let micProblems: Set<String> = ["denied", "nomic", "offline", "unsupported", "error",
                                           "needs_tap", "hidden"]

    static func make(_ s: UIStatus) -> Health {
        var problems: [String] = []
        var warnings: [String] = []
        if !s.keyboardMode {
            if !s.decoderConnected { problems.append("No decoder") }
            else if !s.decoderOk { problems.append("Decoder not OK") }
            if s.decoderConnected && !s.displayOk { problems.append("Display off") }
            if s.decoderConnected && !s.boardOk { warnings.append("Board?") }
        }
        let mic = String(describing: s.mic)
        if micProblems.contains(mic) && mic != "hidden" {
            problems.append("Mic: \(MicCard.make(state: mic, heard: "", muted: false).title.lowercased())")
        }
        var llm = String(describing: s.llm)
        if llm.isEmpty { llm = s.llmOk ? "ok" : "off" }
        if llm == "off" { warnings.append("Fixed replies only (no Claude)") }
        else if llm == "failed" { warnings.append("Claude's last page failed: fixed list shown") }
        if s.micMuted { warnings.append("Mic muted") }
        var flags: [String] = []
        if s.fakeDecoder { flags.append("FAKE DECODER") }
        if s.keyboardMode { flags.append("KEYBOARD") }
        let level: Level = !problems.isEmpty ? .bad : (!warnings.isEmpty ? .warn : .ok)
        let text = !problems.isEmpty ? problems.joined(separator: " · ")
            : (!warnings.isEmpty ? "Ready · " + warnings.joined(separator: " · ") : "Ready")
        return Health(level: level, text: text, flags: flags)
    }
}

struct MicCard: Equatable {
    let title: String
    let detail: String
    let heard: String
    let tone: Tone

    private static let table: [String: (String, String)] = [
        "listening": ("Listening", "Speak normally: lines are sent when you pause."),
        "sent": ("Heard", ""),
        "stopped": ("Not listening", ""),
        "idle": ("Mic off", "The mic turns on once the session starts."),
        "reconnect": ("Not listening", ""),
        "speaking": ("Mic off", "Off while the reply plays, so it never hears itself."),
        "paused": ("Mic off (paused)", "Tap Listen to speak once."),
        "muted": ("Muted", "Nothing is heard or sent. Tap Unmute to listen."),
        "disconnected": ("Mic off", "The server is not connected."),
        "needs_tap": ("Allow the microphone", "Tap Restart mic to grant mic and speech access."),
        "hidden": ("Keep the app open", "iOS turns the mic off while the app is in the background."),
        "denied": ("Microphone blocked", "Allow Microphone and Speech Recognition in Settings, then tap Restart mic. Typing works."),
        "nomic": ("No microphone", "Plug the lapel mic in. Typing works."),
        "offline": ("Speech recognition offline", "Retrying. Typing works."),
        "unsupported": ("No speech recognition", "This iPhone cannot recognise English speech. Typing works."),
        "error": ("Speech recognition failed", "Tap Restart mic, or type below."),
    ]

    static func make(state: String, heard text: String, muted: Bool) -> MicCard {
        let state = muted ? "muted" : state
        let (title, detail) = table[state] ?? table["stopped"]!
        var tone: Tone = .idle
        if state == "listening" || state == "sent" { tone = .look }
        else if state == "muted" { tone = .picked }
        else if Health.micProblems.contains(state) { tone = .warn }
        let heard = !muted && (state == "listening" || state == "sent") ? text : ""
        return MicCard(title: title, detail: heard.isEmpty ? detail : "", heard: heard, tone: tone)
    }
}
