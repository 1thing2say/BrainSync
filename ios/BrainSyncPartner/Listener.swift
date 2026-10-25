// Speech in: the partner's voice, turned into lines for the server (partner_text, source stt). A
// port of createListener in ui/listen_core.js onto SFSpeechRecognizer and AVAudioEngine. The pure
// parts (line joining, the echo check, the restart backoff) come from BrainSyncKit's ListenCore.
//
// The server says when to listen (Listen): until_stopped = from delay_s on until the next listen,
// seconds > 0 = for that long after delay_s, neither = stop now. The app never listens while a
// reply plays (AppModel stops the listener before speaking).
//
// iOS differences from Chrome: a recognition task gives one growing transcription instead of
// final segments. A pause of stt_gap_s ends the segment: it goes into the LineAssembler, the line
// is sent, and a fresh task starts while the audio engine keeps running, so nothing is lost.

import AVFoundation
import Foundation
import Observation
import Speech
import BrainSyncKit

@MainActor
@Observable
final class Listener {
    /// listen_core.js states: listening, sent, stopped, needs_tap, denied, nomic, offline,
    /// unsupported, error, hidden, muted, speaking, paused, disconnected, ...
    private(set) var state = "stopped"
    /// What was heard so far in the current line (the mic card shows it while the partner talks).
    private(set) var heard = ""
    /// Recognition tasks started (sent with mic_report).
    private(set) var restarts = 0
    /// The server allows the mic now, or a window is open.
    private(set) var wanted = false
    @ObservationIgnored private var windowEnd: TimeInterval?

    var untilStopped: Bool { wanted && windowEnd == nil }

    /// A finished line to send as partner_text.
    @ObservationIgnored var onLine: ((String) -> Void)?
    /// Every state change (AppModel turns it into mic_report).
    @ObservationIgnored var onStateChange: ((String) -> Void)?
    /// Makes sure the shared audio session is set up and active (the Speaker owns it).
    @ObservationIgnored var prepareSession: (() -> Void)?

    /// The loop's fixed timings, in seconds (T in ui/listen_core.js).
    private typealias T = ListenTimes

    @ObservationIgnored private var gap: TimeInterval = 1.2
    @ObservationIgnored private var maxLine: TimeInterval = 15.0
    @ObservationIgnored private var echoWindow: TimeInterval = 4.0

    @ObservationIgnored private var oneLine = false
    @ObservationIgnored private var suspended = false
    @ObservationIgnored private var assembler = ListenCore.LineAssembler(gap: 1.2, maxLine: 15.0)
    /// The current task's transcription, not yet final.
    @ObservationIgnored private var segment = ""
    @ObservationIgnored private var segmentChanged: TimeInterval = 0
    @ObservationIgnored private var lineStarted = false
    @ObservationIgnored private var lastSpoken = ""
    @ObservationIgnored private var reopenedAt: TimeInterval = -.infinity
    @ObservationIgnored private var policy = ListenCore.RestartPolicy()
    @ObservationIgnored private var lastError: ListenCore.ListenError?

    private let recognizer: SFSpeechRecognizer? = {
        let r = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        r?.queue = .main
        return r
    }()
    @ObservationIgnored private var engine: AVAudioEngine?
    private let feed = RequestFeed()
    @ObservationIgnored private var task: SFSpeechRecognitionTask?
    @ObservationIgnored private var request: SFSpeechAudioBufferRecognitionRequest?
    @ObservationIgnored private var taskToken = 0
    @ObservationIgnored private var sessionStart: TimeInterval = 0
    @ObservationIgnored private var lastEvent: TimeInterval = 0
    @ObservationIgnored private var heardSpeech = false
    @ObservationIgnored private var timers: [String: Timer] = [:]
    /// A call, Siri or an alarm has the audio session; no session starts until it ends.
    @ObservationIgnored private var interrupted = false
    @ObservationIgnored private var interruptedAt: TimeInterval = 0
    /// The interruption's end is not always posted: after this long the watchdog tries again.
    private static let interruptionGiveUp: TimeInterval = 30
    @ObservationIgnored private var engineObserver: NSObjectProtocol?
    @ObservationIgnored private var sessionObservers: [NSObjectProtocol] = []
    /// Engines torn down in the last few seconds, kept alive: AVFoundation finishes a voice
    /// processing engine's teardown on its own queues after stop(), and freeing the engine under
    /// them crashed the app (EXC_BAD_ACCESS in objc_retain / objc_release, 2026-10-25).
    @ObservationIgnored private var retiredEngines: [(engine: AVAudioEngine, until: TimeInterval)] = []
    private static let retireS: TimeInterval = 5

