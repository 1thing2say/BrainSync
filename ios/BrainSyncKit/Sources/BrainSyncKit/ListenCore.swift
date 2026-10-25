import Foundation

/// The pure parts of ui/listen_core.js (PLAN.md V4), ported so the app hears the partner the same
/// way the browser does: which lines are meaningful, how final segments join into one line,
/// the laptop's own reply heard back (echo), when a line is done, and how a dead recognizer
/// restarts. Times are seconds (the JS uses milliseconds).
public enum ListenCore {
    /// The loop's timings (listen_core.js `T`), in seconds.
    public struct Timing: Equatable {
        /// After a normal end: restart at once, or the start of the partner's next sentence is lost.
        public var healthyRestart: TimeInterval = 0.1
        /// A session that died at once (under `healthyAfter`): back off, doubling...
        public var quickFirst: TimeInterval = 0.5
        /// ...up to this.
        public var quickMax: TimeInterval = 4.0
        /// No network: 1, 2, 4, 8 s, then every 8 s, never giving up.
        public var networkFirst: TimeInterval = 1.0
        public var networkMax: TimeInterval = 8.0
        /// No microphone: try again every 3 s.
        public var audioCapture: TimeInterval = 3.0
        /// A session that ran this long ended normally, not "died at once".
        public var healthyAfter: TimeInterval = 1.0
        /// No event at all for this long: abort it and start a new one.
        public var stallAfter: TimeInterval = 60.0
        /// Checks that a session is running or about to.
        public var watchdog: TimeInterval = 2.0
        /// No start by then: the recognizer may want a tap first.
        public var needsTapAfter: TimeInterval = 3.0
        /// A window ending mid-sentence waits this long for the sentence to finish.
        public var grace: TimeInterval = 3.0

        public init() {}

        public static let defaults = Timing()
    }

    /// Results below this confidence are dropped; 0 means "unknown" and is kept.
    public static let minConfidence: Double = 0.3
    /// Unconfirmed words this long are kept when a session ends.
    public static let minCarryWords = 3

    static let filler: Set<String> = ["um", "uh", "hmm", "mm", "ah", "oh", "er", "erm"]

    /// Lower case, only `a-z 0-9 '` and single spaces.
    public static func norm(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        for scalar in text.lowercased().unicodeScalars {
            let v = scalar.value
            let keep = (97...122).contains(v) || (48...57).contains(v) || v == 39 // a-z 0-9 '
            out.append(keep ? scalar : " ")
        }
        return String(out).split(separator: " ", omittingEmptySubsequences: true).joined(separator: " ")
    }

    public static func words(_ text: String) -> [String] {
        norm(text).split(separator: " ").map(String.init)
    }

    /// More than filler ("um", "uh") and more than one letter.
    public static func meaningful(_ text: String) -> Bool {
        let w = words(text).filter { !filler.contains($0) }
        return w.count > 1 || (w.count == 1 && w[0].count > 1)
    }

    /// Joins final segments into one line. A segment without closing punctuation gets a "." so the
    /// server can tell the sentences apart.
    public static func joinFinals(_ finals: [String]) -> String {
        let parts = finals.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        let closers: Set<Character> = [".", "!", "?", ";", ","]
        return parts.enumerated().map { i, f in
            (i < parts.count - 1 && !(f.last.map { closers.contains($0) } ?? false)) ? f + "." : f
        }.joined(separator: " ")
    }

    /// The laptop's own reply heard back. Only checked within `echoWindow` of the mic reopening,
    /// and only on two or more words in a row, so the partner's own "yes" is kept.
    public static func isEcho(_ text: String, lastSpoken: String, sinceReopen: TimeInterval,
                              echoWindow: TimeInterval) -> Bool {
        if lastSpoken.isEmpty || sinceReopen > echoWindow { return false }
        let a = words(text)
        let b = words(lastSpoken)
        let heard = " " + a.joined(separator: " ") + " "
        let reply = " " + b.joined(separator: " ") + " "
        return (a.count >= 2 && reply.contains(heard)) || (b.count >= 2 && heard.contains(reply))
    }

    /// Collects final segments into one partner line: the line is done after `gap` of silence
    /// since the last final, `maxLine` since the first (a monologue), or when forced.
    public struct LineAssembler {
        public var gap: TimeInterval
        public var maxLine: TimeInterval
        private var finals: [String] = []
        private var firstAt: TimeInterval = 0
        private var lastAt: TimeInterval = 0

        public init(gap: TimeInterval, maxLine: TimeInterval) {
            self.gap = gap
            self.maxLine = maxLine
        }

        public mutating func addFinal(_ text: String, at time: TimeInterval) {
            let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !t.isEmpty else { return }
            if finals.isEmpty { firstAt = time }
            finals.append(t)
            lastAt = time
        }

        /// What was heard so far, joined.
        public var pending: String { ListenCore.joinFinals(finals) }

        public var isEmpty: Bool { finals.isEmpty }

        /// When the current line is due (nil when nothing is pending): for scheduling a timer.
        public var dueAt: TimeInterval? {
            finals.isEmpty ? nil : min(lastAt + gap, firstAt + maxLine)
        }

        /// The joined line once it is done (or `force`), clearing it; nil while not done, or when
        /// what was heard is only filler (then it is dropped).
        public mutating func takeLine(at time: TimeInterval, force: Bool = false) -> String? {
            guard !finals.isEmpty else { return nil }
            guard force || time - lastAt >= gap || time - firstAt >= maxLine else { return nil }
            let text = ListenCore.joinFinals(finals)
            finals = []
            return ListenCore.meaningful(text) ? text : nil
        }

        /// Drops what was heard (mute).
        public mutating func clear() { finals = [] }
    }

    public enum ListenError: Equatable {
        case network, noMic, denied, unsupported, other
    }

    /// When to start the next recognition session after one ended (createListener's
    /// `scheduleRestart`): network errors back off 1, 2, 4, 8, 8... s; no microphone retries every
    /// 3 s; a session that ran `healthyAfter` or heard speech restarts at once; one that died at
    /// once backs off 0.5, 1, 2, 4, 4... s; denied and unsupported never restart.
    public struct RestartPolicy {
        public var timing: Timing
        private var quick = 0
        private var netFails = 0

        public init() { timing = .defaults }

        public init(timing: Timing) { self.timing = timing }

        /// nil = do not restart (denied / unsupported).
        public mutating func delay(afterSessionRan ran: TimeInterval, error: ListenError?,
                                   heard: Bool = false) -> TimeInterval? {
            if heard { quick = 0; netFails = 0 } // speech arrived: the JS onresult resets both
            switch error {
            case .denied?, .unsupported?:
                return nil
            case .network?:
                let d = min(timing.networkFirst * pow(2, Double(netFails)), timing.networkMax)
                netFails += 1
                return d
            case .noMic?:
                return timing.audioCapture
            case .other?, nil:
                if heard || ran >= timing.healthyAfter {
                    quick = 0
                    return timing.healthyRestart
                }
                let d = min(timing.quickFirst * pow(2, Double(quick)), timing.quickMax)
                quick += 1
                return d
            }
        }

        /// The mic was (re)opened or kicked: back-offs start over.
        public mutating func reset() {
            quick = 0
            netFails = 0
        }
    }
}
