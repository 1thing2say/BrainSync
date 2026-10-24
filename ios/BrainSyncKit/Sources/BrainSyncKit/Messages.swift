import Foundation

// The /ws/ui messages (PLAN.md section 6), mirroring app/schemas.py. Swift names are camelCase,
// the wire keys snake_case. Unknown fields are ignored; fields with a server-side default may be
// missing.

// MARK: - Server -> app

public enum PageMode: String, Codable, Equatable {
    case page, confirm, list
}

public enum SlotRole: String, Codable, Equatable {
    case option, next, say, back
}

public struct UISlot: Codable, Equatable {
    public var slot: String
    public var role: SlotRole
    public var label: String
    /// The sentence beside an option's target; empty for NEXT / Say it / Back.
    public var utterance: String

    public init(slot: String, role: SlotRole, label: String, utterance: String = "") {
        self.slot = slot
        self.role = role
        self.label = label
        self.utterance = utterance
    }

    enum CodingKeys: String, CodingKey { case slot, role, label, utterance }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        slot = try c.decode(String.self, forKey: .slot)
        role = try c.decode(SlotRole.self, forKey: .role)
        label = try c.decode(String.self, forKey: .label)
        utterance = try c.decodeIfPresent(String.self, forKey: .utterance) ?? ""
    }
}

/// A menu option (schemas.Option): the list-mode entries of `show_page.options`.
public struct MenuOption: Codable, Equatable {
    public var label: String
    public var utterance: String
    public var intentId: String

    public init(label: String, utterance: String, intentId: String = "") {
        self.label = label
        self.utterance = utterance
        self.intentId = intentId
    }

    enum CodingKeys: String, CodingKey {
        case label, utterance
        case intentId = "intent_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decode(String.self, forKey: .label)
        utterance = try c.decode(String.self, forKey: .utterance)
        intentId = try c.decodeIfPresent(String.self, forKey: .intentId) ?? ""
    }
}

public struct ShowPage: Codable, Equatable {
    public var listId: String
    public var page: Int
    public var mode: PageMode
    public var slots: [UISlot]
    public var text: String
    public var options: [MenuOption]
    public var highlightIndex: Int?

    public init(listId: String, page: Int, mode: PageMode, slots: [UISlot], text: String = "",
                options: [MenuOption] = [], highlightIndex: Int? = nil) {
        self.listId = listId
        self.page = page
        self.mode = mode
        self.slots = slots
        self.text = text
        self.options = options
        self.highlightIndex = highlightIndex
    }

    enum CodingKeys: String, CodingKey {
        case listId = "list_id"
        case page, mode, slots, text, options
        case highlightIndex = "highlight_index"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        listId = try c.decode(String.self, forKey: .listId)
        page = try c.decode(Int.self, forKey: .page)
        mode = try c.decode(PageMode.self, forKey: .mode)
        slots = try c.decode([UISlot].self, forKey: .slots)
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        options = try c.decodeIfPresent([MenuOption].self, forKey: .options) ?? []
        highlightIndex = try c.decodeIfPresent(Int.self, forKey: .highlightIndex)
    }
}

public enum TrialPhase: String, Codable, Equatable {
    case cue, flicker, rest, baseline
}

public struct Trial: Codable, Equatable {
    public var trialId: Int
    public var phase: TrialPhase

    public init(trialId: Int, phase: TrialPhase) {
        self.trialId = trialId
        self.phase = phase
    }

    enum CodingKeys: String, CodingKey {
        case trialId = "trial_id"
        case phase
    }
}

public struct Highlight: Codable, Equatable {
    public var slot: String

    public init(slot: String) { self.slot = slot }
}

public struct Speak: Codable, Equatable {
    public var text: String
    public var utteranceId: String
    /// Server-relative (`/audio/abc`); nil = speak with the local voice.
    public var audioUrl: String?

    public init(text: String, utteranceId: String, audioUrl: String? = nil) {
        self.text = text
        self.utteranceId = utteranceId
        self.audioUrl = audioUrl
    }

    enum CodingKeys: String, CodingKey {
        case text
        case utteranceId = "utterance_id"
        case audioUrl = "audio_url"
    }
}

/// What the partner's mic does; the server owns it. `untilStopped`: listen until the next
/// `listen`; otherwise `seconds` > 0 is a window and `seconds` == 0 means off.
public struct Listen: Codable, Equatable {
    public var seconds: Double
    public var delayS: Double
    public var untilStopped: Bool
    public var muted: Bool
    /// start, turn, speaking, paused, muted, idle, reconnect (kept as a string: open set).
    public var reason: String

