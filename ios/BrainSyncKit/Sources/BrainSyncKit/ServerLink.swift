import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Where the BrainSync server is and the access key it wants (app/remote.py: `?key=K` on every
/// URL, close code 4403 / HTTP 403 without it).
///
/// Two ways in:
/// - the pair link `brainsync://pair?url=<percent-encoded base>&key=<key>`;
/// - the web phone link pasted as text, `<base>/?key=<key>`.
public struct ServerLink: Equatable, Codable {
    /// `scheme://host[:port]`, no path, no trailing slash (http or https only).
    public let base: URL
    public let key: String

    /// Characters left as they are inside a query value; everything else is percent-encoded
    /// (so `+`, `&`, `=` and `/` in a key or a URL survive the round trip).
    static let queryValueAllowed: CharacterSet = {
        var set = CharacterSet()
        set.insert(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
        return set
    }()

    static func encode(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: queryValueAllowed) ?? value
    }

    /// Builds a link from a base URL (any path, query or fragment is dropped) and a key.
    public init?(base: URL, key: String) {
        let key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty, let normalized = ServerLink.normalize(base) else { return nil }
        self.base = normalized
        self.key = key
    }

    /// `brainsync://pair?url=...&key=...`
    public init?(pairURL: URL) {
        guard pairURL.scheme?.lowercased() == "brainsync",
              let parts = URLComponents(url: pairURL, resolvingAgainstBaseURL: false) else { return nil }
        // brainsync://pair?... has host "pair"; tolerate brainsync:pair?... and brainsync:///pair?...
        let target = (parts.host ?? "").isEmpty ? parts.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
                                               : (parts.host ?? "")
        guard target.lowercased() == "pair" else { return nil }
        let items = parts.queryItems ?? []
        guard let urlText = items.first(where: { $0.name == "url" })?.value,
              let key = items.first(where: { $0.name == "key" })?.value,
              let base = URL(string: urlText.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
        self.init(base: base, key: key)
    }

    /// A pair link or a web phone link (`<base>/?key=<key>`), as typed or pasted: surrounding
    /// whitespace is trimmed; a missing `http://` is added.
    public init?(string: String) {
        var text = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if text.lowercased().hasPrefix("brainsync:") {
            guard let url = URL(string: text) else { return nil }
            self.init(pairURL: url)
            return
        }
        if !text.contains("://") { text = "http://" + text }
        guard let url = URL(string: text),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let key = parts.queryItems?.first(where: { $0.name == "key" })?.value else { return nil }
        self.init(base: url, key: key)
    }

    static func normalize(_ url: URL) -> URL? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = parts.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = parts.host, !host.isEmpty else { return nil }
        var out = URLComponents()
        out.scheme = scheme
        out.host = host
        out.port = parts.port
        return out.url
    }

    private var origin: (scheme: String, host: String, port: Int?) {
        let parts = URLComponents(url: base, resolvingAgainstBaseURL: false)
        return ((parts?.scheme ?? "").lowercased(), (parts?.host ?? "").lowercased(), parts?.port)
    }

    /// Same scheme, host and port; the key may differ (a restarted server has a new one). A
    /// quick tunnel gets a new trycloudflare host every start, so that counts as another server.
    public func sameServer(as other: ServerLink) -> Bool {
        origin == other.origin
    }

    /// `host[:port]`, for asking the partner before a link switches servers.
    public var hostLabel: String {
        let o = origin
        if o.host.isEmpty { return baseString }
        return o.port.map { "\(o.host):\($0)" } ?? o.host
    }

    private var baseString: String {
        var s = base.absoluteString
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    private var keyQuery: String { "key=" + ServerLink.encode(key) }

    /// `ws(s)://<host>/ws/ui?key=<key>&client=ios` (wss for an https base).
    public var uiSocketURL: URL {
        var s = baseString
        if s.hasPrefix("https://") {
            s = "wss://" + s.dropFirst("https://".count)
        } else if s.hasPrefix("http://") {
            s = "ws://" + s.dropFirst("http://".count)
        }
        return URL(string: s + "/ws/ui?" + keyQuery + "&client=ios")!
    }

    /// `<base>/api/config?key=<key>`
    public var configURL: URL {
        URL(string: baseString + "/api/config?" + keyQuery)!
    }

    /// `<base><path>?key=<key>` for a server-relative path such as `/audio/abc`.
    public func audioURL(_ path: String) -> URL {
        let clean = path.trimmingCharacters(in: .whitespacesAndNewlines)
        let rel = clean.hasPrefix("/") ? clean : "/" + clean
        let sep = rel.contains("?") ? "&" : "?"
        return URL(string: baseString + rel + sep + keyQuery)
            ?? URL(string: baseString + "/" + ServerLink.encode(rel) + sep + keyQuery)!
    }

    /// `brainsync://pair?url=<base>&key=<key>`
    public var pairLink: URL {
        URL(string: "brainsync://pair?url=" + ServerLink.encode(baseString) + "&" + keyQuery)!
    }
}
