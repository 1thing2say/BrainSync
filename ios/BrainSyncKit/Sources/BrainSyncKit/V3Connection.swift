import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// MARK: - Socket abstraction

/// What a socket reports to its connection.
public enum V3SocketEvent: Equatable {
    /// The WebSocket handshake finished.
    case opened
    /// One text frame.
    case text(String)
    /// The socket ended: the server's close code (0 when none arrived) and reason. Comes at most
    /// once, and nothing comes after it.
    case closed(code: Int, reason: String)
}

/// The one socket `V3Connection` needs, so tests can drive the connection without a network.
/// `onEvent` may be called on any thread.
public protocol V3Socket: AnyObject {
    var onEvent: ((V3SocketEvent) -> Void)? { get set }
    func open()
    func send(_ text: String)
    /// Ends the socket from this side, with no `.closed` event.
    func close(code: Int)
}

/// `V3Socket` on `URLSessionWebSocketTask`.
public final class V3URLSessionSocket: V3Socket {
    /// A receive can fail before URLSession has recorded the server's close frame; wait this long
    /// for the close code before reporting the end (a 4000 read as a plain drop would reconnect
    /// and quietly take the headset back from the newer controller).
    static let closeSettle: TimeInterval = 0.3

    public var onEvent: ((V3SocketEvent) -> Void)?

    private let url: URL
    private let lock = NSLock()
    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private var done = false

    public init(url: URL) {
        self.url = url
    }

    deinit {
        session?.invalidateAndCancel()
    }

    public func open() {
        let delegate = V3SocketDelegate()
        delegate.owner = self
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        let task = session.webSocketTask(with: url)
        lock.lock()
        self.session = session
        self.task = task
        lock.unlock()
        task.resume()
        receive(task)
    }

    public func send(_ text: String) {
        lock.lock()
        let task = done ? nil : self.task
        lock.unlock()
        task?.send(.string(text)) { _ in } // a failed send shows up as a failed receive
    }

    public func close(code: Int) {
        lock.lock()
        let wasDone = done
        done = true
        let task = self.task, session = self.session
        lock.unlock()
        guard !wasDone else { return }
        task?.cancel(with: URLSessionWebSocketTask.CloseCode(rawValue: code) ?? .normalClosure, reason: nil)
        session?.finishTasksAndInvalidate()
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .string(let text): self.emit(.text(text))
                case .data(let data): self.emit(.text(String(decoding: data, as: UTF8.self)))
                @unknown default: break
                }
                self.receive(task)
            case .failure(let error):
                let code = task.closeCode.rawValue
                if code != 0 {
                    self.finish(code: code, reason: error.localizedDescription)
                } else {
                    DispatchQueue.global().asyncAfter(deadline: .now() + V3URLSessionSocket.closeSettle) { [weak self] in
                        self?.finish(code: task.closeCode.rawValue, reason: error.localizedDescription)
                    }
                }
            }
        }
    }

    fileprivate func opened() {
        emit(.opened)
    }

    fileprivate func finish(code: Int, reason: String) {
        lock.lock()
        let wasDone = done
        done = true
        let session = self.session
        lock.unlock()
        guard !wasDone else { return }
        onEvent?(.closed(code: code, reason: reason))
        session?.invalidateAndCancel()
    }

    private func emit(_ event: V3SocketEvent) {
        lock.lock()
        let live = !done
        lock.unlock()
        if live { onEvent?(event) }
    }
}

/// Reports the socket's open and close (with the server's close code) to its V3URLSessionSocket.
/// URLSession keeps its delegate alive until invalidated, so this holds the socket weakly.
private final class V3SocketDelegate: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    weak var owner: V3URLSessionSocket?

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol proto: String?) {
        owner?.opened()
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        let text = reason.map { String(decoding: $0, as: UTF8.self) } ?? ""
        owner?.finish(code: closeCode.rawValue, reason: text.isEmpty ? "closed (\(closeCode.rawValue))" : text)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        // A refused handshake (HTTP 403 from the Origin check, a dead tunnel) ends here.
        guard let error = error else { return }
        let status = (task.response as? HTTPURLResponse)?.statusCode
        owner?.finish(code: 0, reason: status.map { "HTTP \($0)" } ?? error.localizedDescription)
    }
}

// MARK: - Clock offset

