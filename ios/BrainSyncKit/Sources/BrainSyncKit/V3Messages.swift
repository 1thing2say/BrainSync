import Foundation

// The PROTOCOL-v3 messages the phone reads and sends as a slots-mode controller of
// `brainsync.server` (PROTOCOL-v3.md sections 2, 3 and 6; brainsync/server/protocol.py).
// Swift names are camelCase, the wire keys snake_case (spelled out in CodingKeys, never a key
// strategy: a strategy would also rewrite the slot names used as keys of `scores`). Unknown
// fields are ignored, an unknown message type decodes as `.unknown`, and fields an older
// server leaves out get the protocol's default, so a newer or older server never breaks the
// app. Point-mode messages (scan_request, scan_result, click) are not modelled: a slots-mode
// controller never needs them, so they arrive as `.unknown`.

/// PROTOCOL-v3's `protocol` number.
public let V3ProtocolVersion = 3

// MARK: - Server -> client

/// The stimulus paradigm (v3.2): `code` (c-VEP, one m-sequence with a lag per target) or
/// `freq` (one frequency per target).
public enum V3Paradigm: String, Codable, Equatable {
    case code, freq
}

/// The welcome's `code` object (v3.2): one bit a frame, target k delayed by `k * lagFrames`.
public struct V3Code: Codable, Equatable {
    /// The 63-bit m-sequence, 0 (black) or 1 (lit, `fill`).
    public var bits: [Int]
    public var lagFrames: Int
    /// Frames per code cycle; equals `bits.count`.
    public var cycleFrames: Int
    /// The server's `--train-cycles`.
    public var trainCycles: Int
    /// The CSS colour of a lit square, "#ffffff".
    public var fill: String

    public init(bits: [Int], lagFrames: Int, cycleFrames: Int, trainCycles: Int, fill: String = "#ffffff") {
        self.bits = bits
        self.lagFrames = lagFrames
        self.cycleFrames = cycleFrames
        self.trainCycles = trainCycles
        self.fill = fill
    }

    enum CodingKeys: String, CodingKey {
        case bits, fill
        case lagFrames = "lag_frames"
        case cycleFrames = "cycle_frames"
        case trainCycles = "train_cycles"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bits = try c.decode([Int].self, forKey: .bits)
        lagFrames = try c.decode(Int.self, forKey: .lagFrames)
        cycleFrames = try c.decodeIfPresent(Int.self, forKey: .cycleFrames) ?? bits.count
        trainCycles = try c.decodeIfPresent(Int.self, forKey: .trainCycles) ?? 30
        fill = try c.decodeIfPresent(String.self, forKey: .fill) ?? "#ffffff"
    }
}

public struct V3ServerInfo: Codable, Equatable {
    public var name: String
    public var version: String

    public init(name: String = "brainsync", version: String = "") {
        self.name = name
        self.version = version
    }

    enum CodingKeys: String, CodingKey { case name, version }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        version = try c.decodeIfPresent(String.self, forKey: .version) ?? ""
    }
}

/// The answer to a valid hello.
public struct V3Welcome: Codable, Equatable {
    public var `protocol`: Int
    /// "controller" or "viewer".
    public var role: String
    /// "slots", "point", or nil for a viewer that named none.
    public var mode: String?
    /// `freq` when an older server leaves it out.
    public var paradigm: V3Paradigm
    public var server: V3ServerInfo
    /// The server runs `--board fake`: `gaze` steers its simulated EEG.
    public var fake: Bool
    public var waveform: String
    /// The nominal display rate (the first of `refreshRates`).
    public var refreshHz: Double
    public var refreshToleranceHz: Double
    /// Every rate a controller may have, each within `refreshToleranceHz`; `[refreshHz]` when an
    /// older server leaves it out. Code mode has one: the template holds at that rate only.
    public var refreshRates: [Double]
    /// Freq mode's target frequencies; empty in code mode.
    public var freqs: [Double]
    public var maxSlots: Int
    public var detect: Bool
    /// Code mode's m-sequence and timing; nil in freq mode.
    public var code: V3Code?