    public init(seconds: Double, delayS: Double = 0, untilStopped: Bool = false, muted: Bool = false,
                reason: String) {
        self.seconds = seconds
        self.delayS = delayS
        self.untilStopped = untilStopped
        self.muted = muted
        self.reason = reason
    }

    enum CodingKeys: String, CodingKey {
        case seconds
        case delayS = "delay_s"
        case untilStopped = "until_stopped"
        case muted, reason
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seconds = try c.decode(Double.self, forKey: .seconds)
        delayS = try c.decodeIfPresent(Double.self, forKey: .delayS) ?? 0
        untilStopped = try c.decodeIfPresent(Bool.self, forKey: .untilStopped) ?? false
        muted = try c.decodeIfPresent(Bool.self, forKey: .muted) ?? false
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
    }
}

public struct StateMessage: Codable, Equatable {
    public var name: String
    public var detail: String

    public init(name: String, detail: String = "") {
        self.name = name
        self.detail = detail
    }

    enum CodingKeys: String, CodingKey { case name, detail }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
    }
}

public enum TranscriptWho: String, Codable, Equatable {
    case partner, user
}

public struct Transcript: Codable, Equatable {
    public var who: TranscriptWho
    public var text: String

    public init(who: TranscriptWho, text: String) {
        self.who = who
        self.text = text
    }
}

/// Health for the partner page's badges (schemas.UIStatus). Every field defaults when missing.
public struct UIStatus: Codable, Equatable {
    public var decoderConnected: Bool
    public var decoderOk: Bool
    public var displayOk: Bool
    public var llmOk: Bool
    public var fakeDecoder: Bool
    public var keyboardMode: Bool
    public var boardOk: Bool
    public var decoderMessage: String
    public var decoderName: String
    /// off, waiting, ok, failed
    public var llm: String
    /// The partner's Mute (held by the server).
    public var micMuted: Bool
    /// The page's last mic_report state ("listening", "offline", ...), "" before the first.
    public var mic: String
    public var resumeReady: Bool

    public init(decoderConnected: Bool = false, decoderOk: Bool = false, displayOk: Bool = false,
                llmOk: Bool = false, fakeDecoder: Bool = false, keyboardMode: Bool = false,
                boardOk: Bool = false, decoderMessage: String = "", decoderName: String = "",
                llm: String = "off", micMuted: Bool = false, mic: String = "", resumeReady: Bool = false) {
        self.decoderConnected = decoderConnected
        self.decoderOk = decoderOk
        self.displayOk = displayOk
        self.llmOk = llmOk
        self.fakeDecoder = fakeDecoder
        self.keyboardMode = keyboardMode
        self.boardOk = boardOk
        self.decoderMessage = decoderMessage
        self.decoderName = decoderName
        self.llm = llm
        self.micMuted = micMuted
        self.mic = mic
        self.resumeReady = resumeReady
    }

    enum CodingKeys: String, CodingKey {
        case decoderConnected = "decoder_connected"
        case decoderOk = "decoder_ok"
        case displayOk = "display_ok"
        case llmOk = "llm_ok"
        case fakeDecoder = "fake_decoder"
        case keyboardMode = "keyboard_mode"
        case boardOk = "board_ok"
        case decoderMessage = "decoder_message"
        case decoderName = "decoder_name"
        case llm
        case micMuted = "mic_muted"
        case mic
        case resumeReady = "resume_ready"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func bool(_ k: CodingKeys) -> Bool { (try? c.decodeIfPresent(Bool.self, forKey: k)) ?? false }
        func str(_ k: CodingKeys, _ d: String = "") -> String {
            (try? c.decodeIfPresent(String.self, forKey: k)) ?? d
        }
        decoderConnected = bool(.decoderConnected)
        decoderOk = bool(.decoderOk)
        displayOk = bool(.displayOk)
        llmOk = bool(.llmOk)
        fakeDecoder = bool(.fakeDecoder)
        keyboardMode = bool(.keyboardMode)
        boardOk = bool(.boardOk)
        decoderMessage = str(.decoderMessage)
        decoderName = str(.decoderName)
        llm = str(.llm, "off")
        micMuted = bool(.micMuted)
        mic = str(.mic)
        resumeReady = bool(.resumeReady)
    }
}

public enum ServerMessage: Equatable {
    case showPage(ShowPage)
    case trial(Trial)
    case highlight(Highlight)
    case speak(Speak)
    case listen(Listen)
    case state(StateMessage)
    case transcript(Transcript)
    case status(UIStatus)
    /// A message type this app does not know (its `type`), from a newer server.
    case unknown(String)

    private struct TypeOnly: Decodable { let type: String }

