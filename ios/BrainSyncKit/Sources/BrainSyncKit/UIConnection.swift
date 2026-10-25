import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// The app's /ws/ui connection. The server owns all state and re-sends it on every connect, so
/// this only keeps one socket open: it reconnects `reconnectDelay` after a drop, drops messages
/// sent while disconnected, and gives up for good when the server says the key is wrong
/// (close code 4403 / HTTP 403) or a newer partner page replaced this one (close code 4000).
///
/// All socket work runs on a private serial queue; `onEvent` and `isConnected` change on
/// `callbackQueue` (main by default).
public final class UIConnection {
    public enum StopReason: Equatable {
        case badKey, replaced
    }

    public enum Event {
        case connected
        case message(ServerMessage)
        case disconnected(String)
        case stopped(StopReason)
    }

    /// app/remote.py FORBIDDEN
    static let closeForbidden = 4403
    /// app/server.py REPLACED
    static let closeReplaced = 4000
    static let pingInterval: TimeInterval = 10
    /// A receive can fail before URLSession has recorded the server's close frame; wait this long
    /// for the close code before deciding whether to reconnect.
    static let closeSettle: TimeInterval = 0.3

    /// Why a connection that ended must not come back, or nil to reconnect. `closeCode` is the
    /// websocket close code (0 when none arrived); `httpStatus` the handshake's HTTP status, if
    /// the upgrade was refused (the server refuses a keyless remote socket before accepting it).
    static func stopReason(closeCode: Int, httpStatus: Int?) -> StopReason? {
        if closeCode == closeForbidden || httpStatus == 403 { return .badKey }
        if closeCode == closeReplaced { return .replaced }
        return nil
    }

    public let link: ServerLink
    public var onEvent: ((Event) -> Void)?
    public private(set) var isConnected = false

    private let session: URLSession
    /// The session this connection made for itself (when the caller passed `.shared`), so its
    /// delegate reports the open and the close code; invalidated in deinit.
    private let ownedSession: URLSession?
    private let reconnectDelay: TimeInterval
    private let callbackQueue: DispatchQueue
    private let queue = DispatchQueue(label: "brainsync.uiconnection")

    // Owned by `queue`.
    private var task: URLSessionWebSocketTask?
    private var generation = 0
    private var running = false
    private var connected = false
    private var announcedDown = false
    private var pingTimer: DispatchSourceTimer?
    private var pingOutstanding = false
    /// A task that ended without a known close code, waiting `closeSettle` for one.
    private var settling: URLSessionWebSocketTask?

    public init(link: ServerLink, session: URLSession = .shared, reconnectDelay: TimeInterval = 1.0,
                callbackQueue: DispatchQueue = .main) {
        self.link = link
        self.reconnectDelay = reconnectDelay
        self.callbackQueue = callbackQueue
        if session === URLSession.shared {
            let delegate = SocketDelegate()
            let own = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
            self.session = own
            self.ownedSession = own
            delegate.owner = self
        } else {
            self.session = session
            self.ownedSession = nil
        }
    }

    deinit {
        pingTimer?.cancel()
        task?.cancel(with: .goingAway, reason: nil)
        ownedSession?.invalidateAndCancel()
    }

    public func start() {
        queue.async { [weak self] in
            guard let self = self, !self.running else { return }
            self.running = true
            self.announcedDown = false
            self.connect()
        }
    }

    public func stop() {
        queue.async { [weak self] in
            guard let self = self else { return }
            self.running = false
            self.teardown(closeCode: .normalClosure)
            self.setConnected(false)
        }
    }

    public func send(_ message: ClientMessage) {
        guard let data = try? message.encoded() else { return }
        let text = String(decoding: data, as: UTF8.self)
        queue.async { [weak self] in
            guard let self = self, self.connected, let task = self.task else { return }
            task.send(.string(text)) { _ in } // a failed send shows up as a failed receive
        }
    }

    // MARK: - Private (on `queue`)

    private func connect() {
        guard running else { return }
        generation += 1
        let gen = generation
        let task = session.webSocketTask(with: link.uiSocketURL)
        self.task = task
        pingOutstanding = false
        task.resume()
        receive(task, gen)
        // The delegate reports the open; with a caller's session (no delegate of ours) the first
        // pong or message means connected.
        sendPing(task, gen)
        startPingTimer(gen)
    }

    private func receive(_ task: URLSessionWebSocketTask, _ gen: Int) {
        task.receive { [weak self] result in
            guard let self = self else { return }
            self.queue.async {
                guard gen == self.generation, self.running else { return }
                switch result {
                case .success(let message):
                    self.markConnected()
                    let data: Data
                    switch message {
                    case .string(let text): data = Data(text.utf8)
                    case .data(let d): data = d
                    @unknown default: data = Data()
                    }
                    if let decoded = try? ServerMessage.decode(data) {
                        self.emit(.message(decoded))
                    }
                    self.receive(task, gen)
                case .failure(let error):
                    self.ended(task, gen, why: error.localizedDescription)
                }
            }
        }
    }