/// The server-minus-phone clock offset from ping / pong round trips (PROTOCOL-v3 section 1).
///
/// Each sample is `t_server - (t_send + t_recv) / 2`: the server stamped the pong somewhere in
/// the round trip, assumed to be its middle, so a sample is off by at most half its round trip.
/// The estimate is the median of the last 8 samples, taken over the faster half of them (and
/// never over a round trip of more than `maxSaneRTT` while a faster one exists): a phone whose
/// Wi-Fi stalls or that wakes from sleep gives a slow, lopsided round trip that would pull a
/// plain median. The server makes its own estimate for decoding; this one is for the app
/// (logging, and showing a cycle's time on the server's clock).
public struct V3ClockEstimate: Equatable {
    public struct Sample: Equatable {
        public var offset: Double
        public var rtt: Double
    }

    public static let maxSamples = 8
    /// Round trips above this many seconds count only when no faster one is left.
    public static let maxSaneRTT: TimeInterval = 1.0

    public private(set) var samples: [Sample] = []

    public init() {}

    /// Adds one round trip: the phone's send and receive times and the server's stamp, all Unix
    /// seconds. A receive before the send (a clock step) is dropped.
    public mutating func add(tSend: Double, tRecv: Double, tServer: Double) {
        guard tSend.isFinite, tRecv.isFinite, tServer.isFinite, tRecv >= tSend else { return }
        samples.append(Sample(offset: tServer - (tSend + tRecv) / 2, rtt: tRecv - tSend))
        if samples.count > V3ClockEstimate.maxSamples { samples.removeFirst(samples.count - V3ClockEstimate.maxSamples) }
    }

    /// `t_server - t_client` in seconds, or nil before the first pong.
    public var offset: Double? {
        guard !samples.isEmpty else { return nil }
        let sane = samples.filter { $0.rtt <= V3ClockEstimate.maxSaneRTT }
        let pool = (sane.isEmpty ? samples : sane).sorted { $0.rtt < $1.rtt }
        let fast = pool.prefix((pool.count + 1) / 2)
        return V3ClockEstimate.median(fast.map(\.offset))
    }

    /// The last round trip, in seconds.
    public var lastRTT: Double? { samples.last?.rtt }

    static func median(_ values: [Double]) -> Double? {
        guard !values.isEmpty else { return nil }
        let s = values.sorted()
        let mid = s.count / 2
        return s.count % 2 == 1 ? s[mid] : (s[mid - 1] + s[mid]) / 2
    }
}

// MARK: - Connection

/// The phone's connection to a PROTOCOL-v3 server (`brainsync.server`) as a slots-mode
/// controller. It keeps one socket open: on every connect it sends the app's `hello` first
/// (built fresh by `hello`, so `refresh_hz` is the latest measurement; the token, the takeover
/// flag and `client.instance` are filled in here), then a `ping` every `pingInterval` once the
/// welcome came, which keeps the clock offset estimate. It reconnects `reconnectDelay` after a
/// drop, or after `deadAfter` seconds without any frame from the server (the server sends a
/// status every second, so silence means a dead socket: Wi-Fi changed, phone slept).
///
/// It gives up for good, with `.stopped`, when the server refuses the token (`error bad_token`,
/// close 4001), another controller holds the headset (`error busy`, close 4002; call
/// `start(takeover: true)` to replace it), a newer controller replaced this one (close 4000:
/// reconnecting would take the headset back from it), or the hello itself was refused
/// (`bad_hello`, `too_many_slots`: sending it again would loop).
///
/// `client.instance` stays the same for the connection's lifetime, so a reconnect after a drop
/// the server has not noticed yet replaces the old socket instead of getting `busy`.
///
/// Messages sent while no welcome is held are dropped (the flicker is off then anyway). All
/// socket work runs on a private serial queue; `onEvent`, the `hello` closure and the published
/// properties run and change on `callbackQueue` (main by default).
public final class V3Connection {
    public enum StopReason: Equatable {
        case badToken
        case replaced
        case busy
        /// The server refused the hello (`bad_hello`, `too_many_slots`); its message says why.
        case rejected(String)
    }

    public enum Event {
        /// The server welcomed this hello (every connect, also after a reconnect).
        case connected(V3Welcome)
        /// Every server message after the welcome (phase, layout, decision, error, ...).
        case message(V3ServerMessage)
        /// The socket dropped; a reconnect follows. Sent once per outage.
        case disconnected(String)
        case stopped(StopReason)
    }

    static let closeReplaced = 4000
    static let closeBadToken = 4001
    static let closeBusy = 4002

