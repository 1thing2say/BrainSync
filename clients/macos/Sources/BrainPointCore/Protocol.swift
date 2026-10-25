import CoreGraphics
import Foundation

// PROTOCOL-v3.md (point mode) plus the v3.1 "ax" scan, as JSONSerialization dictionaries out
// and typed structs in. Builders return `[String: Any]`; `Codec.encode` turns one into a JSON
// text frame (nil for NaN or infinity, which the server refuses). `Codec.parse` reads one
// server frame. Unknown fields are ignored, as the protocol says.

public enum Proto {
    public static let version = 3
    public static let clientName = "brainpoint-desktop"
    public static let clientKind = "desktop"
    /// Close codes after which the client must not reconnect by itself.
    public static let closeReplaced = 4000, closeBadToken = 4001, closeBusy = 4002
    public static let noReconnectCodes: Set<Int> = [4000, 4001, 4002]
    /// Reconnect delays in seconds: 1, 2, 4, then 5 forever.
    public static let backoffS: [Double] = [1, 2, 4, 5]
    public static let pingEveryS = 10.0
    public static let maxSettleMs = 10_000
}

public struct Viewport: Equatable, Sendable {
    public var w: Double, h: Double, dpr: Double
    public init(w: Double, h: Double, dpr: Double) { self.w = w; self.h = h; self.dpr = dpr }
    var dict: [String: Any] { ["w": w, "h": h, "dpr": dpr] }
}

public struct FrontApp: Equatable, Sendable {
    public var name: String, bundleID: String, pid: Int32
    public init(name: String, bundleID: String, pid: Int32) { self.name = name; self.bundleID = bundleID; self.pid = pid }
}

// MARK: - server -> client

public struct Welcome: Equatable, Sendable {
    public var role: String
    public var mode: String?
    public var fake: Bool
    public var waveform: String
    public var refreshHz: Double
    public var refreshToleranceHz: Double
    public var refreshRates: [Double]
    public var freqs: [Double]
    public var maxSlots: Int
    public var detect: Bool
    public var serverName: String
    public var serverVersion: String
}

public struct PhaseMsg: Equatable, Sendable {
    public var phase: String
    public var flicker: Bool
    public var seconds: Double?
    public var text: String
    public init(phase: String, flicker: Bool, seconds: Double? = nil, text: String = "") {
        self.phase = phase; self.flicker = flicker; self.seconds = seconds; self.text = text
    }
}

public struct LayoutTarget: Equatable, Sendable {
    public var slot: String, freq: Double, phase: Double, label: String, kind: String
    public var rects: [NormRect]
    public init(slot: String, freq: Double, phase: Double, label: String, kind: String, rects: [NormRect]) {
        self.slot = slot; self.freq = freq; self.phase = phase; self.label = label; self.kind = kind; self.rects = rects
    }
}

public struct LayoutMsg: Equatable, Sendable {
    public var layoutID: Int, stage: String, targets: [LayoutTarget]
    public init(layoutID: Int, stage: String, targets: [LayoutTarget]) {
        self.layoutID = layoutID; self.stage = stage; self.targets = targets
    }
}

public struct SelectionMsg: Equatable, Sendable {
    public var selectionID: Int, layoutID: Int, slot: String, tServer: Double
    public var score: Double?
    public var scores: [String: Double]
}

public struct ScanRequestMsg: Equatable, Sendable {
    public var scanID: Int, settleMs: Int
}

public struct ScanResultMsg: Equatable, Sendable {
    public var scanID: Int, ok: Bool, ms: Double, nElements: Int, nCandidates: Int
    public var summary: String, cached: Bool, error: String?
}

public struct ClickElement: Equatable, Sendable {
    public var id: Int?
    public var label: String
    public var role: String
    public var hintID: Int?
    public var risky: Bool
    public init(id: Int?, label: String, role: String, hintID: Int?, risky: Bool) {
        self.id = id; self.label = label; self.role = role; self.hintID = hintID; self.risky = risky
    }
}

