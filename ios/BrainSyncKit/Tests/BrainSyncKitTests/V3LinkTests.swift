import XCTest
@testable import BrainSyncKit

final class V3LinkTests: XCTestCase {
    func testTheLinkTheServerPrints() throws {
        // brainsync/server/app.py browser_url
        let local = try XCTUnwrap(V3Link(string: "http://127.0.0.1:8765/#token=Zx9-abc_DEF123"))
        XCTAssertEqual(local.base.absoluteString, "http://127.0.0.1:8765")
        XCTAssertEqual(local.token, "Zx9-abc_DEF123")
        XCTAssertFalse(local.takeover)
        XCTAssertEqual(local.socketURL.absoluteString, "ws://127.0.0.1:8765/ws")
        XCTAssertEqual(local.healthURL.absoluteString, "http://127.0.0.1:8765/health")
        XCTAssertEqual(local.hostLabel, "127.0.0.1:8765")
        XCTAssertEqual(local.pageURL.absoluteString, "http://127.0.0.1:8765/#token=Zx9-abc_DEF123")
    }

    func testTheJetsonTunnelLink() throws {
        // deploy/jetson/cloudflare/brainsync-tunnel.sh writes https://<host>/#token=<token>
        let tunnel = try XCTUnwrap(V3Link(string: "  https://quiet-river-1234.trycloudflare.com/#token=abc_DEF-9\n"))
        XCTAssertEqual(tunnel.token, "abc_DEF-9")
        XCTAssertEqual(tunnel.socketURL.absoluteString, "wss://quiet-river-1234.trycloudflare.com/ws")
        XCTAssertEqual(tunnel.hostLabel, "quiet-river-1234.trycloudflare.com")
        // LINK_WITH_TOKEN=0, or a --no-auth server: no token.
        let bare = try XCTUnwrap(V3Link(string: "https://quiet-river-1234.trycloudflare.com/"))
        XCTAssertNil(bare.token)
        XCTAssertEqual(bare.pageURL.absoluteString, "https://quiet-river-1234.trycloudflare.com/")
    }

    func testTakeoverInTheFragment() throws {
        let link = try XCTUnwrap(V3Link(string: "http://192.168.1.20:8765/#token=t0k&takeover=1"))
        XCTAssertEqual(link.token, "t0k")
        XCTAssertTrue(link.takeover)
    }

    func testTypedAddressGetsHttp() throws {
        let link = try XCTUnwrap(V3Link(string: "192.168.1.20:8765"))
        XCTAssertEqual(link.base.absoluteString, "http://192.168.1.20:8765")
        XCTAssertNil(link.token)
        XCTAssertEqual(link.socketURL.absoluteString, "ws://192.168.1.20:8765/ws")
    }

    func testAppLinkRoundTrip() throws {
        let link = try XCTUnwrap(V3Link(base: URL(string: "https://a.trycloudflare.com/some/path?x=1")!, token: "a+b/c=d"))
        XCTAssertEqual(link.base.absoluteString, "https://a.trycloudflare.com")
        let app = link.appLink.absoluteString
        XCTAssertTrue(app.hasPrefix("brainsync://v3?url=https%3A%2F%2Fa.trycloudflare.com&token="), app)
        XCTAssertEqual(V3Link(url: link.appLink), link)
        XCTAssertEqual(V3Link(string: app), link)
        let noToken = try XCTUnwrap(V3Link(string: "brainsync://v3?url=http%3A%2F%2F10.0.0.2%3A8765"))
        XCTAssertNil(noToken.token)
        XCTAssertEqual(noToken.socketURL.absoluteString, "ws://10.0.0.2:8765/ws")
        XCTAssertEqual(V3Link(string: "brainsync:v3?url=http://10.0.0.2:8765&token=x&takeover=true")?.takeover, true)
    }

    func testRefusesOtherThings() {
        XCTAssertNil(V3Link(string: ""))
        XCTAssertNil(V3Link(string: "ftp://host/#token=x"))
        XCTAssertNil(V3Link(string: "brainsync://pair?url=http%3A%2F%2Fh%3A8000&key=k"))
        XCTAssertNil(V3Link(string: "brainsync://v3?token=x")) // no url
        XCTAssertNil(V3Link(string: "http://host:8000/?key=abc")) // the Voice server's link
        XCTAssertNil(V3Link(base: URL(string: "file:///tmp")!, token: "x"))
        XCTAssertNil(V3Link(base: URL(string: "http://h/")!, token: "  ")?.token)
    }

    func testPairScreenTellsTheTwoApart() throws {
        guard case .v3(let a) = try XCTUnwrap(V3AnyLink.parse("https://x.trycloudflare.com/#token=abc")) else {
            return XCTFail("a #token link is v3")
        }
        XCTAssertEqual(a.token, "abc")
        guard case .v3 = try XCTUnwrap(V3AnyLink.parse("brainsync://v3?url=http%3A%2F%2Fh%3A8765&token=t")) else {
            return XCTFail()
        }
        guard case .voice(let voice) = try XCTUnwrap(V3AnyLink.parse("brainsync://pair?url=http%3A%2F%2Fh%3A8000&key=k")) else {
            return XCTFail("a pair link is the Voice server's")
        }
        XCTAssertEqual(voice.key, "k")
        guard case .voice = try XCTUnwrap(V3AnyLink.parse("https://y.trycloudflare.com/?key=k2")) else {
            return XCTFail("a ?key= link is the Voice server's")
        }
        guard case .voice = try XCTUnwrap(V3AnyLink.parse(URL(string: "brainsync://pair?url=http%3A%2F%2Fh&key=k")!)) else {
            return XCTFail()
        }
        guard case .v3 = try XCTUnwrap(V3AnyLink.parse(URL(string: "brainsync://v3?url=http%3A%2F%2Fh&token=t")!)) else {
            return XCTFail()
        }
        XCTAssertNil(V3AnyLink.parse("brainsync://nothing"))
        XCTAssertNil(V3AnyLink.parse("   "))
    }
}
