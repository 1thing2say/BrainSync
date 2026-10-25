import XCTest
@testable import BrainSyncKit

final class ServerLinkTests: XCTestCase {
    func testPairLink() throws {
        let url = URL(string: "brainsync://pair?url=https%3A%2F%2Fabc.trycloudflare.com&key=K123")!
        let link = try XCTUnwrap(ServerLink(pairURL: url))
        XCTAssertEqual(link.base.absoluteString, "https://abc.trycloudflare.com")
        XCTAssertEqual(link.key, "K123")
        XCTAssertEqual(link.uiSocketURL.absoluteString, "wss://abc.trycloudflare.com/ws/ui?key=K123&client=ios")
        XCTAssertEqual(link.configURL.absoluteString, "https://abc.trycloudflare.com/api/config?key=K123")
    }

    func testPairLinkLanWithPort() throws {
        let url = URL(string: "brainsync://pair?url=http%3A%2F%2F192.168.1.5%3A8000&key=abc")!
        let link = try XCTUnwrap(ServerLink(pairURL: url))
        XCTAssertEqual(link.base.absoluteString, "http://192.168.1.5:8000")
        XCTAssertEqual(link.uiSocketURL.absoluteString, "ws://192.168.1.5:8000/ws/ui?key=abc&client=ios")
    }

    func testPairLinkRoundTrip() throws {
        let link = try XCTUnwrap(ServerLink(string: "http://192.168.1.5:8000/?key=a+b&c=d/e"))
        // "a+b" only: "&c=d/e" is another query item of the web link
        XCTAssertEqual(link.key, "a+b")
        XCTAssertEqual(ServerLink(pairURL: link.pairLink), link)
        XCTAssertEqual(ServerLink(string: link.pairLink.absoluteString), link)
        XCTAssertTrue(link.pairLink.absoluteString.hasPrefix("brainsync://pair?url=http%3A%2F%2F192.168.1.5%3A8000&key="))
    }

    func testWebPhoneLink() throws {
        let link = try XCTUnwrap(ServerLink(string: "https://abc.trycloudflare.com/?key=XYZ"))
        XCTAssertEqual(link.base.absoluteString, "https://abc.trycloudflare.com")
        XCTAssertEqual(link.key, "XYZ")
        let noSlash = try XCTUnwrap(ServerLink(string: "https://abc.trycloudflare.com?key=XYZ"))
        XCTAssertEqual(noSlash, link)
    }

    func testWhitespaceTrimmed() throws {
        let link = try XCTUnwrap(ServerLink(string: "  \n https://abc.trycloudflare.com/?key=XYZ \t\n"))
        XCTAssertEqual(link.key, "XYZ")
        let pair = try XCTUnwrap(ServerLink(string: " brainsync://pair?url=http%3A%2F%2F10.0.0.2%3A8000&key=k \n"))
        XCTAssertEqual(pair.base.absoluteString, "http://10.0.0.2:8000")
    }

    func testMissingSchemeIsHttp() throws {
        let link = try XCTUnwrap(ServerLink(string: "192.168.1.5:8000/?key=k"))
        XCTAssertEqual(link.base.absoluteString, "http://192.168.1.5:8000")
    }

    func testWsVsWss() throws {
        let http = try XCTUnwrap(ServerLink(base: URL(string: "http://host:8000")!, key: "k"))
        let https = try XCTUnwrap(ServerLink(base: URL(string: "https://host")!, key: "k"))
        XCTAssertEqual(http.uiSocketURL.scheme, "ws")
        XCTAssertEqual(https.uiSocketURL.scheme, "wss")
        XCTAssertEqual(https.uiSocketURL.path, "/ws/ui")
    }