    /// Throws when the data is not a JSON object with a `type`, or a known type is malformed.
    public static func decode(_ data: Data) throws -> ServerMessage {
        let d = JSONDecoder()
        let type = try d.decode(TypeOnly.self, from: data).type
        switch type {
        case "show_page": return .showPage(try d.decode(ShowPage.self, from: data))
        case "trial": return .trial(try d.decode(Trial.self, from: data))
        case "highlight": return .highlight(try d.decode(Highlight.self, from: data))
        case "speak": return .speak(try d.decode(Speak.self, from: data))
        case "listen": return .listen(try d.decode(Listen.self, from: data))
        case "state": return .state(try d.decode(StateMessage.self, from: data))
        case "transcript": return .transcript(try d.decode(Transcript.self, from: data))
        case "status": return .status(try d.decode(UIStatus.self, from: data))
        default: return .unknown(type)
        }
    }

    public static func decode(_ text: String) throws -> ServerMessage {
        try decode(Data(text.utf8))
    }
}

// MARK: - App -> server

public enum PartnerSource: String, Codable {
    case stt, typed
}

/// schemas.MicReport.state
public enum MicState: String, Codable, CaseIterable {
    case listening
    case needsTap = "needs_tap"
    case denied
    case nomic
    case offline
    case unsupported
    case error
    case stopped
    case hidden
    case muted
    case speaking
    case paused
}

public enum ClientMessage: Equatable {
    case ttsDone(utteranceId: String)
    case partnerText(text: String, source: PartnerSource)
    case key(String)
    case stop
    case resume
    case judgment(utteranceId: String, correct: Bool)
    case micSet(muted: Bool)
    case micReport(state: MicState, restarts: Int)

    enum Keys: String, CodingKey {
        case type
        case utteranceId = "utterance_id"
        case text, source, key, correct, muted, state, restarts
    }

    private struct Box: Codable {
        let message: ClientMessage

        init(_ message: ClientMessage) { self.message = message }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: Keys.self)
            switch message {
            case .ttsDone(let id):
                try c.encode("tts_done", forKey: .type)
                try c.encode(id, forKey: .utteranceId)
            case .partnerText(let text, let source):
                try c.encode("partner_text", forKey: .type)
                try c.encode(text, forKey: .text)
                try c.encode(source, forKey: .source)
            case .key(let key):
                try c.encode("key", forKey: .type)
                try c.encode(key, forKey: .key)
            case .stop:
                try c.encode("stop", forKey: .type)
            case .resume:
                try c.encode("resume", forKey: .type)
            case .judgment(let id, let correct):
                try c.encode("judgment", forKey: .type)
                try c.encode(id, forKey: .utteranceId)
                try c.encode(correct, forKey: .correct)
            case .micSet(let muted):
                try c.encode("mic_set", forKey: .type)
                try c.encode(muted, forKey: .muted)
            case .micReport(let state, let restarts):
                try c.encode("mic_report", forKey: .type)
                try c.encode(state, forKey: .state)
                try c.encode(restarts, forKey: .restarts)
            }
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            let type = try c.decode(String.self, forKey: .type)
            switch type {
            case "tts_done": message = .ttsDone(utteranceId: try c.decode(String.self, forKey: .utteranceId))
            case "partner_text":
                message = .partnerText(text: try c.decode(String.self, forKey: .text),
                                       source: try c.decode(PartnerSource.self, forKey: .source))
            case "key": message = .key(try c.decode(String.self, forKey: .key))
            case "stop": message = .stop
            case "resume": message = .resume
            case "judgment":
                message = .judgment(utteranceId: try c.decode(String.self, forKey: .utteranceId),
                                    correct: try c.decode(Bool.self, forKey: .correct))
            case "mic_set": message = .micSet(muted: try c.decode(Bool.self, forKey: .muted))
            case "mic_report":
                message = .micReport(state: try c.decode(MicState.self, forKey: .state),
                                     restarts: try c.decodeIfPresent(Int.self, forKey: .restarts) ?? 0)
            default:
                throw DecodingError.dataCorruptedError(forKey: .type, in: c,
                                                       debugDescription: "unknown ui message type \(type)")
            }
        }
    }

    /// The JSON object the server's `parse_ui_to_server` accepts.
    public func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(Box(self))
    }

    /// The JSON text of `encoded()`.
    public func encodedString() throws -> String {
        String(decoding: try encoded(), as: UTF8.self)
    }

    /// Parses an app -> server message (tests, logging). Throws on an unknown type.
    public static func decode(_ data: Data) throws -> ClientMessage {
        try JSONDecoder().decode(Box.self, from: data).message
    }
}
