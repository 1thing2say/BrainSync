// Speech out (ui/speech.js): the server's pre-generated Cartesia clip when there is one, otherwise
// AVSpeechSynthesizer with a local English voice (works offline). Rule 2: only the exact text the
// server sends is spoken. `done` is called once per utterance when it finishes; a cancelled reply
// (STOP, pause, a newer reply) reports nothing, as in the browser.

import AVFoundation
import Foundation
import Observation

@MainActor
@Observable
final class Speaker {
    /// A reply is playing (clip or synthesis): the mic stays off.
    private(set) var isSpeaking = false
    /// The sentence being spoken, shown while it plays.
    private(set) var speakingText = ""
    /// The server's speech.tts_rate (1.0 = normal), for the synthesized fallback only.
    var rate: Double = 1.0

    /// A clip that has not started playing by then falls back to synthesis.
    private static let clipStartTimeout: TimeInterval = 5.0
    /// A clip that stalls (a slow tunnel) and makes no progress for this long is given up on.
    private static let clipStallTimeout: TimeInterval = 4.0

    @ObservationIgnored private var player: AVPlayer?
    @ObservationIgnored private var observers: [NSObjectProtocol] = []
    @ObservationIgnored private var statusObservation: NSKeyValueObservation?
    @ObservationIgnored private var startTimer: Timer?
    @ObservationIgnored private var stallTimer: Timer?
    @ObservationIgnored private var interruptionObserver: NSObjectProtocol?
    private let synthesizer = AVSpeechSynthesizer()
    private let synthDelegate = SynthDelegate()
    /// The utterance playing now (a counter); 0 = none. Stale callbacks compare against it.
    @ObservationIgnored private var current = 0
    @ObservationIgnored private var counter = 0
    @ObservationIgnored private var currentId = ""
    @ObservationIgnored private var currentText = ""
    @ObservationIgnored private var fellBack = false
    @ObservationIgnored private var done: ((String) -> Void)?
    @ObservationIgnored private var sessionConfigured = false