public struct ClickMsg: Equatable, Sendable {
    public var clickID: Int, x: Double, y: Double, element: ClickElement
    public init(clickID: Int, x: Double, y: Double, element: ClickElement) {
        self.clickID = clickID; self.x = x; self.y = y; self.element = element
    }
}

public struct StatusMsg: Equatable, Sendable {
    public var board: String, ok: Bool, text: String, rttMs: Int?
    public init(board: String, ok: Bool, text: String = "", rttMs: Int? = nil) {
        self.board = board; self.ok = ok; self.text = text; self.rttMs = rttMs
    }
}

public struct ErrorMsg: Equatable, Sendable {
    public var code: String, message: String, fatal: Bool
    public init(code: String, message: String, fatal: Bool) { self.code = code; self.message = message; self.fatal = fatal }
}

public struct PongMsg: Equatable, Sendable {
    public var pingID: Int, tClient: Double, tServer: Double
}

public enum ServerMessage: Equatable, Sendable {
    case welcome(Welcome)
    case phase(PhaseMsg)
    case layout(LayoutMsg)
    case selection(SelectionMsg)
    case scanRequest(ScanRequestMsg)
    case scanResult(ScanResultMsg)
    case click(ClickMsg)
    case status(StatusMsg)
    case error(ErrorMsg)
    case pong(PongMsg)
    case ping                    // the server's 20 s keepalive; nothing to answer
    case unknown(String)         // a type this client does not handle

    public var type: String {
        switch self {
        case .welcome: "welcome"
        case .phase: "phase"
        case .layout: "layout"
        case .selection: "selection"
        case .scanRequest: "scan_request"
        case .scanResult: "scan_result"
        case .click: "click"
        case .status: "status"
        case .error: "error"
        case .pong: "pong"
        case .ping: "ping"
        case .unknown(let t): t
        }
    }
}

public struct CodecError: Error, Equatable, Sendable, CustomStringConvertible {
    public var message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
}

// MARK: - codec

public enum Codec {
    /// One compact JSON text frame, or nil when the object holds NaN, infinity or a non-JSON
    /// value (JSONSerialization would raise an Objective-C exception on those).
    public static func encode(_ msg: [String: Any]) -> String? {
        guard JSONSerialization.isValidJSONObject(msg),
              let data = try? JSONSerialization.data(withJSONObject: msg, options: [.sortedKeys, .withoutEscapingSlashes])
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// The JSON object of a frame, for logs and tests.
    public static func object(_ text: String) -> [String: Any]? {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return obj
    }

    // MARK: client -> server builders

    public static func hello(token: String?, instance: String, version: String, refreshHz: Double,
                             viewport: Viewport, takeover: Bool = false) -> [String: Any] {
        ["type": "hello", "protocol": Proto.version, "token": token.map { $0 as Any } ?? NSNull(),
         "role": "controller", "takeover": takeover,
         "client": ["name": Proto.clientName, "kind": Proto.clientKind, "version": version,
                    "instance": String(instance.prefix(64))],
         "mode": "point", "slots": NSNull(), "refresh_hz": refreshHz, "accepted": true,
         "viewport": viewport.dict]
    }

    /// A v3.1 `scan` with source "ax": no image, the app and window, the window's size as the
    /// viewport, and the hints (`Hints.make`).
    public static func scanAX(scanID: Int, app: FrontApp, windowTitle: String, viewport: Viewport,
                              hints: [[String: Any]]) -> [String: Any] {
        ["type": "scan", "scan_id": scanID, "source": "ax",
         "app": ["bundle_id": app.bundleID, "name": app.name, "pid": Int(app.pid)],
         "window": ["title": Hints.clip(windowTitle, 200)],
         "title": Hints.clip(windowTitle, 200),
         "viewport": viewport.dict, "hints": Array(hints.prefix(Hints.maxHints))]
    }

    public static func onset(layoutID: Int, tClient: Double) -> [String: Any] {
        ["type": "onset", "layout_id": layoutID, "t_client": tClient]
    }

    public static func frames(refreshHz: Double, dropped: Int) -> [String: Any] {
        ["type": "frames", "refresh_hz": (refreshHz * 1000).rounded() / 1000, "dropped": max(0, dropped)]
    }

    public static func ping(pingID: Int, tClient: Double) -> [String: Any] {
        ["type": "ping", "ping_id": pingID, "t_client": tClient]
    }

    public static func pause() -> [String: Any] { ["type": "pause"] }
    public static func resume() -> [String: Any] { ["type": "resume"] }
    /// v3.1: the focused app or window changed; the server drops its pages and asks for a new scan.
    public static func rescan() -> [String: Any] { ["type": "rescan"] }
    public static func bye() -> [String: Any] { ["type": "bye"] }

    public static func gaze(slot: String?) -> [String: Any] {
        ["type": "gaze", "slot": slot.map { $0 as Any } ?? NSNull()]
    }

    public static func clicked(clickID: Int, ok: Bool, method: String, detail: String) -> [String: Any] {
        ["type": "clicked", "click_id": clickID, "ok": ok, "method": Hints.clip(method, 64),
         "detail": Hints.clip(detail, 500)]
    }

    // MARK: server -> client parser

    public static func parse(_ text: String) -> Result<ServerMessage, CodecError> {
        guard let data = text.data(using: .utf8),
              let any = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) else {
            return .failure(CodecError("not JSON: \(text.prefix(80))"))
        }
        guard let m = any as? [String: Any] else { return .failure(CodecError("not a JSON object")) }
        guard let type = m["type"] as? String else { return .failure(CodecError("no string \"type\"")) }
        do { return .success(try parse(type: type, m)) } catch let e as CodecError { return .failure(e) } catch {
            return .failure(CodecError("\(error)"))
        }
    }

