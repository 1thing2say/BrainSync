import AVFoundation
import Foundation

/// Speaks short announcements with the server's voice: GET /api/speech?text=… (a WAV from
/// Cartesia, the key stays on the server), played with AVAudioPlayer. A 503 means the server
/// has no voice, and a 404 an older server without the route: both are silent, noted once.
@MainActor
final class Speaker: NSObject, AVAudioPlayerDelegate {
    var serverURL: URL?
    /// The server token, sent as `Authorization: Bearer` (v3.1: /api/speech needs it with auth on).
    var token: String?
    var log: ((String, [String: Any]) -> Void)?
    private var player: AVAudioPlayer?
    private var queue: [String] = []
    private var busy = false
    private var silentNoted = false

    func say(_ text: String) {
        let t = String(text.prefix(300))
        guard !t.isEmpty else { return }
        queue.append(t)
        if queue.count > 3 { queue.removeFirst(queue.count - 3) }
        next()
    }

    private func next() {
        guard !busy, !queue.isEmpty, let ws = serverURL else { return }
        let text = queue.removeFirst()
        guard let url = Settings.speechURL(fromWS: ws, text: text) else { return }
        busy = true
        var req = URLRequest(url: url)
        req.timeoutInterval = 10
        if let token, !token.isEmpty { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        URLSession.shared.dataTask(with: req) { data, resp, err in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            let errText = err?.localizedDescription
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self.fetched(text: text, data: data, code: code, error: errText) }
            }
        }.resume()
    }

    private func fetched(text: String, data: Data?, code: Int, error: String?) {
        defer { if player == nil { busy = false; next() } }
        if code == 200, let data, let p = try? AVAudioPlayer(data: data) {
            p.delegate = self
            player = p
            p.play()
            log?("speak", ["text": text, "bytes": data.count])
            return
        }
        if code == 503 || code == 404 {
            if !silentNoted { log?("speak_silent", ["text": text, "http": code, "why": code == 503 ? "the server has no voice" : "no /api/speech on this server"]) }
            silentNoted = true
        } else {
            log?("speak_failed", ["text": text, "http": code, "error": error ?? ""])
        }
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async {
            MainActor.assumeIsolated {
                self.player = nil
                self.busy = false
                self.next()
            }
        }
    }
}
