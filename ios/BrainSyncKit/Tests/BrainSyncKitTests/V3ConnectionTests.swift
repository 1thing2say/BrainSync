import XCTest
@testable import BrainSyncKit

/// A socket the test drives by hand.
private final class FakeSocket: V3Socket {
    var onEvent: ((V3SocketEvent) -> Void)?
    let url: URL
    private let lock = NSLock()
    private var _sent: [String] = []
    private(set) var closedWith: Int?
    private(set) var opened = false

    init(url: URL) {
        self.url = url
    }

    var sent: [String] {
        lock.lock(); defer { lock.unlock() }
        return _sent
    }

    /// The sent frames as JSON objects.
    var sentObjects: [[String: Any]] {
        sent.compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
    }

    func sentTypes() -> [String] {
        sentObjects.compactMap { $0["type"] as? String }
    }

    func open() {
        lock.lock(); opened = true; lock.unlock()
    }

    func send(_ text: String) {
        lock.lock(); _sent.append(text); lock.unlock()
    }

    func close(code: Int) {
        lock.lock(); closedWith = code; lock.unlock()
    }

    func deliver(_ event: V3SocketEvent) {
        onEvent?(event)
    }

    func server(_ json: String) {
        deliver(.text(json))
    }
}

private final class Box<T> {
    private let lock = NSLock()
    private var value: T

    init(_ value: T) {
        self.value = value
    }

    func get() -> T {
        lock.lock(); defer { lock.unlock() }
        return value
    }

    func update(_ change: (inout T) -> Void) {
        lock.lock(); change(&value); lock.unlock()
    }
}

final class V3ConnectionTests: XCTestCase {
    private let welcomeJSON = #"{"type":"welcome","protocol":3,"role":"controller","mode":"slots","paradigm":"code","#
        + #""fake":true,"refresh_hz":60.0,"refresh_tolerance_hz":1.0,"refresh_rates":[60.0],"freqs":[],"#
        + ##""max_slots":3,"detect":false,"code":{"bits":[0,1],"lag_frames":21,"cycle_frames":2,"train_cycles":30,"fill":"#ffffff"}}"##

    private var sockets: Box<[FakeSocket]>!
    private var events: Box<[V3Connection.Event]>!
    private var refresh: Box<Double>!
    private var now: Box<Double>!
    private let callbacks = DispatchQueue(label: "test.callbacks")

    override func setUp() {
        sockets = Box([])
        events = Box([])
        refresh = Box(60.0)
        now = Box(1000.0)
    }

    private func connection(link: V3Link? = nil, pingInterval: TimeInterval = 60, deadAfter: TimeInterval = 600,
                            reconnectDelay: TimeInterval = 0.05) -> V3Connection {
        let link = link ?? V3Link(string: "http://127.0.0.1:8765/#token=secret")!
        let sockets = self.sockets!, refresh = self.refresh!, now = self.now!
        let conn = V3Connection(link: link,
                                hello: { V3Hello.controller(slots: ["t0", "t1", "t2"], refreshHz: refresh.get()) },
                                instance: "inst-1", reconnectDelay: reconnectDelay, pingInterval: pingInterval,
                                deadAfter: deadAfter, callbackQueue: callbacks, clock: { now.get() },
                                makeSocket: { url in
                                    let s = FakeSocket(url: url)
                                    sockets.update { $0.append(s) }
                                    return s
                                })
        let events = self.events!
        conn.onEvent = { e in events.update { $0.append(e) } }
        return conn
    }

    private func eventually(_ what: String, timeout: TimeInterval = 2, _ condition: () -> Bool,
                            file: StaticString = #filePath, line: UInt = #line) {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline { usleep(2_000) }
        XCTAssertTrue(condition(), "timed out waiting for \(what)", file: file, line: line)
    }

    /// Lets queued work on the connection and callback queues run.
    private func settle(_ seconds: TimeInterval = 0.05) {
        usleep(useconds_t(seconds * 1_000_000))
        callbacks.sync {}
    }

    private func socket(_ i: Int) -> FakeSocket {
        sockets.get()[i]
    }

    /// Opens socket `i`, waits for its hello and welcomes it.
    private func welcome(_ i: Int) {
        eventually("socket \(i)") { sockets.get().count > i }
        socket(i).deliver(.opened)
        eventually("hello on socket \(i)") { socket(i).sentTypes().contains("hello") }
        socket(i).server(welcomeJSON)
        eventually("connected") { stops().isEmpty && connects() > i }
    }