    func testKeyPercentEncoding() throws {
        let link = try XCTUnwrap(ServerLink(base: URL(string: "https://host")!, key: "a+b&c=d/e f"))
        let s = link.uiSocketURL.absoluteString
        XCTAssertEqual(s, "wss://host/ws/ui?key=a%2Bb%26c%3Dd%2Fe%20f&client=ios")
        let items = URLComponents(url: link.uiSocketURL, resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(items?.first(where: { $0.name == "key" })?.value, "a+b&c=d/e f")
        XCTAssertEqual(ServerLink(pairURL: link.pairLink), link)
    }

    func testAudioURL() throws {
        let link = try XCTUnwrap(ServerLink(base: URL(string: "https://host/")!, key: "k"))
        XCTAssertEqual(link.audioURL("/audio/abc").absoluteString, "https://host/audio/abc?key=k")
        XCTAssertEqual(link.audioURL("audio/abc").absoluteString, "https://host/audio/abc?key=k")
        XCTAssertEqual(link.audioURL("/audio/abc?v=2").absoluteString, "https://host/audio/abc?v=2&key=k")
    }

    func testBasePathDropped() throws {
        let link = try XCTUnwrap(ServerLink(string: "https://host/index.html?key=k#x"))
        XCTAssertEqual(link.base.absoluteString, "https://host")
    }

    func testGarbageIsNil() {
        XCTAssertNil(ServerLink(string: ""))
        XCTAssertNil(ServerLink(string: "   "))
        XCTAssertNil(ServerLink(string: "hello world"))
        XCTAssertNil(ServerLink(string: "https://host/"))            // no key
        XCTAssertNil(ServerLink(string: "https://host/?key="))       // empty key
        XCTAssertNil(ServerLink(string: "ftp://host/?key=k"))
        XCTAssertNil(ServerLink(string: "brainsync://pair?key=k"))   // no url
        XCTAssertNil(ServerLink(string: "brainsync://pair?url=https%3A%2F%2Fhost"))  // no key
        XCTAssertNil(ServerLink(string: "brainsync://other?url=https%3A%2F%2Fhost&key=k"))
        XCTAssertNil(ServerLink(pairURL: URL(string: "https://host/?key=k")!))
        XCTAssertNil(ServerLink(pairURL: URL(string: "brainsync://pair?url=notaurl&key=k")!))
    }

    func testSameServer() throws {
        let link = try XCTUnwrap(ServerLink(string: "https://abc.trycloudflare.com/?key=K1"))
        // A new key from the same server: the app re-pairs without asking.
        XCTAssertTrue(link.sameServer(as: try XCTUnwrap(ServerLink(string: "https://ABC.trycloudflare.com/?key=K2"))))
        // Another host (a restarted tunnel, or a stranger's link), port or scheme: ask first.
        XCTAssertFalse(link.sameServer(as: try XCTUnwrap(ServerLink(string: "https://xyz.trycloudflare.com/?key=K1"))))
        XCTAssertFalse(link.sameServer(as: try XCTUnwrap(ServerLink(string: "https://abc.trycloudflare.com:8443/?key=K1"))))
        XCTAssertFalse(link.sameServer(as: try XCTUnwrap(ServerLink(string: "http://abc.trycloudflare.com/?key=K1"))))
        let lan = try XCTUnwrap(ServerLink(string: "http://192.168.1.5:8000/?key=k"))
        XCTAssertTrue(lan.sameServer(as: try XCTUnwrap(ServerLink(string: "brainsync://pair?url=http%3A%2F%2F192.168.1.5%3A8000&key=k2"))))
        XCTAssertFalse(lan.sameServer(as: try XCTUnwrap(ServerLink(string: "http://192.168.1.5:9000/?key=k"))))
    }

    func testHostLabel() throws {
        XCTAssertEqual(try XCTUnwrap(ServerLink(string: "https://abc.trycloudflare.com/?key=k")).hostLabel,
                       "abc.trycloudflare.com")
        XCTAssertEqual(try XCTUnwrap(ServerLink(string: "http://192.168.1.5:8000/?key=k")).hostLabel,
                       "192.168.1.5:8000")
    }

    func testCodable() throws {
        let link = try XCTUnwrap(ServerLink(string: "https://host/?key=k"))
        let data = try JSONEncoder().encode(link)
        XCTAssertEqual(try JSONDecoder().decode(ServerLink.self, from: data), link)
    }
}