    private func sendPing(_ task: URLSessionWebSocketTask, _ gen: Int) {
        pingOutstanding = true
        task.sendPing { [weak self] error in
            guard let self = self else { return }
            self.queue.async {
                guard gen == self.generation, self.running else { return }
                if let error = error {
                    self.ended(task, gen, why: error.localizedDescription)
                } else {
                    self.pingOutstanding = false
                    self.markConnected()
                }
            }
        }
    }

    private func startPingTimer(_ gen: Int) {
        pingTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + UIConnection.pingInterval, repeating: UIConnection.pingInterval)
        timer.setEventHandler { [weak self] in
            guard let self = self, gen == self.generation, self.running, let task = self.task else { return }
            if self.pingOutstanding {
                // The last ping never came back: the socket is dead (Wi-Fi changed, phone slept).
                self.ended(task, gen, why: "no answer to ping")
            } else {
                self.sendPing(task, gen)
            }
        }
        pingTimer = timer
        timer.resume()
    }

    /// The delegate saw the handshake finish: the real "connected" signal.
    fileprivate func socketOpened(_ task: URLSessionWebSocketTask) {
        queue.async { [weak self] in
            guard let self = self, self.running, self.settling == nil, task === self.task else { return }
            self.markConnected()
        }
    }

    /// The delegate saw the server's close frame, with its code.
    fileprivate func socketClosed(_ task: URLSessionWebSocketTask, code: Int) {
        queue.async { [weak self] in
            guard let self = self, self.running else { return }
            if let pending = self.settling, pending === task {
                self.finish(task, code: code, why: "closed (\(code))")
            } else if task === self.task {
                self.ended(task, self.generation, why: "closed (\(code))", closeCode: code)
            }
        }
    }

    /// `closeCode` comes from the delegate; without it the task's property is read, and a missing
    /// code (0, no HTTP 403) gets `closeSettle` to arrive: the receive often fails first, and
    /// reconnecting on a 4000 would quietly take the session back from the newer partner page.
    private func ended(_ task: URLSessionWebSocketTask, _ gen: Int, why: String, closeCode: Int? = nil) {
        guard gen == generation, settling == nil else { return }
        let status = (task.response as? HTTPURLResponse)?.statusCode
        let code = closeCode ?? task.closeCode.rawValue
        if closeCode == nil && code == 0 && status != 403 {
            generation += 1  // callbacks from this task are stale now
            pingTimer?.cancel()
            pingTimer = nil
            pingOutstanding = false
            setConnected(false)
            settling = task
            queue.asyncAfter(deadline: .now() + UIConnection.closeSettle) { [weak self] in
                guard let self = self, let pending = self.settling, pending === task else { return }
                self.finish(task, code: task.closeCode.rawValue, why: why)
            }
            return
        }
        finish(task, code: code, why: why)
    }

    private func finish(_ task: URLSessionWebSocketTask, code: Int, why: String) {
        let status = (task.response as? HTTPURLResponse)?.statusCode
        teardown(closeCode: .goingAway)
        setConnected(false)
        if let reason = UIConnection.stopReason(closeCode: code, httpStatus: status) {
            running = false
            emit(.stopped(reason))
            return
        }
        if !announcedDown {
            announcedDown = true
            emit(.disconnected(why))
        }
        let next = generation // teardown moved it on; a stop() or newer connect moves it again
        queue.asyncAfter(deadline: .now() + reconnectDelay) { [weak self] in
            guard let self = self, self.running, next == self.generation else { return }
            self.connect()
        }
    }

    private func teardown(closeCode: URLSessionWebSocketTask.CloseCode) {
        generation += 1
        settling = nil
        pingTimer?.cancel()
        pingTimer = nil
        pingOutstanding = false
        task?.cancel(with: closeCode, reason: nil)
        task = nil
    }

    private func markConnected() {
        guard !connected else { return }
        announcedDown = false
        setConnected(true)
        emit(.connected)
    }

    private func setConnected(_ value: Bool) {
        connected = value
        callbackQueue.async { [weak self] in self?.isConnected = value }
    }

    private func emit(_ event: Event) {
        callbackQueue.async { [weak self] in self?.onEvent?(event) }
    }
}

/// Reports the socket's open and close (with the server's close code) to its UIConnection.
/// URLSession keeps its delegate alive, so this holds the connection weakly.
private final class SocketDelegate: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    weak var owner: UIConnection?

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol proto: String?) {
        owner?.socketOpened(webSocketTask)
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        owner?.socketClosed(webSocketTask, code: closeCode.rawValue)
    }
}