    public init(protocol: Int = V3ProtocolVersion, role: String = "controller", mode: String? = "slots",
                paradigm: V3Paradigm = .code, server: V3ServerInfo = V3ServerInfo(), fake: Bool = false,
                waveform: String = "sine", refreshHz: Double = 60, refreshToleranceHz: Double = 1,
                refreshRates: [Double]? = nil, freqs: [Double] = [], maxSlots: Int = 3,
                detect: Bool = false, code: V3Code? = nil) {
        self.protocol = `protocol`
        self.role = role
        self.mode = mode
        self.paradigm = paradigm
        self.server = server
        self.fake = fake
        self.waveform = waveform
        self.refreshHz = refreshHz
        self.refreshToleranceHz = refreshToleranceHz
        self.refreshRates = refreshRates ?? [refreshHz]
        self.freqs = freqs
        self.maxSlots = maxSlots
        self.detect = detect
        self.code = code
    }

    enum CodingKeys: String, CodingKey {
        case `protocol`, role, mode, paradigm, server, fake, waveform, freqs, detect, code
        case refreshHz = "refresh_hz"
        case refreshToleranceHz = "refresh_tolerance_hz"
        case refreshRates = "refresh_rates"
        case maxSlots = "max_slots"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        `protocol` = try c.decodeIfPresent(Int.self, forKey: .protocol) ?? V3ProtocolVersion
        role = try c.decodeIfPresent(String.self, forKey: .role) ?? "controller"
        mode = try c.decodeIfPresent(String.self, forKey: .mode)
        // An unknown future paradigm reads as freq, the protocol's default.
        let rawParadigm = try c.decodeIfPresent(String.self, forKey: .paradigm)
        paradigm = rawParadigm.flatMap(V3Paradigm.init(rawValue:)) ?? .freq
        server = try c.decodeIfPresent(V3ServerInfo.self, forKey: .server) ?? V3ServerInfo()
        fake = try c.decodeIfPresent(Bool.self, forKey: .fake) ?? false
        waveform = try c.decodeIfPresent(String.self, forKey: .waveform) ?? "sine"
        refreshHz = try c.decode(Double.self, forKey: .refreshHz)
        refreshToleranceHz = try c.decodeIfPresent(Double.self, forKey: .refreshToleranceHz) ?? 1.0
        refreshRates = try c.decodeIfPresent([Double].self, forKey: .refreshRates) ?? [refreshHz]
        freqs = try c.decodeIfPresent([Double].self, forKey: .freqs) ?? []
        maxSlots = try c.decode(Int.self, forKey: .maxSlots)
        detect = try c.decodeIfPresent(Bool.self, forKey: .detect) ?? false
        code = try c.decodeIfPresent(V3Code.self, forKey: .code)
    }

    /// True when `hz` is within the tolerance of one of the accepted rates (the server's
    /// `check_refresh`), so the phone can tell the user before the server answers `refresh`.
    public func accepts(refreshHz hz: Double) -> Bool {
        hz > 0 && refreshRates.contains { abs(hz - $0) <= refreshToleranceHz + 1e-9 }
    }
}

/// The server's error codes (PROTOCOL-v3 section 3). Kept as a string so a new code still reads.
public struct V3Error: Codable, Equatable {
    public var code: String
    public var message: String
    /// The server closes the connection right after a fatal error.
    public var fatal: Bool

    public init(code: String, message: String = "", fatal: Bool = false) {
        self.code = code
        self.message = message
        self.fatal = fatal
    }

    enum CodingKeys: String, CodingKey { case code, message, fatal }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        code = try c.decodeIfPresent(String.self, forKey: .code) ?? "internal"
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? ""
        fatal = try c.decodeIfPresent(Bool.self, forKey: .fatal) ?? false
    }
}

public enum V3PhaseName: String, Codable, Equatable, CaseIterable {
    case wait, calibrate, train, run, pause, stop
}