    private func connects() -> Int {
        events.get().filter { if case .connected = $0 { return true } else { return false } }.count
    }

    private func stops() -> [V3Connection.StopReason] {
        events.get().compactMap { if case .stopped(let r) = $0 { return r } else { return nil } }
    }

    private func disconnects() -> Int {
        events.get().filter { if case .disconnected = $0 { return true } else { return false } }.count
    }

    func testCloseCodesAndErrors() {
        XCTAssertEqual(V3Connection.stopReason(closeCode: 4000), .replaced)
        XCTAssertEqual(V3Connection.stopReason(closeCode: 4001), .badToken)
        XCTAssertEqual(V3Connection.stopReason(closeCode: 4002), .busy)
        XCTAssertEqual(V3Connection.stopReason(closeCode: 1008, error: V3Error(code: "bad_token", fatal: true)), .badToken)
        XCTAssertEqual(V3Connection.stopReason(closeCode: 0, error: V3Error(code: "busy", fatal: true)), .busy)
        XCTAssertEqual(V3Connection.stopReason(closeCode: 1008, error: V3Error(code: "too_many_slots", message: "m", fatal: true)),
                       .rejected("m"))
        // Everything else reconnects: server restart, network drop, tunnel hiccup, hello timeout.
        for code in [0, 1000, 1001, 1006, 1011, 4003, 4008] {
            XCTAssertNil(V3Connection.stopReason(closeCode: code), "\(code)")
        }
        XCTAssertNil(V3Connection.stopReason(closeCode: 1006, error: V3Error(code: "refresh", fatal: false)))
        XCTAssertNil(V3Connection.stopReason(closeCode: 1011, error: V3Error(code: "internal", fatal: true)))
    }

    func testHelloFirstWithTokenInstanceAndFreshRefresh() throws {
        let conn = connection()
        conn.start()
        eventually("a socket") { sockets.get().count == 1 }
        XCTAssertEqual(socket(0).url.absoluteString, "ws://127.0.0.1:8765/ws")
        XCTAssertTrue(socket(0).opened)
        XCTAssertTrue(socket(0).sent.isEmpty, "nothing before the socket opens")
        socket(0).deliver(.opened)
        eventually("hello") { !socket(0).sent.isEmpty }
        let hello = try XCTUnwrap(socket(0).sentObjects.first)
        XCTAssertEqual(hello["type"] as? String, "hello")
        XCTAssertEqual(hello["token"] as? String, "secret")
        XCTAssertEqual(hello["takeover"] as? Bool, false)
        XCTAssertEqual(hello["refresh_hz"] as? Double, 60)
        XCTAssertEqual((hello["client"] as? [String: Any])?["instance"] as? String, "inst-1")
        XCTAssertEqual(try V3ClientMessage.decode(socket(0).sent[0]),
                       .hello(V3Hello(token: "secret", role: .controller,
                                      client: V3ClientInfo(instance: "inst-1"), mode: "slots",
                                      slots: ["t0", "t1", "t2"], refreshHz: 60, accepted: true)))

        // A drop: one disconnected event, then a new socket whose hello has the same instance and
        // the refresh measured now.
        socket(0).server(welcomeJSON)
        eventually("connected") { connects() == 1 }
        refresh.update { $0 = 59.94 }
        socket(0).deliver(.closed(code: 1006, reason: "gone"))
        eventually("reconnect") { sockets.get().count == 2 }
        XCTAssertEqual(disconnects(), 1)
        socket(1).deliver(.opened)
        eventually("second hello") { !socket(1).sent.isEmpty }
        let again = try XCTUnwrap(socket(1).sentObjects.first)
        XCTAssertEqual(again["refresh_hz"] as? Double, 59.94)
        XCTAssertEqual((again["client"] as? [String: Any])?["instance"] as? String, "inst-1")
        conn.stop()
    }