    private func now() -> TimeInterval { ProcessInfo.processInfo.systemUptime }

    init() {
        // The web page restarts on 'devicechange'. Here a stopped AVAudioEngine (the lapel mic
        // plugged in or out, a Bluetooth route, a call or Siri) feeds the task nothing and reports
        // no error, so without these the mic stays deaf until the one-minute stall watchdog.
        let center = NotificationCenter.default
        sessionObservers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification,
                                                   object: nil, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            guard raw == AVAudioSession.RouteChangeReason.newDeviceAvailable.rawValue
                || raw == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue else { return }
            MainActor.assumeIsolated { self?.audioPathChanged() }
        })
        sessionObservers.append(center.addObserver(forName: AVAudioSession.interruptionNotification,
                                                   object: nil, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            let began = raw == AVAudioSession.InterruptionType.began.rawValue
            MainActor.assumeIsolated { self?.interruption(began: began) }
        })
    }

    // MARK: Public API (createListener's)

    func configure(gap: TimeInterval, maxLine: TimeInterval, echo: TimeInterval) {
        self.gap = gap
        self.maxLine = maxLine
        echoWindow = echo
        if assembler.pending.isEmpty {
            assembler = ListenCore.LineAssembler(gap: gap, maxLine: maxLine)
        }
    }

    /// The reply the phone is about to speak, for the echo check.
    func heardSpoken(_ text: String) {
        lastSpoken = text
    }

    /// Always mode: listen from `delay` on until told to stop. Again while on does nothing.
    func want(delay: TimeInterval) {
        if wanted && windowEnd == nil { return }
        stop("restart", quiet: true)
        open(delay: delay, seconds: 0)
    }

    /// Turn mode and the Listen button: listen for `seconds`, then stop; one line ends the turn.
    func window(seconds: TimeInterval, delay: TimeInterval) {
        stop("restart", quiet: true)
        open(delay: delay, seconds: seconds)
    }

    func stop(_ why: String = "stopped", quiet: Bool = false) {
        if why != "muted" && why != "restart" && !assembler.pending.isEmpty {
            // What the partner said just before the mic closed.
            if let line = assembler.takeLine(at: now(), force: true), passes(line) {
                onLine?(line)
            }
        }
        wanted = false
        windowEnd = nil
        oneLine = false
        clearAllTimers()
        cancelTask()
        stopAudio()
        assembler = ListenCore.LineAssembler(gap: gap, maxLine: maxLine)
        segment = ""
        lineStarted = false
        if !quiet { report(why) }
    }

    /// Start a task now (the Restart mic button, permissions granted, back in the foreground).
    @discardableResult
    func kick() -> Bool {
        guard wanted else { return false }
        suspended = false
        interrupted = false
        policy = ListenCore.RestartPolicy()
        cancelTask()
        clearTimer("restart")
        clearTimer("delay")
        startSession()
        if timers["watchdog"] == nil {
            setTimer("watchdog", T.watchdog) { [weak self] in self?.watchdog() }
        }
        return true
    }

    /// The app went to the background: iOS takes the mic away. Kept wanted, so kick() resumes.
    func suspend() {
        suspended = true
        cancelTask()
        stopAudio()
        clearTimer("restart")
        clearTimer("gap")
        report("hidden")
    }

    /// Back in the foreground: listen again if the server still wants the mic.
    func resumeFromBackground() {
        suspended = false
        interrupted = false  // the interruption's end is not always posted
        if wanted {
            kick()
        } else if state == "hidden" {
            report("stopped")
        }
    }

    /// Asks for speech recognition and microphone access (from a tap: the flicker warning).
    func requestPermissions() {
        Task { @MainActor [weak self] in
            await self?.askPermissions()
        }
    }

    /// The prompts themselves; returns once both are answered (the setup's Microphone step waits
    /// for it to show the result). It opens nothing by itself: kick() only runs when the server
    /// already wants the mic, and AppModel holds every listen back until the warning is accepted.
    func askPermissions() async {
        if SFSpeechRecognizer.authorizationStatus() == .notDetermined {
            _ = await Listener.requestSpeechAuthorization()
        }
        if AVAudioApplication.shared.recordPermission == .undetermined {
            _ = await AVAudioApplication.requestRecordPermission()
        }
        if wanted { kick() }
    }

    // MARK: Windows

    private func open(delay: TimeInterval, seconds: TimeInterval) {
        wanted = true
        policy = ListenCore.RestartPolicy()
        windowEnd = seconds > 0 ? now() + delay + seconds : nil
        oneLine = seconds > 0
        setTimer("delay", delay) { [weak self] in
            guard let self else { return }
            self.reopenedAt = self.now()
            self.startSession()
        }
        setTimer("watchdog", delay + T.watchdog) { [weak self] in self?.watchdog() }
        if seconds > 0 {
            setTimer("window", delay + seconds) { [weak self] in self?.closeWindow() }
        }
    }

    private func closeWindow() {
        if !segment.isEmpty {
            // Let the sentence finish, then send whatever there is.
            setTimer("window", T.grace) { [weak self] in
                guard let self else { return }
                self.finalizeSegment(at: self.now())
                self.flushLine(force: true)
                if self.wanted { self.stop("stopped") }
            }
            return
        }
        flushLine(force: true)
        if wanted { stop("stopped") }
    }

    // MARK: Sessions

    private func startSession() {
        clearTimer("restart")
        guard wanted, !suspended, !interrupted, task == nil else { return }
        sessionStart = now()
        lastEvent = now()
        heardSpeech = false

        switch SFSpeechRecognizer.authorizationStatus() {
        case .denied, .restricted:
            stop("denied")
            return
        case .notDetermined:
            report("needs_tap")  // the watchdog tries again; requestPermissions() kicks
            return
        default:
            break
        }
        switch AVAudioApplication.shared.recordPermission {
        case .denied:
            stop("denied")
            return
        case .undetermined:
            report("needs_tap")
            return
        default:
            break
        }
        guard let recognizer else {
            stop("unsupported")
            return
        }
        if !recognizer.isAvailable {
            lastError = .network
            report("offline")
            scheduleRestart()
            return
        }
        do {
            try startAudio()
        } catch {
            lastError = .noMic
            report("nomic")
            scheduleRestart()
            return
        }

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.taskHint = .dictation
        request.addsPunctuation = true
        if recognizer.supportsOnDeviceRecognition {
            request.requiresOnDeviceRecognition = true  // works offline, nothing leaves the phone
        }
        taskToken += 1
        let token = taskToken
        self.request = request
        feed.set(request)
        lastError = nil
        task = recognizer.recognitionTask(with: request,
                                          resultHandler: Listener.resultHandler(for: self, token: token))
        restarts += 1
        report("listening", heardText())
    }

    private func onRecognition(token: Int, result: SFSpeechRecognitionResult?, error: Error?) {
        guard token == taskToken, task != nil else { return }
        lastEvent = now()
        if let result {
            let text = result.bestTranscription.formattedString
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                heardSpeech = true
                policy = ListenCore.RestartPolicy()
                if Self.isReset(old: segment, new: text) {
                    // The recognizer started over after a pause: the old text was final.
                    finalizeSegment(at: segmentChanged)
                }
                segment = text
                segmentChanged = now()
                if !lineStarted {
                    lineStarted = true
                    setTimer("maxLine", maxLine) { [weak self] in self?.maxLineReached() }
                }
                if result.isFinal {
                    finalizeSegment(at: now())
                } else {
                    setTimer("gap", gap) { [weak self] in self?.gapElapsed() }
                }
                report("listening", heardText())
            }
        }
        if result?.isFinal == true || error != nil {
            taskEnded(error: error)
        }
    }

    /// The partner paused for stt_gap_s: the line is done. A fresh task takes the next one.
    private func gapElapsed() {
        let hadSegment = !segment.isEmpty
        finalizeSegment(at: segmentChanged)
        if hadSegment && task != nil {
            cancelTask()
            policy = ListenCore.RestartPolicy()
            startSession()
        }
        flushLine(force: true)
    }

    /// A monologue goes out after stt_max_line_s anyway.
    private func maxLineReached() {
        let hadSegment = !segment.isEmpty
        finalizeSegment(at: now())
        if hadSegment && task != nil {
            cancelTask()
            startSession()
        }
        flushLine(force: true)
    }

    /// The task ended by itself (a final result, an error, the recognizer's time limit).
    private func taskEnded(error: Error?) {
        task = nil
        request = nil
        feed.set(nil)
        finalizeSegment(at: segmentChanged)  // confirmed words carry over to the next task
        if !assembler.pending.isEmpty {
            setTimer("gap", gap) { [weak self] in self?.flushLine(force: true) }
        }
        lastError = classify(error)
        switch lastError {
        case .network?: report("offline")
        case .noMic?: report("nomic")
        default: break
        }
        scheduleRestart()
    }

    private func scheduleRestart() {
        guard wanted, !suspended else { return }
        if let windowEnd, now() >= windowEnd {
            closeWindow()
            return
        }
        let ran = now() - sessionStart
        guard let delay = policy.delay(afterSessionRan: ran, error: lastError, heard: heardSpeech) else {
            stop(lastError == .unsupported ? "unsupported" : "denied")
            return
        }
        setTimer("restart", delay) { [weak self] in self?.startSession() }
    }

    private func watchdog() {
        guard wanted else { return }
        if interrupted && now() - interruptedAt > Self.interruptionGiveUp {
            interrupted = false
        }
        if !suspended && !interrupted {
            if task == nil && timers["restart"] == nil && timers["delay"] == nil {
                startSession()
            } else if task != nil && engine?.isRunning != true {
                // The audio engine stopped under a running task (a missed route change).
                restartAudio()
            } else if task != nil && now() - lastEvent > T.stallAfter {
                // No event for a minute (long silence, or a stuck task): start a new one.
                finalizeSegment(at: segmentChanged)
                flushLine(force: true)
                cancelTask()
                lastError = nil
                scheduleRestart()
            }
        }
        if wanted {
            setTimer("watchdog", T.watchdog) { [weak self] in self?.watchdog() }
        }
    }

    private func classify(_ error: Error?) -> ListenCore.ListenError? {
        guard let error else { return nil }
        let status = SFSpeechRecognizer.authorizationStatus()
        if status == .denied || status == .restricted { return .denied }
        if AVAudioApplication.shared.recordPermission == .denied { return .denied }
        let ns = error as NSError
        // No speech heard (1110), or a task we cancelled (216, 301): a normal end.
        if [1110, 216, 301, 203].contains(ns.code) { return nil }
        if ns.domain == NSURLErrorDomain || recognizer?.isAvailable == false { return .network }
        return .other
    }

    // MARK: Lines

    private func finalizeSegment(at time: TimeInterval) {
        guard !segment.isEmpty else { return }
        assembler.addFinal(segment, at: time)
        segment = ""
    }

    @discardableResult
    private func flushLine(force: Bool) -> Bool {
        clearTimer("gap")
        guard let line = assembler.takeLine(at: now(), force: force) else {
            if segment.isEmpty { endLine() }
            return false
        }
        if segment.isEmpty { endLine() }
        guard passes(line) else {
            report("listening", heardText())
            return false
        }
        onLine?(line)
        report("sent", line)
        if oneLine { stop("done", quiet: true) }
        return true
    }

    private func endLine() {
        lineStarted = false
        clearTimer("maxLine")
    }

    private func passes(_ line: String) -> Bool {
        ListenCore.meaningful(line)
            && !ListenCore.isEcho(line, lastSpoken: lastSpoken, sinceReopen: now() - reopenedAt,
                                  echoWindow: echoWindow)
    }

    private func heardText() -> String {
        ListenCore.joinFinals([assembler.pending, segment].filter { !$0.isEmpty })
    }

    /// The new transcription is clearly shorter than the old one and not a prefix of it.
    private static func isReset(old: String, new: String) -> Bool {
        let o = ListenCore.norm(old)
        let n = ListenCore.norm(new)
        guard !o.isEmpty else { return false }
        return n.count + 4 < o.count && !o.hasPrefix(n)
    }

    private func report(_ newState: String, _ text: String = "") {
        if newState == state && text == heard { return }
        state = newState
        heard = text
        onStateChange?(newState)
    }

    // MARK: Audio

    /// A new or removed input or output device; several notifications arrive together, so they
    /// are coalesced.
    private func audioPathChanged() {
        guard wanted, !suspended, !interrupted else { return }
        setTimer("audioPath", 0.3) { [weak self] in self?.restartAudio() }
    }

    /// The engine's configuration changed. It stops itself then; one that kept running (voice
    /// processing settling after start) is left alone, so this never loops. Whether it stopped is
    /// read here, while the notification still holds the engine: the timer only rebuilds and
    /// never touches the old engine again (that crashed, 2026-10-25).
    private func engineConfigChanged(_ changed: AVAudioEngine?) {
        guard wanted, !suspended, !interrupted, timers["audioPath"] == nil else { return }
        guard let changed, changed === engine, !changed.isRunning else { return }
        setTimer("audioPath", 0.3) { [weak self] in self?.restartAudio() }
    }

    /// Keep what was heard, rebuild the engine on the new route and start a fresh task.
    private func restartAudio() {
        guard wanted, !suspended, !interrupted else { return }
        finalizeSegment(at: segmentChanged)
        if !assembler.pending.isEmpty {
            setTimer("gap", gap) { [weak self] in self?.flushLine(force: true) }
        }
        cancelTask()
        stopAudio()
        kick()
    }

    private func interruption(began: Bool) {
        if began {
            interrupted = true
            interruptedAt = now()
            clearTimer("audioPath")
            clearTimer("restart")
            finalizeSegment(at: segmentChanged)
            cancelTask()
            stopAudio()
            if wanted && !suspended { report("stopped") }
        } else {
            interrupted = false
            guard wanted, !suspended else { return }
            prepareSession?()
            kick()
        }
    }

    private func startAudio() throws {
        if let engine, engine.isRunning { return }
        stopAudio()
        prepareSession?()
        do {
            engine = try makeEngine(voiceProcessing: true)
        } catch {
            engine = try makeEngine(voiceProcessing: false)
        }
    }

    private func makeEngine(voiceProcessing: Bool) throws -> AVAudioEngine {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        if voiceProcessing {
            // Echo cancellation, so the phone's own speaker is not heard back.
            try input.setVoiceProcessingEnabled(true)
            input.voiceProcessingOtherAudioDuckingConfiguration =
                AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false,
                                                                     duckingLevel: .min)
            _ = engine.mainMixerNode  // voice processing wants the output side in the graph
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            if voiceProcessing { try? input.setVoiceProcessingEnabled(false) }
            throw ListenerFailure.noInput
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: format, block: Listener.tapBlock(feed))
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            if voiceProcessing { try? input.setVoiceProcessingEnabled(false) }
            throw error
        }
        // The engine stops itself when the hardware configuration changes.
        engineObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] note in
            let changed = note.object as? AVAudioEngine
            MainActor.assumeIsolated { self?.engineConfigChanged(changed) }
        }
        return engine
    }

    private func stopAudio() {
        feed.set(nil)
        if let engineObserver {
            NotificationCenter.default.removeObserver(engineObserver)
            self.engineObserver = nil
        }
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        if engine.inputNode.isVoiceProcessingEnabled {
            try? engine.inputNode.setVoiceProcessingEnabled(false)  // so replies are not ducked
        }
        retire(engine)
        self.engine = nil
    }

    /// Keep a stopped engine alive for `retireS` seconds instead of freeing it now (see
    /// `retiredEngines`); older ones are let go here.
    private func retire(_ engine: AVAudioEngine) {
        let t = now()
        retiredEngines.removeAll { $0.until <= t }
        retiredEngines.append((engine, t + Listener.retireS))
    }

    private func cancelTask() {
        taskToken += 1  // late callbacks from the old task are ignored
        feed.set(nil)
        request?.endAudio()
        task?.cancel()
        task = nil
        request = nil
    }

    // MARK: Timers (main run loop, common modes so they fire while scrolling)

    private func setTimer(_ name: String, _ seconds: TimeInterval, _ action: @escaping @MainActor () -> Void) {
        clearTimer(name)
        let timer = Timer(timeInterval: max(0, seconds), repeats: false) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.timers[name] = nil
                action()
            }
        }
        timers[name] = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func clearTimer(_ name: String) {
        timers[name]?.invalidate()
        timers[name] = nil
    }

    private func clearAllTimers() {
        timers.values.forEach { $0.invalidate() }
        timers.removeAll()
    }

    // MARK: Callbacks built outside the main actor (they run on other threads)

    nonisolated private static func tapBlock(_ feed: RequestFeed) -> AVAudioNodeTapBlock {
        return { buffer, _ in feed.append(buffer) }
    }

    nonisolated private static func resultHandler(for listener: Listener, token: Int)
        -> (SFSpeechRecognitionResult?, Error?) -> Void {
        return { [weak listener] result, error in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    listener?.onRecognition(token: token, result: result, error: error)
                }
            }
        }
    }

    nonisolated private static func requestSpeechAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
    }
}

