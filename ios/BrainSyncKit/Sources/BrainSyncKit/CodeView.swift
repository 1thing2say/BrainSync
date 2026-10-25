import Foundation

// c-VEP on the partner's mirror, as pure values (ui/view_core.js confidence() and training()):
// the bar under each square and the training card. Pure, so the package tests cover it.

/// One square's confidence bar: the correlation clipped to 0...1 (a steady fill), its signed
/// score as text, and whether it is this decision's best and the committed slot.
public struct ConfidenceBar: Equatable {
    public let fraction: Double
    public let text: String
    public let best: Bool
    public let committed: Bool

    public init(fraction: Double, text: String, best: Bool, committed: Bool) {
        self.fraction = fraction
        self.text = text
        self.best = best
        self.committed = committed
    }

    /// Every slot's bar for one `confidence` message.
    public static func make(_ msg: Confidence) -> [String: ConfidenceBar] {
        var out: [String: ConfidenceBar] = [:]
        for (slot, raw) in msg.scores {
            let v = raw.isFinite ? raw : 0
            let text = (v < 0 ? "-" : "+") + String(format: "%.2f", abs(v))
            out[slot] = ConfidenceBar(fraction: Swift.min(1, Swift.max(0, v)), text: text,
                                      best: slot == msg.raw, committed: slot == msg.committed)
        }
        return out
    }
}

/// The training card: a title, a detail line, how full its bar is, its tone ("look", "ok",
/// "bad") and whether the training is over.
public struct TrainingCard: Equatable {
    public let title: String
    public let detail: String
    public let fraction: Double
    public let tone: String
    public let done: Bool

    public init(title: String, detail: String, fraction: Double, tone: String, done: Bool) {
        self.title = title
        self.detail = detail
        self.fraction = fraction
        self.tone = tone
        self.done = done
    }

    public static func make(_ msg: Training) -> TrainingCard {
        let of = msg.of > 0 ? msg.of : 1
        let fraction = Swift.min(1, Swift.max(0, Double(msg.cycle) / Double(of)))
        if let accuracy = msg.accuracy {
            let chance = msg.chance.map { " (chance \(Int(($0 * 100).rounded())) %)" } ?? ""
            return TrainingCard(title: "Template ready",
                                detail: "\(Int((accuracy * 100).rounded())) % on held-out cycles\(chance)",
                                fraction: 1, tone: "ok", done: true)
        }
        if !msg.message.isEmpty {
            return TrainingCard(title: "Training failed", detail: msg.message, fraction: fraction,
                                tone: "bad", done: true)
        }
        let place = msg.label.isEmpty ? "the first square" : "the \(msg.label) square"
        return TrainingCard(title: "Training: look at \(place)", detail: "cycle \(msg.cycle) of \(of)",
                            fraction: fraction, tone: "look", done: false)
    }
}

/// Where a slot sits on the user's screen, as a chip on its card: c-VEP's row (Left, Middle,
/// Right) or f-VEP's diamond (Top, Left, Right, Bottom).
public enum ScreenPlace {
    public static func name(_ slot: String, layout: String, slots: [String]) -> String {
        if layout == "cvep" {
            guard let i = slots.firstIndex(of: slot) else { return "" }
            switch (i, slots.count) {
            case (0, _): return "Left"
            case (let k, let n) where k == n - 1: return "Right"
            case (1, 3): return "Middle"
            default: return "Square \(i + 1)"
            }
        }
        switch slot {
        case "s1": return "Top"
        case "s2": return "Left"
        case "s3": return "Right"
        case "s4": return "Bottom"
        default: return ""
        }
    }
}