    /// Why a connection that ended must not come back, or nil to reconnect: from the close code,
    /// else from the fatal error that came before it.
    static func stopReason(closeCode: Int, error: V3Error? = nil) -> StopReason? {
        switch closeCode {
        case closeReplaced: return .replaced
        case closeBadToken: return .badToken
        case closeBusy: return .busy
        default: break
        }
        guard let error = error else { return nil }
        switch error.code {
        case "bad_token": return .badToken
        case "busy": return .busy
        case "bad_hello", "too_many_slots": return error.fatal ? .rejected(error.message) : nil
        default: return nil
        }
    }

    public let link: V3Link
    /// `hello.client.instance`: a random id kept for this connection's lifetime.
    public let instance: String
    public var onEvent: ((Event) -> Void)?
    /// True from the welcome until the socket drops.
    public private(set) var isConnected = false
    /// The last welcome, kept across drops.
    public private(set) var welcome: V3Welcome?
    /// `t_server - t_client` in seconds (`V3ClockEstimate`), nil before the first pong.
    public private(set) var clockOffset: Double?
    /// The last ping's round trip in milliseconds.
    public private(set) var rttMs: Double?

    private let makeHello: () -> V3Hello
    private let makeSocket: (URL) -> V3Socket
    private let clock: () -> Double
    private let reconnectDelay: TimeInterval
    private let pingInterval: TimeInterval
    private let deadAfter: TimeInterval
    private let callbackQueue: DispatchQueue
    private let queue = DispatchQueue(label: "brainsync.v3connection")

    // Owned by `queue`.
    private var socket: V3Socket?
    private var generation = 0
    private var running = false
    private var welcomed = false
    private var announcedDown = false
    private var takeoverNext = false
    private var refusal: V3Error?
    private var timer: DispatchSourceTimer?
    private var lastHeard = 0.0
    private var pingId = 0
    private var pings: [Int: Double] = [:]
    private var estimate = V3ClockEstimate()

    /// - Parameters:
    ///   - hello: builds the hello on `callbackQueue` at every connect; its token, takeover and
    ///     `client.instance` are replaced by the link's, `start`'s and `instance`.
    ///   - clock: Unix seconds, for `ping.t_client` and the round trips.
    ///   - makeSocket: the socket for `link.socketURL` (tests pass a fake).
    public init(link: V3Link, hello: @escaping () -> V3Hello, instance: String? = nil,
                reconnectDelay: TimeInterval = 1.0, pingInterval: TimeInterval = 2.0,
                deadAfter: TimeInterval = 8.0, callbackQueue: DispatchQueue = .main,
                clock: @escaping () -> Double = { Date().timeIntervalSince1970 },
                makeSocket: @escaping (URL) -> V3Socket = { V3URLSessionSocket(url: $0) }) {
        self.link = link
        self.makeHello = hello
        self.instance = instance ?? UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        self.reconnectDelay = reconnectDelay
        self.pingInterval = pingInterval
        self.deadAfter = deadAfter
        self.callbackQueue = callbackQueue
        self.clock = clock
        self.makeSocket = makeSocket
    }

    deinit {
        timer?.cancel()
        socket?.close(code: 1001)
    }

    /// Connects, and keeps reconnecting until `stop()` or a stop reason. `takeover` replaces the
    /// controller that holds the headset (after `.stopped(.busy)`); a link with `takeover=1`
    /// does the same. It applies until the next welcome.
    public func start(takeover: Bool = false) {
        queue.async { [weak self] in
            guard let self = self, !self.running else { return }
            self.running = true
            self.announcedDown = false
            self.takeoverNext = takeover || self.link.takeover
            self.connect()
        }
    }

    /// Says `bye` (the server frees the controller's place at once) and closes for good.
    public func stop() {
        queue.async { [weak self] in
            guard let self = self, self.running else { return }
            self.running = false
            if self.welcomed, let socket = self.socket, let text = try? V3ClientMessage.bye.encodedString() {
                socket.send(text)
            }
            self.teardown(code: 1000)
        }
    }

    /// Sends one message; dropped while no welcome is held. `hello` is the connection's own.
    public func send(_ message: V3ClientMessage) {
        if case .hello = message { return }
        guard let text = try? message.encodedString() else { return }
        queue.async { [weak self] in
            guard let self = self, self.welcomed, let socket = self.socket else { return }
            socket.send(text)
        }
    }