/// What the session is doing. The phone flickers only while `flicker` is true (and the warning
/// was accepted, the socket is open and a layout has targets: PROTOCOL-v3 rule 1).
public struct V3Phase: Codable, Equatable {
    /// The raw phase name; `name` is nil for a phase this build does not know.
    public var phase: String
    public var flicker: Bool
    /// The phase's length (calibrate), or nil.
    public var seconds: Double?
    public var text: String
    /// Train only: the slot the user looks at (lag 0).
    public var cue: String?
    /// Train only: the training cycles.
    public var cycles: Int?

    public var name: V3PhaseName? { V3PhaseName(rawValue: phase) }

    public init(phase: V3PhaseName, flicker: Bool, seconds: Double? = nil, text: String = "",
                cue: String? = nil, cycles: Int? = nil) {
        self.phase = phase.rawValue
        self.flicker = flicker
        self.seconds = seconds
        self.text = text
        self.cue = cue
        self.cycles = cycles
    }

    enum CodingKeys: String, CodingKey { case phase, flicker, seconds, text, cue, cycles }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        phase = try c.decode(String.self, forKey: .phase)
        // An unknown phase never flickers, whatever it says.
        let said = try c.decodeIfPresent(Bool.self, forKey: .flicker) ?? false
        flicker = said && V3PhaseName(rawValue: phase) != nil
        seconds = try c.decodeIfPresent(Double.self, forKey: .seconds)
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        cue = try c.decodeIfPresent(String.self, forKey: .cue)
        cycles = try c.decodeIfPresent(Int.self, forKey: .cycles)
    }
}

/// A rectangle normalized to the viewport: x right, y down, all in [0, 1] (point mode only).
public struct V3Rect: Codable, Equatable {
    public var x: Double
    public var y: Double
    public var w: Double
    public var h: Double

    public init(x: Double, y: Double, w: Double, h: Double) {
        self.x = x
        self.y = y
        self.w = w
        self.h = h
    }
}

/// One target of a layout. A code target has `lag` (frames) and `freq` nil; a freq target has
/// `freq` (Hz) and no `lag`.
public struct V3Target: Codable, Equatable {
    public var slot: String
    public var freq: Double?
    public var lag: Int?
    /// The sine's starting phase in radians (freq mode).
    public var phase: Double
    public var label: String
    /// slot, button, more, confirm or back.
    public var kind: String
    /// Empty in slots mode, where the phone places its own targets.
    public var rects: [V3Rect]

    public init(slot: String, freq: Double? = nil, lag: Int? = nil, phase: Double = 0, label: String = "",
                kind: String = "slot", rects: [V3Rect] = []) {
        self.slot = slot
        self.freq = freq
        self.lag = lag
        self.phase = phase
        self.label = label
        self.kind = kind
        self.rects = rects
    }

    enum CodingKeys: String, CodingKey { case slot, freq, lag, phase, label, kind, rects }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        slot = try c.decode(String.self, forKey: .slot)
        freq = try c.decodeIfPresent(Double.self, forKey: .freq)
        lag = try c.decodeIfPresent(Int.self, forKey: .lag)
        phase = try c.decodeIfPresent(Double.self, forKey: .phase) ?? 0
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? slot
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "slot"
        rects = try c.decodeIfPresent([V3Rect].self, forKey: .rects) ?? []
    }
}

/// What to draw; replaces the previous layout as a whole.
public struct V3Layout: Codable, Equatable {
    public var layoutId: Int
    /// slots, pick, confirm, idle or train.
    public var stage: String
    public var targets: [V3Target]

    public init(layoutId: Int, stage: String = "slots", targets: [V3Target]) {
        self.layoutId = layoutId
        self.stage = stage
        self.targets = targets
    }

    /// The target of `slot`, if this layout has it.
    public func target(_ slot: String) -> V3Target? {
        targets.first { $0.slot == slot }
    }

    enum CodingKeys: String, CodingKey {
        case stage, targets
        case layoutId = "layout_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        layoutId = try c.decode(Int.self, forKey: .layoutId)
        stage = try c.decodeIfPresent(String.self, forKey: .stage) ?? "slots"
        targets = try c.decodeIfPresent([V3Target].self, forKey: .targets) ?? []
    }
}

