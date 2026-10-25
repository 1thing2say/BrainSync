import AppKit
import BrainPointCore
import Foundation

/// What the user set, from UserDefaults, with launch arguments on top for this run only:
///
///     --server ws://127.0.0.1:8799/ws   the server's WebSocket URL
///     --token TOKEN                     the server token (prefer the settings window)
///     --allow-fake-control              let the SIMULATED board press (developers only)
///     --accept-warning                  skip the photosensitivity alert (developers only)
///     --verbose                         also print every log line to stderr
struct Settings: Equatable {
    static let defaultServer = "ws://127.0.0.1:8765/ws"

    var serverURL = Settings.defaultServer
    // TODO: keep the token in the Keychain; UserDefaults for now.
    var token = ""
    var allowFakeControl = false
    var denyList = AppModel.defaultDenyList

    private enum Key {
        static let server = "serverURL", token = "token", allowFake = "allowFakeControl", deny = "denyList"
    }

    static func load(_ d: UserDefaults = .standard) -> Settings {
        var s = Settings()
        if let v = d.string(forKey: Key.server), !v.isEmpty { s.serverURL = v }
        s.token = d.string(forKey: Key.token) ?? ""
        s.allowFakeControl = d.bool(forKey: Key.allowFake)
        if let v = d.stringArray(forKey: Key.deny) { s.denyList = v }
        return s
    }

    func save(_ d: UserDefaults = .standard) {
        d.set(serverURL, forKey: Key.server)
        d.set(token, forKey: Key.token)
        d.set(allowFakeControl, forKey: Key.allowFake)
        d.set(denyList, forKey: Key.deny)
    }

    /// A ws:// or wss:// URL, or nil.
    static func validURL(_ s: String) -> URL? {
        guard let u = URL(string: s.trimmingCharacters(in: .whitespaces)),
              ["ws", "wss"].contains(u.scheme?.lowercased() ?? ""), u.host != nil else { return nil }
        return u
    }

    /// The server's /api/speech URL for `text`, derived from the ws URL (ws -> http, wss -> https).
    static func speechURL(fromWS ws: URL, text: String) -> URL? {
        guard var c = URLComponents(url: ws, resolvingAgainstBaseURL: false) else { return nil }
        c.scheme = c.scheme?.lowercased() == "wss" ? "https" : "http"
        c.path = "/api/speech"
        c.queryItems = [URLQueryItem(name: "text", value: text)]
        return c.url
    }
}

struct LaunchOptions {
    var server: String?
    var token: String?
    var allowFakeControl = false
    var acceptWarning = false
    var verbose = false

    static func parse(_ args: [String] = CommandLine.arguments) -> LaunchOptions {
        var o = LaunchOptions()
        var i = 1
        while i < args.count {
            let a = args[i]
            func next() -> String? { i + 1 < args.count ? { i += 1; return args[i] }() : nil }
            switch a {
            case "--server": o.server = next()
            case "--token": o.token = next()
            case "--allow-fake-control": o.allowFakeControl = true
            case "--accept-warning": o.acceptWarning = true
            case "--verbose", "-v": o.verbose = true
            default:
                if a.hasPrefix("--server=") { o.server = String(a.dropFirst(9)) }
                else if a.hasPrefix("--token=") { o.token = String(a.dropFirst(8)) }
            }
            i += 1
        }
        return o
    }

    /// The arguments for the log, without the token.
    var summary: [String: Any] {
        ["server": server ?? NSNull(), "token_given": token != nil, "allow_fake_control": allowFakeControl,
         "accept_warning": acceptWarning, "verbose": verbose]
    }
}

/// The settings window: server URL, token, allow-fake-control (dev) and the deny list.
@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let urlField = NSTextField()
    private let tokenField = NSSecureTextField()
    private let fakeBox = NSButton(checkboxWithTitle: "Allow the SIMULATED board to press (developers only)", target: nil, action: nil)
    private let denyView = NSTextView()
    private let errorLabel = NSTextField(labelWithString: "")
    var onSave: ((Settings) -> Void)?

    func show(_ s: Settings) {
        if window == nil { build() }
        urlField.stringValue = s.serverURL
        tokenField.stringValue = s.token
        fakeBox.state = s.allowFakeControl ? .on : .off
        denyView.string = s.denyList.joined(separator: "\n")
        errorLabel.stringValue = ""
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    private func build() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 380),
                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "BrainPoint Settings"
        w.isReleasedWhenClosed = false
        w.delegate = self
        urlField.placeholderString = Settings.defaultServer
        tokenField.placeholderString = "empty for a server run with --no-auth"
        let scroll = NSScrollView()
        scroll.documentView = denyView
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        denyView.isRichText = false
        denyView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        denyView.autoresizingMask = [.width]
        denyView.isVerticallyResizable = true
        denyView.textContainer?.widthTracksTextView = true
        scroll.heightAnchor.constraint(equalToConstant: 120).isActive = true
        errorLabel.textColor = .systemRed
        let save = NSButton(title: "Save", target: self, action: #selector(saveClicked))
        save.keyEquivalent = "\r"
        let grid = NSGridView(views: [
            [NSTextField(labelWithString: "Server URL:"), urlField],
            [NSTextField(labelWithString: "Token:"), tokenField],
            [NSGridCell.emptyContentView, fakeBox],
            [NSTextField(labelWithString: "Deny list:"), scroll],
            [NSGridCell.emptyContentView, NSTextField(labelWithString: "One bundle id per line; BrainPoint never controls these apps.")],
            [NSGridCell.emptyContentView, errorLabel],
            [NSGridCell.emptyContentView, save],
        ])
        grid.column(at: 0).xPlacement = .trailing
        grid.rowSpacing = 10
        grid.translatesAutoresizingMaskIntoConstraints = false
        urlField.widthAnchor.constraint(greaterThanOrEqualToConstant: 360).isActive = true
        let content = NSView()
        content.addSubview(grid)
        NSLayoutConstraint.activate([
            grid.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            grid.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            grid.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            grid.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -20),
        ])
        w.contentView = content
        w.center()
        window = w
    }

    @objc private func saveClicked() {
        guard Settings.validURL(urlField.stringValue) != nil else {
            errorLabel.stringValue = "The server URL must start with ws:// or wss://, e.g. \(Settings.defaultServer)"
            return
        }
        var s = Settings()
        s.serverURL = urlField.stringValue.trimmingCharacters(in: .whitespaces)
        s.token = tokenField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        s.allowFakeControl = fakeBox.state == .on
        s.denyList = denyView.string.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        onSave?(s)
        window?.close()
    }
}