    init() {
        synthesizer.delegate = synthDelegate
        synthDelegate.onFinish = { [weak self] utterance in
            Task { @MainActor in self?.synthFinished(utterance) }
        }
        // A call, Siri or an alarm cuts the reply off and nothing reports its end: report it now,
        // so the server moves on (the mic reopens after the interruption).
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard raw == AVAudioSession.InterruptionType.began.rawValue else { return }
            MainActor.assumeIsolated { self?.interrupted() }
        }
    }

    private func interrupted() {
        guard isSpeaking, current != 0 else { return }
        finish(token: current)
    }

    /// One audio session for the app: playback through the loudspeaker and the partner's mic.
    func activateSession() {
        let session = AVAudioSession.sharedInstance()
        do {
            if !sessionConfigured {
                try session.setCategory(.playAndRecord, mode: .default,
                                        options: [.defaultToSpeaker, .allowBluetoothA2DP])
                sessionConfigured = true
            }
            try session.setActive(true)
        } catch {
            // Playback still works on most devices; synthesis is the fallback anyway.
        }
    }

    func speak(text: String, utteranceId: String, audioURL: URL?, done: @escaping (String) -> Void) {
        stopAll()
        activateSession()
        counter += 1
        current = counter
        currentId = utteranceId
        currentText = text
        fellBack = false
        self.done = done
        isSpeaking = true
        speakingText = text
        if let audioURL {
            playClip(audioURL, token: current)
        } else {
            synthesize(text, token: current)
        }
    }

    /// STOP, pause or idle: silence now, report nothing.
    func cancel() {
        stopAll()
        current = 0
        done = nil
        isSpeaking = false
        speakingText = ""
    }

    // MARK: Clip

    private func playClip(_ url: URL, token: Int) {
        let item = AVPlayerItem(url: url)
        let player = AVPlayer(playerItem: item)
        self.player = player
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification,
                                            object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.finish(token: token) }
        })
        observers.append(center.addObserver(forName: AVPlayerItem.failedToPlayToEndTimeNotification,
                                            object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.fallback(token: token) }
        })
        observers.append(center.addObserver(forName: AVPlayerItem.playbackStalledNotification,
                                            object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.stalled(token: token) }
        })
        statusObservation = Speaker.observeFailure(of: item, speaker: self, token: token)
        // A clip that never starts (no network, a slow tunnel) is spoken by the synthesizer.
        startTimer = Timer.scheduledTimer(withTimeInterval: Self.clipStartTimeout, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.current == token, let player = self.player else { return }
                if player.currentTime().seconds <= 0.05 { self.fallback(token: token) }
            }
        }
        player.play()
    }

    /// The clip ran dry. If it makes no progress for clipStallTimeout: synthesize the sentence if
    /// barely any of it played, otherwise count the reply as done.
    private func stalled(token: Int) {
        guard token == current, let player, stallTimer == nil else { return }
        let at = player.currentTime().seconds
        stallTimer = Timer.scheduledTimer(withTimeInterval: Self.clipStallTimeout, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.current == token, let player = self.player else { return }
                self.stallTimer = nil
                let now = player.currentTime().seconds
                guard !(now > at + 0.1) else { return }  // it recovered
                if at < 0.5 { self.fallback(token: token) } else { self.finish(token: token) }
            }
        }
    }

    private func fallback(token: Int) {
        guard token == current, !fellBack else { return }
        fellBack = true
        stopClip()
        synthesize(currentText, token: token)
    }

    // MARK: Synthesis

    private func synthesize(_ text: String, token: Int) {
        let utterance = AVSpeechUtterance(string: text)
        let base = Double(AVSpeechUtteranceDefaultSpeechRate)
        let scaled = base * rate
        utterance.rate = Float(min(Double(AVSpeechUtteranceMaximumSpeechRate),
                                   max(Double(AVSpeechUtteranceMinimumSpeechRate), scaled)))
        utterance.voice = Self.bestVoice()
        synthDelegate.track(utterance, token: token)
        synthesizer.speak(utterance)
    }

    private func synthFinished(_ token: Int) {
        finish(token: token)
    }

    /// The best installed en-US voice: Premium, then Enhanced, then the default. All are local.
    private static func bestVoice() -> AVSpeechSynthesisVoice? {
        let english = AVSpeechSynthesisVoice.speechVoices().filter { $0.language == "en-US" }
        if let premium = english.first(where: { $0.quality == .premium }) { return premium }
        if let enhanced = english.first(where: { $0.quality == .enhanced }) { return enhanced }
        return AVSpeechSynthesisVoice(language: "en-US") ?? english.first
    }

    // MARK: Ending

    private func finish(token: Int) {
        guard token == current, token != 0 else { return }
        let id = currentId
        let callback = done
        stopAll()
        current = 0
        done = nil
        isSpeaking = false
        speakingText = ""
        callback?(id)
    }

    private func stopClip() {
        startTimer?.invalidate()
        startTimer = nil
        stallTimer?.invalidate()
        stallTimer = nil
        statusObservation?.invalidate()
        statusObservation = nil
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        observers.removeAll()
        player?.pause()
        player = nil
    }

    /// KVO may call back on any thread: built outside the main actor, hops back to it.
    nonisolated private static func observeFailure(of item: AVPlayerItem, speaker: Speaker,
                                                   token: Int) -> NSKeyValueObservation {
        item.observe(\.status, options: [.new]) { [weak speaker] item, _ in
            guard item.status == .failed else { return }
            DispatchQueue.main.async {
                MainActor.assumeIsolated { speaker?.fallback(token: token) }
            }
        }
    }

    private func stopAll() {
        stopClip()
        synthDelegate.forgetAll()  // our own stop reports nothing
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
    }
}

/// AVSpeechSynthesizer's delegate. Each utterance carries its own token, so a late didFinish of
/// an old utterance can never end the next one. Our own stop forgets every token first, so it
/// reports nothing; a cancel by the system (still tracked) reports the end like a finish.
private final class SynthDelegate: NSObject, AVSpeechSynthesizerDelegate, @unchecked Sendable {
    var onFinish: ((Int) -> Void)?
    private let lock = NSLock()
    private var tokens: [ObjectIdentifier: Int] = [:]

    func track(_ utterance: AVSpeechUtterance, token: Int) {
        lock.lock()
        tokens[ObjectIdentifier(utterance)] = token
        lock.unlock()
    }

    func forgetAll() {
        lock.lock()
        tokens.removeAll()
        lock.unlock()
    }

    private func take(_ utterance: AVSpeechUtterance) -> Int? {
        lock.lock()
        defer { lock.unlock() }
        return tokens.removeValue(forKey: ObjectIdentifier(utterance))
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        if let token = take(utterance) { onFinish?(token) }
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        if let token = take(utterance) { onFinish?(token) }
    }
}