    func testWelcomeMessagesAndSendsDroppedWhileDisconnected() throws {
        let conn = connection()
        conn.start()
        eventually("a socket") { sockets.get().count == 1 }
        socket(0).deliver(.opened)
        eventually("hello") { !socket(0).sent.isEmpty }
        conn.send(.cycle(layoutId: 1, n: 0, tClient: 1))   // before the welcome: dropped
        conn.send(.hello(V3Hello.viewer()))                 // never: the hello is the connection's
        settle()
        XCTAssertEqual(socket(0).sentTypes(), ["hello"])

        socket(0).server(welcomeJSON)
        eventually("connected") { connects() == 1 }
        callbacks.sync {}
        XCTAssertTrue(conn.isConnected)
        XCTAssertEqual(conn.welcome?.paradigm, .code)
        XCTAssertEqual(conn.welcome?.code?.lagFrames, 21)

        socket(0).server(#"{"type":"phase","phase":"train","flicker":true,"seconds":null,"text":"Look at the T0 square","cue":"t0","cycles":30}"#)
        socket(0).server(#"{"type":"scan_result","scan_id":1}"#)
        socket(0).server("garbage")
        eventually("two messages") {
            events.get().filter { if case .message = $0 { return true } else { return false } }.count == 2
        }
        let messages = events.get().compactMap { e -> V3ServerMessage? in
            if case .message(let m) = e { return m } else { return nil }
        }
        XCTAssertEqual(messages.first?.type, "phase")
        XCTAssertEqual(messages.last, .unknown("scan_result"))

        conn.send(.cycle(layoutId: 1, n: 0, tClient: 1))
        eventually("cycle sent") { socket(0).sentTypes().contains("cycle") }

        socket(0).deliver(.closed(code: 1006, reason: "drop"))
        eventually("disconnected") { disconnects() == 1 }
        callbacks.sync {}
        XCTAssertFalse(conn.isConnected)
        conn.send(.pause)
        settle()
        XCTAssertFalse(socket(0).sentTypes().contains("pause"))
        conn.stop()
    }

    func testPingsAndClockOffset() throws {
        let conn = connection(pingInterval: 0.05)
        conn.start()
        welcome(0)
        // The first ping goes out with the welcome.
        eventually("ping") { socket(0).sentTypes().contains("ping") }
        let ping = try XCTUnwrap(socket(0).sentObjects.first { $0["type"] as? String == "ping" })
        XCTAssertEqual(ping["ping_id"] as? Int, 1)
        XCTAssertEqual(ping["t_client"] as? Double, 1000)
        // The pong comes back 40 ms later, stamped by a server clock 2.5 s ahead.
        now.update { $0 = 1000.04 }
        socket(0).server(#"{"type":"pong","ping_id":1,"t_client":1000.0,"t_server":1002.52}"#)
        eventually("offset") { var o: Double?; callbacks.sync { o = conn.clockOffset }; return o != nil }
        callbacks.sync {}
        XCTAssertEqual(conn.clockOffset ?? 0, 2.5, accuracy: 1e-9)
        XCTAssertEqual(conn.rttMs ?? 0, 40, accuracy: 1e-6)
        // The timer keeps pinging.
        eventually("more pings") { socket(0).sentTypes().filter { $0 == "ping" }.count >= 3 }
        // An unknown pong changes nothing.
        socket(0).server(#"{"type":"pong","ping_id":999,"t_client":0,"t_server":0}"#)
        settle()
        XCTAssertEqual(conn.clockOffset ?? 0, 2.5, accuracy: 1e-9)
        conn.stop()
    }

    func testReplacedStopsForGood() {
        let conn = connection()
        conn.start()
        welcome(0)
        socket(0).deliver(.closed(code: 4000, reason: "replaced"))
        eventually("stopped") { stops() == [.replaced] }
        settle(0.2)
        XCTAssertEqual(sockets.get().count, 1, "no reconnect after 4000")
        XCTAssertEqual(disconnects(), 0)
        _ = conn
    }

    func testBadTokenStops() {
        let conn = connection()
        conn.start()
        eventually("a socket") { sockets.get().count == 1 }
        socket(0).deliver(.opened)
        socket(0).server(#"{"type":"error","code":"bad_token","message":"wrong token","fatal":true}"#)
        // The close code may never arrive (0): the error alone decides.
        socket(0).deliver(.closed(code: 0, reason: "closed"))
        eventually("stopped") { stops() == [.badToken] }
        settle(0.2)
        XCTAssertEqual(sockets.get().count, 1)
        _ = conn
    }

    func testBusyThenTakeover() throws {
        let conn = connection()
        conn.start()
        eventually("a socket") { sockets.get().count == 1 }
        socket(0).deliver(.opened)
        socket(0).server(#"{"type":"error","code":"busy","message":"another controller","fatal":true}"#)
        socket(0).deliver(.closed(code: 4002, reason: "busy"))
        eventually("stopped") { stops() == [.busy] }
        settle(0.2)
        XCTAssertEqual(sockets.get().count, 1)

        conn.start(takeover: true)
        eventually("a new socket") { sockets.get().count == 2 }
        socket(1).deliver(.opened)
        eventually("hello") { !socket(1).sent.isEmpty }
        XCTAssertEqual(socket(1).sentObjects.first?["takeover"] as? Bool, true)
        socket(1).server(welcomeJSON)
        eventually("connected") { connects() == 1 }
        // After the welcome a reconnect is the same instance again: no takeover needed.
        socket(1).deliver(.closed(code: 1006, reason: "drop"))
        eventually("reconnect") { sockets.get().count == 3 }
        socket(2).deliver(.opened)
        eventually("hello") { !socket(2).sent.isEmpty }
        XCTAssertEqual(socket(2).sentObjects.first?["takeover"] as? Bool, false)
        conn.stop()
    }

    func testTakeoverLink() throws {
        let conn = connection(link: V3Link(string: "http://h:8765/#token=t&takeover=1")!)
        conn.start()
        eventually("a socket") { sockets.get().count == 1 }
        socket(0).deliver(.opened)
        eventually("hello") { !socket(0).sent.isEmpty }
        XCTAssertEqual(socket(0).sentObjects.first?["takeover"] as? Bool, true)
        conn.stop()
    }

    func testRefusedHelloStops() {
        let conn = connection()
        conn.start()
        eventually("a socket") { sockets.get().count == 1 }
        socket(0).deliver(.opened)
        socket(0).server(#"{"type":"error","code":"too_many_slots","message":"at most 3","fatal":true}"#)
        socket(0).deliver(.closed(code: 1008, reason: "bad hello"))
        eventually("stopped") { stops() == [.rejected("at most 3")] }
        settle(0.2)
        XCTAssertEqual(sockets.get().count, 1)
        _ = conn
    }

    func testANonFatalErrorIsJustAMessage() {
        let conn = connection()
        conn.start()
        welcome(0)
        socket(0).server(#"{"type":"error","code":"refresh","message":"set 60 Hz","fatal":false}"#)
        eventually("error message") {
            events.get().contains { if case .message(.error(let e)) = $0 { return e.code == "refresh" } else { return false } }
        }
        socket(0).deliver(.closed(code: 1006, reason: "drop"))
        eventually("reconnect") { sockets.get().count == 2 }
        XCTAssertTrue(stops().isEmpty)
        conn.stop()
    }

    func testStopSaysByeAndCloses() {
        let conn = connection()
        conn.start()
        welcome(0)
        conn.stop()
        eventually("closed") { socket(0).closedWith == 1000 }
        XCTAssertEqual(socket(0).sentTypes().last, "bye")
        callbacks.sync {}
        XCTAssertFalse(conn.isConnected)
        // Events from the old socket are ignored now.
        socket(0).deliver(.closed(code: 1000, reason: "bye"))
        settle(0.2)
        XCTAssertEqual(sockets.get().count, 1)
        XCTAssertEqual(disconnects(), 0)
    }

    func testSilentServerCountsAsDead() {
        let conn = connection(pingInterval: 0.03, deadAfter: 5)
        conn.start()
        welcome(0)
        now.update { $0 += 6 }   // no frame for longer than deadAfter
        eventually("closed") { socket(0).closedWith != nil }
        eventually("reconnect") { sockets.get().count == 2 }
        XCTAssertEqual(disconnects(), 1)
        conn.stop()
    }

    func testClockEstimatePrefersFastRoundTrips() {
        var e = V3ClockEstimate()
        XCTAssertNil(e.offset)
        e.add(tSend: 0, tRecv: 0.02, tServer: 10.01)      // offset 10.00, rtt 20 ms
        XCTAssertEqual(e.offset ?? 0, 10, accuracy: 1e-9)
        e.add(tSend: 1, tRecv: 1.6, tServer: 11.8)         // offset 10.5 (lopsided), rtt 600 ms
        e.add(tSend: 2, tRecv: 2.03, tServer: 12.025)      // offset 10.01, rtt 30 ms
        e.add(tSend: 3, tRecv: 4.5, tServer: 15.0)         // offset 11.25, rtt 1.5 s: not sane
        // The faster half of the sane ones: 20 ms and 30 ms.
        XCTAssertEqual(e.offset ?? 0, 10.005, accuracy: 1e-9)
        XCTAssertEqual(e.lastRTT ?? 0, 1.5, accuracy: 1e-9)
        e.add(tSend: 5, tRecv: 4, tServer: 1)              // receive before send: dropped
        XCTAssertEqual(e.samples.count, 4)
        for i in 0..<20 { e.add(tSend: Double(i), tRecv: Double(i) + 0.01, tServer: Double(i) + 7) }
        XCTAssertEqual(e.samples.count, V3ClockEstimate.maxSamples)
        XCTAssertEqual(e.offset ?? 0, 6.995, accuracy: 1e-9)

        var slow = V3ClockEstimate()                       // only slow ones: still an estimate
        slow.add(tSend: 0, tRecv: 2, tServer: 4)
        XCTAssertEqual(slow.offset ?? 0, 3, accuracy: 1e-9)
    }
}

/// Against a real server, only when BRAINSYNC_V3_LINK is set, for example:
///
///     python -m brainsync.server --board fake --no-auth --train-cycles 6 --port 18765
///     BRAINSYNC_V3_LINK=http://127.0.0.1:18765/ swift test --filter V3LiveServerTests
///
/// It trains on t0 with the fake board, then looks at t1 (`gaze`) until the server selects it.
final class V3LiveServerTests: XCTestCase {
    func testTrainThenSelectOnARealServer() throws {
        guard let text = ProcessInfo.processInfo.environment["BRAINSYNC_V3_LINK"], let link = V3Link(string: text) else {
            throw XCTSkip("set BRAINSYNC_V3_LINK to run against a live brainsync.server")
        }
        let callbacks = DispatchQueue(label: "live.callbacks")
        let messages = Box<[V3ServerMessage]>([])
        let welcomes = Box<[V3Welcome]>([])
        let conn = V3Connection(link: link, hello: {
            V3Hello.controller(slots: ["t0", "t1", "t2"], refreshHz: 60, viewport: V3Viewport(w: 390, h: 844, dpr: 3),
                               client: V3ClientInfo(version: "test"))
        }, callbackQueue: callbacks)
        conn.onEvent = { event in
            switch event {
            case .connected(let w): welcomes.update { $0.append(w) }
            case .message(let m): messages.update { $0.append(m) }
            case .stopped(let r): XCTFail("stopped: \(r)")
            case .disconnected: break
            }
        }
        func waitFor(_ what: String, _ timeout: TimeInterval, _ cond: () -> Bool) {
            let deadline = Date().addingTimeInterval(timeout)
            while !cond() && Date() < deadline { usleep(20_000) }
            XCTAssertTrue(cond(), "timed out waiting for \(what)")
        }
        conn.start(takeover: true)
        waitFor("welcome", 5) { !welcomes.get().isEmpty }
        let w = try XCTUnwrap(welcomes.get().first)
        XCTAssertEqual(w.paradigm, .code)
        XCTAssertEqual(w.code?.bits.count, 63)
        waitFor("slots layout", 5) {
            messages.get().contains { if case .layout(let l) = $0 { return l.targets.map(\.lag) == [0, 21, 42] } else { return false } }
        }
        let layoutId = messages.get().compactMap { m -> Int? in if case .layout(let l) = m { return l.layoutId } else { return nil } }.last!
        conn.send(.onset(layoutId: layoutId, tClient: Date().timeIntervalSince1970))
        waitFor("trained", 30) { messages.get().contains { if case .trained = $0 { return true } else { return false } } }
        waitFor("clock offset", 5) { var o: Double?; callbacks.sync { o = conn.clockOffset }; return o != nil }
        conn.send(.gaze(slot: "t1"))
        waitFor("a decision", 10) { messages.get().contains { if case .decision = $0 { return true } else { return false } } }
        waitFor("t1 selected", 20) {
            messages.get().contains { if case .selection(let s) = $0 { return s.slot == "t1" } else { return false } }
        }
        conn.send(.frames(refreshHz: 60, dropped: 0))
        let errors = messages.get().compactMap { m -> V3Error? in if case .error(let e) = m { return e } else { return nil } }
        XCTAssertTrue(errors.isEmpty, "server errors: \(errors)")
        conn.stop()
        usleep(200_000)
    }
}
