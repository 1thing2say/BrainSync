import Foundation

/// Where a PROTOCOL-v3 server (`python -m brainsync.server`, the Jetson) is, and its token.
///
/// The server and the Jetson's tunnel print the reference page's address with the token in the
/// fragment, which a browser never sends to a server (brainsync/server/app.py `browser_url`,
/// deploy/jetson/cloudflare/brainsync-tunnel.sh, `~/brainsync-link.txt`):
///
/// - `http://127.0.0.1:8765/#token=<token>` (local; `&takeover=1` may follow);
/// - `https://<name>.trycloudflare.com/#token=<token>` (the tunnel);
/// - the same without `#token=...` for a `--no-auth` server, or a tunnel started with
///   `LINK_WITH_TOKEN=0` (the token then comes separately).
///
/// The app's own form is `brainsync://v3?url=<percent-encoded base>&token=<token>`. The Voice
/// app's `ServerLink` (`brainsync://pair?...`, `<base>/?key=...`) is another server; `V3AnyLink`
/// tells the two apart.
public struct V3Link: Equatable, Codable {
    /// `scheme://host[:port]`, no path, no trailing slash (http or https only).
    public let base: URL
    /// nil for a server started with `--no-auth`.
    public let token: String?
    /// The link asked to replace the current controller (`takeover=1`).
    public let takeover: Bool

    /// Builds a link from a base URL (any path, query or fragment is dropped) and a token
    /// (blank counts as none).
    public init?(base: URL, token: String?, takeover: Bool = false) {
        guard let normalized = ServerLink.normalize(base) else { return nil }
        let trimmed = token?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.base = normalized
        self.token = (trimmed?.isEmpty ?? true) ? nil : trimmed
        self.takeover = takeover
    }

    /// `brainsync://v3?url=...&token=...[&takeover=1]`, or an http(s) link as printed.
    public init?(url: URL) {
        guard let scheme = url.scheme?.lowercased(),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        if scheme == "brainsync" {
            // brainsync://v3?... has host "v3"; tolerate brainsync:v3?... and brainsync:///v3?...
            let target = (parts.host ?? "").isEmpty
                ? parts.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) : (parts.host ?? "")
            guard target.lowercased() == "v3" else { return nil }
            let items = parts.queryItems ?? []
            guard let urlText = items.first(where: { $0.name == "url" })?.value,
                  let base = URL(string: urlText.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
            self.init(base: base, token: items.first(where: { $0.name == "token" })?.value,
                      takeover: V3Link.truthy(items.first(where: { $0.name == "takeover" })?.value))
            return
        }
        guard scheme == "http" || scheme == "https" else { return nil }
        // A Voice server link carries ?key=...; that is ServerLink's, not ours.
        if parts.queryItems?.contains(where: { $0.name == "key" }) == true { return nil }
        let fragment = V3Link.fragmentItems(parts.percentEncodedFragment)
        self.init(base: url, token: fragment.first(where: { $0.name == "token" })?.value,
                  takeover: V3Link.truthy(fragment.first(where: { $0.name == "takeover" })?.value))
    }

    /// A link as typed or pasted: whitespace is trimmed and a missing `http://` is added
    /// (`192.168.1.20:8765`).
    public init?(string: String) {
        var text = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.lowercased().hasPrefix("brainsync:") && !text.contains("://") { text = "http://" + text }
        guard let url = URL(string: text) else { return nil }
        self.init(url: url)
    }

    /// `token=abc&takeover=1` from a fragment, percent-decoded.
    static func fragmentItems(_ fragment: String?) -> [URLQueryItem] {
        guard let fragment = fragment, !fragment.isEmpty else { return [] }
        var parts = URLComponents()
        parts.percentEncodedQuery = fragment
        return parts.queryItems ?? []
    }

    static func truthy(_ value: String?) -> Bool {
        guard let v = value?.lowercased() else { return false }
        return v == "1" || v == "true" || v == "yes"
    }

    private var baseString: String {
        var s = base.absoluteString
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    /// `ws(s)://<host[:port]>/ws` (wss for an https base). The token goes in `hello`, never in
    /// the URL.
    public var socketURL: URL {
        var s = baseString
        if s.hasPrefix("https://") {
            s = "wss://" + s.dropFirst("https://".count)
        } else if s.hasPrefix("http://") {
            s = "ws://" + s.dropFirst("http://".count)
        }
        return URL(string: s + "/ws")!
    }

    /// `<base>/health`: `{"ok", "board", "controller", "viewers"}`, no token needed.
    public var healthURL: URL {
        URL(string: baseString + "/health")!
    }

    /// The reference page, `<base>/#token=<token>`, as the server prints it.
    public var pageURL: URL {
        URL(string: baseString + "/" + (token.map { "#token=" + ServerLink.encode($0) } ?? ""))!
    }

    /// `brainsync://v3?url=<base>&token=<token>`.
    public var appLink: URL {
        var s = "brainsync://v3?url=" + ServerLink.encode(baseString)
        if let token = token { s += "&token=" + ServerLink.encode(token) }
        if takeover { s += "&takeover=1" }
        return URL(string: s)!
    }

    /// `host[:port]`, for showing which server the phone is paired with.
    public var hostLabel: String {
        let parts = URLComponents(url: base, resolvingAgainstBaseURL: false)
        let host = parts?.host ?? baseString
        return parts?.port.map { "\(host):\($0)" } ?? host
    }
}

/// A link the pair screen accepts: a v3 server (`V3Link`) or the Voice app's server
/// (`ServerLink`).
public enum V3AnyLink: Equatable {
    case v3(V3Link)
    case voice(ServerLink)

    /// `brainsync://pair?...` and `<base>/?key=...` are the Voice server's; `brainsync://v3?...`,
    /// `<base>/#token=...` and a bare `<base>` (a `--no-auth` v3 server) are v3.
    public static func parse(_ text: String) -> V3AnyLink? {
        if let voice = ServerLink(string: text) { return .voice(voice) }
        if let v3 = V3Link(string: text) { return .v3(v3) }
        return nil
    }

    public static func parse(_ url: URL) -> V3AnyLink? {
        if let voice = ServerLink(pairURL: url) { return .voice(voice) }
        return parse(url.absoluteString)
    }
}
