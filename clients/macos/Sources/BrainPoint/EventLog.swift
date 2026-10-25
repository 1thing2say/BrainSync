import Foundation

/// The verbose log: one JSON object per line in ~/Library/Logs/BrainPoint/brainpoint-YYYYMMDD.jsonl,
/// `{"t": unix seconds, "kind": ..., ...fields}`. The token is never passed in.
@MainActor
final class EventLog {
    let dir: URL
    var echo = false
    private var day = ""
    private var handle: FileHandle?
    private let dayFormat: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "yyyyMMdd"; f.locale = Locale(identifier: "en_US_POSIX"); return f
    }()

    init() {
        dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/BrainPoint", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    var currentFile: URL { dir.appendingPathComponent("brainpoint-\(dayFormat.string(from: Date())).jsonl") }

    func write(_ kind: String, _ fields: [String: Any] = [:]) {
        var obj = fields
        obj["t"] = (Date().timeIntervalSince1970 * 1000).rounded() / 1000
        obj["kind"] = kind
        guard JSONSerialization.isValidJSONObject(obj),
              var data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes])
        else { return }
        data.append(0x0A)
        if echo { FileHandle.standardError.write(data) }
        let today = dayFormat.string(from: Date())
        if today != day || handle == nil {
            try? handle?.close()
            day = today
            let url = currentFile
            if !FileManager.default.fileExists(atPath: url.path) {
                FileManager.default.createFile(atPath: url.path, contents: nil)
            }
            handle = try? FileHandle(forWritingTo: url)
            _ = try? handle?.seekToEnd()
        }
        try? handle?.write(contentsOf: data)
    }

    func close() { try? handle?.close(); handle = nil }
}
