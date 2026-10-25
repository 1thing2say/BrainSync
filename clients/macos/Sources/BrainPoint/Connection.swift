import BrainPointCore
import Foundation

/// One WebSocket to the BrainSync server (URLSessionWebSocketTask), with the hello handshake,
/// reconnecting after 1, 2, 4, then every 5 s, except after close code 4000 (replaced by another
/// controller), 4001 (bad token) or 4002 (busy), or a fatal bad_token / busy / bad_hello error:
/// those need the user, so it stops until `open()` is called again. Pings every 10 s once
/// welcomed, for the round-trip time.
@MainActor
final class Connection {
    enum State: String { case closed, connecting, open }

    var onState: ((State) -> Void)?
    var onText: ((String) -> Void)?
    /// (close code or nil, reason, seconds until the next attempt or nil when stopped)
    var onClose: ((Int?, String, Double?) -> Void)?
    var onRTT: ((Int) -> Void)?
    var hello: (() -> String?)?
    var log: ((String, [String: Any]) -> Void)?

    private(set) var url: URL
    private(set) var state: State = .closed
    private(set) var stopped = true
    private(set) var rttMs: Int?
    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private var delegate: Delegate?
    private var attempt = 0
    private var retry: Timer?
    private var pingTimer: Timer?
    private var pingID = 0
    private var pings: [Int: Double] = [:]
    private var stopForFatal: String?

    init(url: URL) { self.url = url }

    /// Start connecting (again, also after a no-reconnect close).
    func open() {
        stopped = false
        stopForFatal = nil
        retry?.invalidate(); retry = nil
        if task == nil { connect() }
    }

    /// Drop the connection and connect to `url` at once with a fresh hello.
    func reconnect(to url: URL) {
        self.url = url
        drop(code: .normalClosure, reason: "reconnect")
        attempt = 0
        open()
    }

    /// Leave for good: bye, close, no reconnect.
    func close(bye: Bool) {
        stopped = true
        retry?.invalidate(); retry = nil
        if bye, let text = Codec.encode(Codec.bye()) { _ = send(text) }
        drop(code: .normalClosure, reason: "bye")
    }

    /// A fatal server error the user must fix: do not reconnect after the close that follows.
    func noteFatal(code: String) {
        if ["bad_token", "busy", "bad_hello"].contains(code) { stopForFatal = code }
    }

    @discardableResult
    func send(_ text: String) -> Bool {
        guard state == .open, let t = task else { return false }
        t.send(.string(text)) { [weak self] err in
            guard let err else { return }
            let msg = err.localizedDescription
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.log?("send_failed", ["error": msg]) } }
        }
        return true
    }

    /// The server welcomed us: the attempt counter resets and pings start.
    func welcomed() {
        attempt = 0
        pingTimer?.invalidate()
        ping()
        pingTimer = Timer.scheduledTimer(withTimeInterval: Proto.pingEveryS, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.ping() }
        }
    }

    /// A pong: the round-trip time in ms, or nil for an unknown ping id.
    @discardableResult
    func pong(_ p: PongMsg) -> Int? {
        guard let sent = pings.removeValue(forKey: p.pingID) else { return nil }
        let ms = Int(((ProcessInfo.processInfo.systemUptime - sent) * 1000).rounded())
        rttMs = max(0, ms)
        onRTT?(rttMs!)
        return rttMs
    }

    private func ping() {
        pingID += 1
        guard let text = Codec.encode(Codec.ping(pingID: pingID, tClient: Date().timeIntervalSince1970)), send(text) else { return }
        pings[pingID] = ProcessInfo.processInfo.systemUptime
        if pings.count > 8, let oldest = pings.keys.min() { pings.removeValue(forKey: oldest) }
    }

    private func setState(_ s: State) {
        guard s != state else { return }
        state = s
        onState?(s)
    }

    private func connect() {
        setState(.connecting)
        let d = Delegate()
        d.owner = self
        let s = URLSession(configuration: .ephemeral, delegate: d, delegateQueue: .main)
        let t = s.webSocketTask(with: url)
        t.maximumMessageSize = 8 * 1024 * 1024
        delegate = d; session = s; task = t
        log?("connecting", ["url": url.absoluteString, "attempt": attempt])
        t.resume()
        receive(t)
    }

    private func receive(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.task === t else { return }
                    switch result {
                    case .success(.string(let s)):
                        self.onText?(s)
                        self.receive(t)
                    case .success(.data(let d)):
                        self.log?("ignored_binary", ["bytes": d.count])
                        self.receive(t)
                    case .success:
                        self.receive(t)
                    case .failure(let e):
                        self.closed(t, code: t.closeCode == .invalid ? nil : t.closeCode.rawValue,
                                    reason: t.closeReason.map { String(decoding: $0, as: UTF8.self) } ?? e.localizedDescription)
                    }
                }
            }
        }
    }

    fileprivate func opened(_ t: URLSessionWebSocketTask) {
        guard task === t else { return }
        setState(.open)
        if let h = hello?() { _ = send(h) } else { log?("hello_failed", ["why": "could not build the hello"]) }
    }

    fileprivate func closed(_ t: URLSessionTask, code: Int?, reason: String) {
        guard task === t else { return }
        task = nil
        session?.invalidateAndCancel(); session = nil; delegate = nil
        pingTimer?.invalidate(); pingTimer = nil
        pings.removeAll()
        rttMs = nil
        let noRetry = code.map { Proto.noReconnectCodes.contains($0) } ?? false || reason == "replaced" || stopForFatal != nil
        if noRetry { stopped = true }
        setState(.closed)
        var wait: Double?
        if !stopped {
            wait = Proto.backoffS[min(attempt, Proto.backoffS.count - 1)]
            attempt += 1
            retry = Timer.scheduledTimer(withTimeInterval: wait!, repeats: false) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.retry = nil
                    if !self.stopped && self.task == nil { self.connect() }
                }
            }
        }
        onClose?(code, stopForFatal.map { "\(reason) (\($0))" } ?? reason, wait)
    }

    private func drop(code: URLSessionWebSocketTask.CloseCode, reason: String) {
        guard let t = task else { setState(.closed); return }
        task = nil
        t.cancel(with: code, reason: reason.data(using: .utf8))
        session?.finishTasksAndInvalidate(); session = nil; delegate = nil
        pingTimer?.invalidate(); pingTimer = nil
        pings.removeAll()
        setState(.closed)
        onClose?(code.rawValue, reason, nil)
    }

    /// URLSession delegate on the main queue; forwards to the connection.
    private final class Delegate: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
        weak var owner: Connection?

        func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                        didOpenWithProtocol protocol: String?) {
            MainActor.assumeIsolated { owner?.opened(webSocketTask) }
        }

        func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
            let text = reason.map { String(decoding: $0, as: UTF8.self) } ?? ""
            MainActor.assumeIsolated { owner?.closed(webSocketTask, code: closeCode.rawValue, reason: text) }
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            let ws = task as? URLSessionWebSocketTask
            let code = ws.flatMap { $0.closeCode == .invalid ? nil : $0.closeCode.rawValue }
            let reason = ws?.closeReason.map { String(decoding: $0, as: UTF8.self) } ?? error?.localizedDescription ?? "closed"
            MainActor.assumeIsolated { owner?.closed(task, code: code, reason: reason) }
        }
    }
}
