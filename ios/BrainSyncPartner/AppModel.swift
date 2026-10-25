// The app's state: what the server last said, and the events going back. Mirrors ui/app.js; the
// server stays the owner of every decision (PLAN rule 6).

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
            UIApplication.shared.isIdleTimerDisabled = true
            fetchConfig()
            if let id = pendingTtsDone {
                pendingTtsDone = nil
                send(.ttsDone(utteranceId: id))
            }
            if let at = pendingResumeAt {
                pendingResumeAt = nil
                if accepted && Date().timeIntervalSince(at) < Self.pendingResumeS { send(.resume) }
            }
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
        UIApplication.shared.isIdleTimerDisabled = false
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
        case .trial(let msg):
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
            status = msg
        case .unknown:
            break
        }
    }

    private func setPhase(_ next: String) {
        if next == "flicker" && phase != "flicker" { trialStarted = Date() }
        if next != "flicker" { trialStarted = nil }
        if next == "cue" { pickedSlot = nil }
        phase = next
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
    }

    /// STOP works before the warning is accepted too (a relaunch during a flickering session).
    func stopPressed() {
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
        showPairing = true
        pendingListen = nil
        pendingResumeAt = nil
        pendingTtsDone = nil
        listener.stop("disconnected")
        speaker.cancel()
        UIApplication.shared.isIdleTimerDisabled = false
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
            speaker.activateSession()
            if wasInBackground, link != nil, !showPairing, stopReason == nil, !connected {
                connect()  // a socket that died in the background
            }
            wasInBackground = false
            listener.resumeFromBackground()
        case .background:
            wasInBackground = true
            // iOS stops the microphone in the background: say so (mic_report hidden), like a
            // hidden browser tab.
            listener.suspend()
        default:
            break
        }
    }

    // MARK: Helpers

    private func addLine(fromPartner: Bool, text: String) {
        nextLineId += 1
        lines.append(ConversationLine(id: nextLineId, fromPartner: fromPartner, text: text))
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

    var health: Health? {
        status.map { Health.make($0) }
    }
}

// MARK: - Pure view helpers (ports of ui/view_core.js)

enum Tone {
    case idle, cue, look, picked, say, warn, stop

    var color: Color {
        switch self {
        case .idle: return Color(white: 0.25)
        case .cue: return Color(red: 0.16, green: 0.36, blue: 0.62)
        case .look: return Color(red: 0.12, green: 0.50, blue: 0.40)
        case .picked: return Color(red: 0.58, green: 0.42, blue: 0.08)
        case .say: return Color(red: 0.40, green: 0.25, blue: 0.62)
        case .warn: return Color(red: 0.62, green: 0.38, blue: 0.08)
        case .stop: return Color(red: 0.62, green: 0.14, blue: 0.14)
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
    ]

    static func make(name: String, detail: String, mode: String?) -> Banner {
        if name.isEmpty { return Banner(text: "Connecting…", tone: .idle) }
        let entry: (String, Tone) = table[name] ?? (name, .idle)
        var text = entry.0
        let tone = entry.1
        if name == "STIMULATING" && mode == "list" { text = "Look at SAY or NEXT" }
        if !detail.isEmpty && name != "SPEAKING" { text += " — \(detail)" }
        return Banner(text: text, tone: tone)
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