/// The decoder committed a target of the current layout.
public struct V3Selection: Codable, Equatable {
    public var selectionId: Int
    public var layoutId: Int
    public var slot: String
    public var tServer: Double
    /// Correlations in code mode, baseline-corrected scores in freq mode; never probabilities.
    public var score: Double
    public var scores: [String: Double]

    public init(selectionId: Int, layoutId: Int, slot: String, tServer: Double, score: Double,
                scores: [String: Double] = [:]) {
        self.selectionId = selectionId
        self.layoutId = layoutId
        self.slot = slot
        self.tServer = tServer
        self.score = score
        self.scores = scores
    }

    enum CodingKeys: String, CodingKey {
        case slot, score, scores
        case selectionId = "selection_id"
        case layoutId = "layout_id"
        case tServer = "t_server"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        selectionId = try c.decode(Int.self, forKey: .selectionId)
        layoutId = try c.decode(Int.self, forKey: .layoutId)
        slot = try c.decode(String.self, forKey: .slot)
        tServer = try c.decodeIfPresent(Double.self, forKey: .tServer) ?? 0
        score = try c.decodeIfPresent(Double.self, forKey: .score) ?? 0
        scores = try c.decodeIfPresent([String: Double].self, forKey: .scores) ?? [:]
    }
}

/// One c-VEP decision of the running layout (about one a cycle), for the confidence bars.
public struct V3Decision: Codable, Equatable {
    public var layoutId: Int
    /// Pearson correlation per slot (not probabilities).
    public var scores: [String: Double]
    /// This decision's best slot.
    public var raw: String?
    /// The slot the votes hold; nil while there is no choice yet.
    public var committed: String?
    /// The best score's lead over the runner-up.
    public var lead: Double?
    /// This decision moved the commitment.
    public var changed: Bool

    public init(layoutId: Int, scores: [String: Double], raw: String? = nil, committed: String? = nil,
                lead: Double? = nil, changed: Bool = false) {
        self.layoutId = layoutId
        self.scores = scores
        self.raw = raw
        self.committed = committed
        self.lead = lead
        self.changed = changed
    }

    enum CodingKeys: String, CodingKey {
        case scores, raw, committed, lead, changed
        case layoutId = "layout_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        layoutId = try c.decode(Int.self, forKey: .layoutId)
        scores = try c.decodeIfPresent([String: Double].self, forKey: .scores) ?? [:]
        raw = try c.decodeIfPresent(String.self, forKey: .raw)
        committed = try c.decodeIfPresent(String.self, forKey: .committed)
        lead = try c.decodeIfPresent(Double.self, forKey: .lead)
        changed = try c.decodeIfPresent(Bool.self, forKey: .changed) ?? false
    }
}

/// Training progress: `cycle` complete cycles recorded of `of`.
public struct V3TrainProgress: Codable, Equatable {
    public var cycle: Int
    public var of: Int

    public init(cycle: Int, of: Int) {
        self.cycle = cycle
        self.of = of
    }
}

/// The template is ready; the phase turns `run` right after.
public struct V3Trained: Codable, Equatable {
    /// Leave-one-cycle-out accuracy in [0, 1], or nil when it could not be measured.
    public var accuracy: Double?
    /// 1 / 3 with three targets.
    public var chance: Double
    public var cycles: Int

    public init(accuracy: Double?, chance: Double, cycles: Int) {
        self.accuracy = accuracy
        self.chance = chance
        self.cycles = cycles
    }

    enum CodingKeys: String, CodingKey { case accuracy, chance, cycles }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        accuracy = try c.decodeIfPresent(Double.self, forKey: .accuracy)
        chance = try c.decodeIfPresent(Double.self, forKey: .chance) ?? 1.0 / 3.0
        cycles = try c.decodeIfPresent(Int.self, forKey: .cycles) ?? 0
    }
}

/// The board's state, about once a second.
public struct V3Status: Codable, Equatable {
    /// unknown, connecting, streaming, no-data or error.
    public var board: String
    /// False when the board is not streaming or the display drops too many frames; `text` says why.
    public var ok: Bool
    public var text: String
    public var rttMs: Int?