/// Microphone and speech recognition access together, for the setup and Settings. iOS posts no
/// change for them, so the screens read it again when they appear or come back to the front.
enum MicPermission {
    case notAsked, granted, denied

    static func current() -> MicPermission {
        let speech = SFSpeechRecognizer.authorizationStatus()
        let mic = AVAudioApplication.shared.recordPermission
        if speech == .denied || speech == .restricted || mic == .denied { return .denied }
        if speech == .authorized && mic == .granted { return .granted }
        return .notAsked
    }
}

/// The few loop timings the app uses directly, from BrainSyncKit's ListenCore.Timing.
private enum ListenTimes {
    static let stallAfter = ListenCore.Timing.defaults.stallAfter
    static let watchdog = ListenCore.Timing.defaults.watchdog
    static let grace = ListenCore.Timing.defaults.grace
}

private enum ListenerFailure: Error {
    case noInput
}

/// The current recognition request, fed from the audio thread.
private final class RequestFeed: @unchecked Sendable {
    private let lock = NSLock()
    private var request: SFSpeechAudioBufferRecognitionRequest?

    func set(_ newRequest: SFSpeechAudioBufferRecognitionRequest?) {
        lock.lock()
        request = newRequest
        lock.unlock()
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        let current = request
        lock.unlock()
        current?.append(buffer)
    }
}