    // MARK: - Private (on `queue`)

    private func connect() {
        guard running else { return }
        generation += 1
        let gen = generation
        welcomed = false
        refusal = nil
        pings = [:]
        let socket = makeSocket(link.socketURL)
        self.socket = socket
        socket.onEvent = { [weak self] event in
            self?.queue.async { self?.handle(event, gen) }
        }
        socket.open()
    }

    private func handle(_ event: V3SocketEvent, _ gen: Int) {
        guard gen == generation, running else { return }
        switch event {
        case .opened:
            sendHello(gen)
        case .text(let text):
            lastHeard = clock()
            guard let message = try? V3ServerMessage.decode(text) else { return }
            received(message)
        case .closed(let code, let reason):
            ended(code: code, why: reason)
        }
    }

    /// Builds the hello on `callbackQueue` (the app's state lives there) and sends it, if this
    /// socket is still the current one.
    private func sendHello(_ gen: Int) {
        let token = link.token, takeover = takeoverNext, instance = self.instance
        callbackQueue.async { [weak self] in
            guard let self = self else { return }
            var hello = self.makeHello()
            hello.token = token
            hello.takeover = takeover
            hello.client.instance = instance
            guard let text = try? V3ClientMessage.hello(hello).encodedString() else { return }
            self.queue.async {
                guard gen == self.generation, self.running, let socket = self.socket else { return }
                self.lastHeard = self.clock()
                socket.send(text)
                self.startTimer(gen)
            }
        }
    }

    private func received(_ message: V3ServerMessage) {
        switch message {
        case .welcome(let w):
            welcomed = true
            takeoverNext = false
            announcedDown = false
            publish { $0.welcome = w; $0.isConnected = true }
            emit(.connected(w))
            ping()
            return
        case .pong(let p):
            if let sent = pings.removeValue(forKey: p.pingId) {
                estimate.add(tSend: sent, tRecv: clock(), tServer: p.tServer)
                let offset = estimate.offset, rtt = estimate.lastRTT.map { $0 * 1000 }
                publish { $0.clockOffset = offset; $0.rttMs = rtt }
            }
        case .error(let e):
            if V3Connection.stopReason(closeCode: 0, error: e) != nil || e.fatal { refusal = e }
        default:
            break
        }
        emit(.message(message))
    }

    private func ping() {
        guard welcomed, let socket = socket else { return }
        pingId += 1
        let now = clock()
        guard let text = try? V3ClientMessage.ping(pingId: pingId, tClient: now).encodedString() else { return }
        pings[pingId] = now
        // Pings never answered (a dropped pong) must not pile up.
        for id in pings.keys.sorted().dropLast(V3ClockEstimate.maxSamples) { pings.removeValue(forKey: id) }
        socket.send(text)
    }

    private func startTimer(_ gen: Int) {
        timer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + pingInterval, repeating: pingInterval)
        t.setEventHandler { [weak self] in
            guard let self = self, gen == self.generation, self.running else { return }
            if self.clock() - self.lastHeard > self.deadAfter {
                self.socket?.close(code: 1001)
                self.ended(code: 0, why: "no answer from the server for \(Int(self.deadAfter)) s")
            } else {
                self.ping()
            }
        }
        timer = t
        t.resume()
    }

    private func ended(code: Int, why: String) {
        let fatal = refusal
        teardown(code: nil)
        if let reason = V3Connection.stopReason(closeCode: code, error: fatal) {
            running = false
            emit(.stopped(reason))
            return
        }
        if !announcedDown {
            announcedDown = true
            emit(.disconnected(why))
        }
        let next = generation // a stop() or a newer connect moves it on
        queue.asyncAfter(deadline: .now() + reconnectDelay) { [weak self] in
            guard let self = self, self.running, next == self.generation else { return }
            self.connect()
        }
    }

    /// Drops the current socket (closing it with `code` when given) and its timer.
    private func teardown(code: Int?) {
        generation += 1
        timer?.cancel()
        timer = nil
        if let code = code { socket?.close(code: code) }
        socket = nil
        welcomed = false
        pings = [:]
        publish { $0.isConnected = false }
    }

    private func publish(_ change: @escaping (V3Connection) -> Void) {
        callbackQueue.async { [weak self] in
            guard let self = self else { return }
            change(self)
        }
    }

    private func emit(_ event: Event) {
        callbackQueue.async { [weak self] in self?.onEvent?(event) }
    }
}