    public init(board: String, ok: Bool, text: String = "", rttMs: Int? = nil) {
        self.board = board
        self.ok = ok
        self.text = text
        self.rttMs = rttMs
    }

    enum CodingKeys: String, CodingKey {
        case board, ok, text
        case rttMs = "rtt_ms"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        board = try c.decodeIfPresent(String.self, forKey: .board) ?? "unknown"
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        rttMs = try c.decodeIfPresent(Int.self, forKey: .rttMs)
    }
}

/// The answer to a client `ping`, echoing its id and time next to the server's time.
public struct V3Pong: Codable, Equatable {
    public var pingId: Int
    public var tClient: Double
    public var tServer: Double

    public init(pingId: Int, tClient: Double, tServer: Double) {
        self.pingId = pingId
        self.tClient = tClient
        self.tServer = tServer
    }

    enum CodingKeys: String, CodingKey {
        case pingId = "ping_id"
        case tClient = "t_client"
        case tServer = "t_server"
    }
}

public enum V3ServerMessage: Equatable {
    case welcome(V3Welcome)
    case error(V3Error)
    case phase(V3Phase)
    case layout(V3Layout)
    case selection(V3Selection)
    case decision(V3Decision)
    case train(V3TrainProgress)
    case trained(V3Trained)
    case status(V3Status)
    case pong(V3Pong)
    /// The server's keepalive (`{"type": "ping"}`, every 20 s); it needs no answer.
    case ping
    /// A message type this build does not handle (point mode's, or a newer server's).
    case unknown(String)

    private struct Envelope: Decodable {
        let type: String
    }

    /// Decodes one server frame. Throws only when it is not a JSON object with a string `type`,
    /// or when a known type lacks a required field.
    public static func decode(_ data: Data) throws -> V3ServerMessage {
        let decoder = JSONDecoder()
        let type = try decoder.decode(Envelope.self, from: data).type
        switch type {
        case "welcome": return .welcome(try decoder.decode(V3Welcome.self, from: data))
        case "error": return .error(try decoder.decode(V3Error.self, from: data))
        case "phase": return .phase(try decoder.decode(V3Phase.self, from: data))
        case "layout": return .layout(try decoder.decode(V3Layout.self, from: data))
        case "selection": return .selection(try decoder.decode(V3Selection.self, from: data))
        case "decision": return .decision(try decoder.decode(V3Decision.self, from: data))
        case "train": return .train(try decoder.decode(V3TrainProgress.self, from: data))
        case "trained": return .trained(try decoder.decode(V3Trained.self, from: data))
        case "status": return .status(try decoder.decode(V3Status.self, from: data))
        case "pong": return .pong(try decoder.decode(V3Pong.self, from: data))
        case "ping": return .ping
        default: return .unknown(type)
        }
    }

    public static func decode(_ text: String) throws -> V3ServerMessage {
        try decode(Data(text.utf8))
    }

    /// The wire `type`.
    public var type: String {
        switch self {
        case .welcome: return "welcome"
        case .error: return "error"
        case .phase: return "phase"
        case .layout: return "layout"
        case .selection: return "selection"
        case .decision: return "decision"
        case .train: return "train"
        case .trained: return "trained"
        case .status: return "status"
        case .pong: return "pong"
        case .ping: return "ping"
        case .unknown(let t): return t
        }
    }
}

// MARK: - Client -> server

public enum V3Role: String, Codable, Equatable {
    case controller, viewer
}

/// `hello.client`: who is connecting. `instance` is a random id the connection keeps for its
/// lifetime, so the server can tell this phone reconnecting from another controller.
public struct V3ClientInfo: Codable, Equatable {
    public var name: String?
    public var kind: String?
    public var version: String?
    public var instance: String?

    public init(name: String? = "brainsync-ios", kind: String? = "ios", version: String? = nil,
                instance: String? = nil) {
        self.name = name
        self.kind = kind
        self.version = version
        self.instance = instance
    }
}