    static func parse(type: String, _ m: [String: Any]) throws -> ServerMessage {
        let r = Reader(m, type)
        switch type {
        case "welcome":
            let server = m["server"] as? [String: Any] ?? [:]
            let hz = try r.num("refresh_hz")
            let rates = (m["refresh_rates"] as? [Any])?.compactMap(Reader.double).filter { $0 > 0 } ?? []
            return .welcome(Welcome(
                role: try r.str("role"), mode: m["mode"] as? String, fake: r.bool("fake") ?? false,
                waveform: (m["waveform"] as? String) ?? "sine", refreshHz: hz,
                refreshToleranceHz: r.optNum("refresh_tolerance_hz") ?? 1.0,
                refreshRates: rates.isEmpty ? [hz] : rates,
                freqs: (m["freqs"] as? [Any])?.compactMap(Reader.double) ?? [],
                maxSlots: r.optInt("max_slots") ?? 0, detect: r.bool("detect") ?? true,
                serverName: server["name"] as? String ?? "", serverVersion: server["version"] as? String ?? ""))
        case "phase":
            return .phase(PhaseMsg(phase: try r.str("phase"), flicker: r.bool("flicker") ?? false,
                                   seconds: r.optNum("seconds"), text: m["text"] as? String ?? ""))
        case "layout":
            guard let raw = m["targets"] as? [Any] else { throw CodecError("layout: no targets list") }
            var targets: [LayoutTarget] = []
            for (k, t) in raw.enumerated() {
                guard let t = t as? [String: Any] else { throw CodecError("layout.targets[\(k)] is not an object") }
                let tr = Reader(t, "layout.targets[\(k)]")
                var rects: [NormRect] = []
                for (j, rr) in ((t["rects"] as? [Any]) ?? []).enumerated() {
                    guard let rd = rr as? [String: Any] else { throw CodecError("layout.targets[\(k)].rects[\(j)] is not an object") }
                    let rrd = Reader(rd, "layout.targets[\(k)].rects[\(j)]")
                    rects.append(NormRect(x: try rrd.num("x"), y: try rrd.num("y"), w: try rrd.num("w"), h: try rrd.num("h")))
                }
                targets.append(LayoutTarget(slot: try tr.str("slot"), freq: try tr.num("freq"),
                                            phase: tr.optNum("phase") ?? 0, label: t["label"] as? String ?? "",
                                            kind: t["kind"] as? String ?? "button", rects: rects))
            }
            return .layout(LayoutMsg(layoutID: try r.int("layout_id"), stage: (m["stage"] as? String) ?? "pick", targets: targets))
        case "selection":
            var scores: [String: Double] = [:]
            for (k, v) in (m["scores"] as? [String: Any]) ?? [:] { if let d = Reader.double(v) { scores[k] = d } }
            return .selection(SelectionMsg(selectionID: try r.int("selection_id"), layoutID: try r.int("layout_id"),
                                           slot: try r.str("slot"), tServer: r.optNum("t_server") ?? 0,
                                           score: r.optNum("score"), scores: scores))
        case "scan_request":
            let settle = r.optInt("settle_ms") ?? 800
            return .scanRequest(ScanRequestMsg(scanID: try r.int("scan_id"),
                                               settleMs: min(Proto.maxSettleMs, max(0, settle))))
        case "scan_result":
            return .scanResult(ScanResultMsg(scanID: try r.int("scan_id"), ok: r.bool("ok") ?? false,
                                             ms: r.optNum("ms") ?? 0, nElements: r.optInt("n_elements") ?? 0,
                                             nCandidates: r.optInt("n_candidates") ?? 0,
                                             summary: m["summary"] as? String ?? "", cached: r.bool("cached") ?? false,
                                             error: m["error"] as? String))
        case "click":
            let e = m["element"] as? [String: Any] ?? [:]
            let er = Reader(e, "click.element")
            return .click(ClickMsg(clickID: try r.int("click_id"), x: try r.num("x"), y: try r.num("y"),
                                   element: ClickElement(id: er.optInt("id"), label: e["label"] as? String ?? "",
                                                         role: e["role"] as? String ?? "", hintID: er.optInt("hint_id"),
                                                         risky: er.bool("risky") ?? false)))
        case "status":
            return .status(StatusMsg(board: (m["board"] as? String) ?? "unknown", ok: r.bool("ok") ?? false,
                                     text: m["text"] as? String ?? "", rttMs: r.optInt("rtt_ms")))
        case "error":
            return .error(ErrorMsg(code: (m["code"] as? String) ?? "internal", message: m["message"] as? String ?? "",
                                   fatal: r.bool("fatal") ?? false))
        case "pong":
            return .pong(PongMsg(pingID: try r.int("ping_id"), tClient: r.optNum("t_client") ?? 0,
                                 tServer: try r.num("t_server")))
        case "ping":
            return .ping
        default:
            return .unknown(type)
        }
    }
}