/// The viewport in points (CSS pixels on the web), with the screen scale as `dpr`.
public struct V3Viewport: Codable, Equatable {
    public var w: Double
    public var h: Double
    public var dpr: Double

    public init(w: Double, h: Double, dpr: Double = 1) {
        self.w = w
        self.h = h
        self.dpr = dpr
    }
}

/// The first message of every connection (PROTOCOL-v3 section 2).
public struct V3Hello: Codable, Equatable {
    public var token: String?
    public var role: V3Role
    /// Replace the current controller (it gets close 4000) instead of getting `busy`.
    public var takeover: Bool
    public var client: V3ClientInfo
    /// "slots" for the phone; nil for a viewer.
    public var mode: String?
    /// Slots mode: 1 to `max_slots` unique names of 1-32 characters.
    public var slots: [String]?
    /// The measured display refresh; a controller must send it.
    public var refreshHz: Double?
    /// True only after the user accepted the photosensitivity warning; a controller must send true.
    public var accepted: Bool
    public var viewport: V3Viewport?

    public init(token: String? = nil, role: V3Role, takeover: Bool = false, client: V3ClientInfo = V3ClientInfo(),
                mode: String?, slots: [String]?, refreshHz: Double?, accepted: Bool, viewport: V3Viewport? = nil) {
        self.token = token
        self.role = role
        self.takeover = takeover
        self.client = client
        self.mode = mode
        self.slots = slots
        self.refreshHz = refreshHz
        self.accepted = accepted
        self.viewport = viewport
    }

    /// A slots-mode controller's hello. `accepted` is true: build it only after the user
    /// accepted the flicker warning.
    public static func controller(slots: [String], refreshHz: Double, viewport: V3Viewport? = nil,
                                  client: V3ClientInfo = V3ClientInfo()) -> V3Hello {
        V3Hello(role: .controller, client: client, mode: "slots", slots: slots, refreshHz: refreshHz,
                accepted: true, viewport: viewport)
    }

    /// A viewer's hello: it gets every broadcast and may send only ping and bye.
    public static func viewer(client: V3ClientInfo = V3ClientInfo()) -> V3Hello {
        V3Hello(role: .viewer, client: client, mode: nil, slots: nil, refreshHz: nil, accepted: false)
    }

    enum CodingKeys: String, CodingKey {
        case type, `protocol`, token, role, takeover, client, mode, slots, accepted, viewport
        case refreshHz = "refresh_hz"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        token = try c.decodeIfPresent(String.self, forKey: .token)
        role = try c.decode(V3Role.self, forKey: .role)
        takeover = try c.decodeIfPresent(Bool.self, forKey: .takeover) ?? false
        client = try c.decodeIfPresent(V3ClientInfo.self, forKey: .client) ?? V3ClientInfo(name: nil, kind: nil)
        mode = try c.decodeIfPresent(String.self, forKey: .mode)
        slots = try c.decodeIfPresent([String].self, forKey: .slots)
        refreshHz = try c.decodeIfPresent(Double.self, forKey: .refreshHz)
        accepted = try c.decodeIfPresent(Bool.self, forKey: .accepted) ?? false
        viewport = try c.decodeIfPresent(V3Viewport.self, forKey: .viewport)
    }

    /// Every field is written, nil as null, as the reference clients do.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode("hello", forKey: .type)
        try c.encode(V3ProtocolVersion, forKey: .protocol)
        try c.encode(token, forKey: .token)
        try c.encode(role, forKey: .role)
        try c.encode(takeover, forKey: .takeover)
        try c.encode(client, forKey: .client)
        try c.encode(mode, forKey: .mode)
        try c.encode(slots, forKey: .slots)
        try c.encode(refreshHz, forKey: .refreshHz)
        try c.encode(accepted, forKey: .accepted)
        try c.encode(viewport, forKey: .viewport)
    }
}

public enum V3ClientMessage: Equatable {
    case hello(V3Hello)
    /// The first frame drawn with flicker on for a layout (Unix seconds).
    case onset(layoutId: Int, tClient: Double)
    /// Code mode: the flip of cycle `n`'s first frame (frame 63 n of the layout).
    case cycle(layoutId: Int, n: Int, tClient: Double)
    /// Every 5 s while flickering: the measured refresh and the running count of late frames.
    case frames(refreshHz: Double, dropped: Int)
    case ping(pingId: Int, tClient: Double)
    case pause
    case resume
    /// Fake board only (`welcome.fake`): look at `slot`, or at nothing (nil).
    case gaze(slot: String?)
    case bye

    enum CodingKeys: String, CodingKey {
        case type, n, slot, dropped
        case layoutId = "layout_id"
        case tClient = "t_client"
        case refreshHz = "refresh_hz"
        case pingId = "ping_id"
    }

    /// The wire `type`.
    public var type: String {
        switch self {
        case .hello: return "hello"
        case .onset: return "onset"
        case .cycle: return "cycle"
        case .frames: return "frames"
        case .ping: return "ping"
        case .pause: return "pause"
        case .resume: return "resume"
        case .gaze: return "gaze"
        case .bye: return "bye"
        }
    }

    /// One JSON object, snake_case keys.
    public func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(Wire(message: self))
    }

    /// The text frame to send.
    public func encodedString() throws -> String {
        String(decoding: try encoded(), as: UTF8.self)
    }

    /// Reads a client message back (tests and logs); throws for an unknown type.
    public static func decode(_ data: Data) throws -> V3ClientMessage {
        try JSONDecoder().decode(Wire.self, from: data).message
    }

    public static func decode(_ text: String) throws -> V3ClientMessage {
        try decode(Data(text.utf8))
    }

    /// The Codable form; the enum itself stays free of a synthesized coding of its own.
    struct Wire: Codable {
        let message: V3ClientMessage

        init(message: V3ClientMessage) {
            self.message = message
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            let type = try c.decode(String.self, forKey: .type)
            switch type {
            case "hello": message = .hello(try V3Hello(from: decoder))
            case "onset":
                message = .onset(layoutId: try c.decode(Int.self, forKey: .layoutId),
                                 tClient: try c.decode(Double.self, forKey: .tClient))
            case "cycle":
                message = .cycle(layoutId: try c.decode(Int.self, forKey: .layoutId),
                                 n: try c.decode(Int.self, forKey: .n),
                                 tClient: try c.decode(Double.self, forKey: .tClient))
            case "frames":
                message = .frames(refreshHz: try c.decode(Double.self, forKey: .refreshHz),
                                  dropped: try c.decode(Int.self, forKey: .dropped))
            case "ping":
                message = .ping(pingId: try c.decode(Int.self, forKey: .pingId),
                                tClient: try c.decode(Double.self, forKey: .tClient))
            case "pause": message = .pause
            case "resume": message = .resume
            case "bye": message = .bye
            case "gaze": message = .gaze(slot: try c.decodeIfPresent(String.self, forKey: .slot))
            default:
                throw DecodingError.dataCorruptedError(forKey: .type, in: c,
                                                       debugDescription: "unknown client message type \(type)")
            }
        }

        func encode(to encoder: Encoder) throws {
            if case .hello(let hello) = message {
                try hello.encode(to: encoder)
                return
            }
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(message.type, forKey: .type)
            switch message {
            case .hello: break
            case .onset(let layoutId, let tClient):
                try c.encode(layoutId, forKey: .layoutId)
                try c.encode(tClient, forKey: .tClient)
            case .cycle(let layoutId, let n, let tClient):
                try c.encode(layoutId, forKey: .layoutId)
                try c.encode(n, forKey: .n)
                try c.encode(tClient, forKey: .tClient)
            case .frames(let refreshHz, let dropped):
                try c.encode(refreshHz, forKey: .refreshHz)
                try c.encode(dropped, forKey: .dropped)
            case .ping(let pingId, let tClient):
                try c.encode(pingId, forKey: .pingId)
                try c.encode(tClient, forKey: .tClient)
            case .gaze(let slot):
                try c.encode(slot, forKey: .slot) // null is "looking at nothing"; the key is required
            case .pause, .resume, .bye:
                break
            }
        }
    }
}