/// Typed field access on a JSONSerialization object. JSON booleans arrive as NSNumber, so a
/// number field refuses a boolean and a boolean field refuses a number.
struct Reader {
    let m: [String: Any]
    let at: String
    init(_ m: [String: Any], _ at: String) { self.m = m; self.at = at }

    static func isBool(_ v: Any) -> Bool {
        guard let n = v as? NSNumber else { return false }
        return CFGetTypeID(n) == CFBooleanGetTypeID()
    }

    static func double(_ v: Any) -> Double? {
        guard let n = v as? NSNumber, !isBool(n) else { return nil }
        let d = n.doubleValue
        return d.isFinite ? d : nil
    }

    static func integer(_ v: Any) -> Int? {
        guard let d = double(v), d == d.rounded(), abs(d) < 9.0e15 else { return nil }
        return Int(d)
    }

    func num(_ k: String) throws -> Double {
        guard let v = m[k], let d = Reader.double(v) else { throw CodecError("\(at).\(k) must be a number") }
        return d
    }
    func optNum(_ k: String) -> Double? { m[k].flatMap(Reader.double) }
    func int(_ k: String) throws -> Int {
        guard let v = m[k], let i = Reader.integer(v) else { throw CodecError("\(at).\(k) must be an integer") }
        return i
    }
    func optInt(_ k: String) -> Int? { m[k].flatMap(Reader.integer) }
    func str(_ k: String) throws -> String {
        guard let s = m[k] as? String else { throw CodecError("\(at).\(k) must be a string") }
        return s
    }
    func bool(_ k: String) -> Bool? {
        guard let v = m[k], Reader.isBool(v) else { return nil }
        return (v as? NSNumber)?.boolValue
    }
}
