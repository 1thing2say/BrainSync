import Foundation

/// Decides whether BrainPoint may flicker and press, from everything it knows.
///
/// **armed** needs all of these, checked in this order:
/// 1. connected to the server, and the controller;
/// 2. a real board, or `allowFakeControl` (the SIMULATED board otherwise gives **dryRun**);
/// 3. board "streaming" and ok, streaming for at least 2 s;
/// 4. Accessibility trusted, warning accepted, no user pause, no safety pause, front app not
///    denied;
/// 5. phase "run".
/// A simulated board without `allowFakeControl` that passes every other condition is **dryRun**:
/// it may flicker but never press. Phase "calibrate" with conditions 1, 3 and 4 met is
/// **calibrating** (still targets). A user or safety pause, or the server's phase "pause", is
/// **paused**; anything else is **off**. The reason is one sentence about the first condition
/// that failed.
///
/// The server's phase "pause" with everything else armed is the server holding the flicker while
/// a click is in flight: point mode sends it just *before* the `click` (so the flicker stops
/// before the page changes), so it blocks the flicker but not that press. User and safety pauses
/// are local (conditions 4) and block both.
public enum ControlGate {
    public static let minStreamingS = 2.0

    public static func decide(_ i: GateInputs, now: Double) -> GateDecision {
        let headset = headsetLine(i, now: now)
        func off(_ reason: String) -> GateDecision {
            GateDecision(state: .off, mayFlicker: false, mayPress: false, reason: reason, headsetLine: headset)
        }
        func paused(_ reason: String) -> GateDecision {
            GateDecision(state: .paused, mayFlicker: false, mayPress: false, reason: reason, headsetLine: headset)
        }

        // 1. connection and role
        if !i.serverConnected { return off("Not connected to the BrainSync server.") }
        if !i.isController {
            return off("Connected as a viewer: another client controls the headset.")
        }
        // 2. the fake-board rule is applied last (dry run), so the reason names a real problem first.
        let dryRun = i.fakeBoard && !i.allowFakeControl
        // 3. board
        if i.boardState != "streaming" {
            return off("The headset is not streaming (board \(i.boardState)).")
        }
        if !i.boardOk { return off("The headset is streaming but the server reports a problem.") }
        let streamed = i.streamingSince.map { now - $0 } ?? 0
        if streamed < minStreamingS {
            return off(String(format: "The headset just started streaming; waiting %.0f s for it to settle.",
                              minStreamingS))
        }
        // 4. local permissions and pauses
        if !i.axTrusted {
            return off("Accessibility permission is not granted to BrainPoint.")
        }
        if !i.warningAccepted { return off("The photosensitivity warning has not been accepted.") }
        if let s = i.safetyPause { return paused("Paused for safety: \(s).") }
        if i.userPaused { return paused("Paused; choose Resume in the menu to continue.") }
        if let app = i.frontAppDenied {
            return off("\(app) is on the deny list, so BrainPoint does not control it.")
        }
        // 5. phase
        switch i.phase {
        case "run":
            if dryRun {
                return GateDecision(state: .dryRun, mayFlicker: true, mayPress: false,
                                    reason: "SIMULATED board: dry run, picks are shown but nothing is pressed.",
                                    headsetLine: headset, dryRun: true)
            }
            return GateDecision(state: .armed, mayFlicker: true, mayPress: true,
                                reason: "Armed: a pick presses the element in the front app.",
                                headsetLine: headset)
        case "calibrate":
            return GateDecision(state: .calibrating, mayFlicker: false, mayPress: false,
                                reason: "Calibrating: look at the screen and relax.", headsetLine: headset)
        case "pause":
            // a click in flight: no flicker, but the press that follows may go through
            return GateDecision(state: .paused, mayFlicker: false, mayPress: !dryRun,
                                reason: "The server paused the session" + note(i.serverNote, fallback: " (a click is in flight)") + ".",
                                headsetLine: headset, dryRun: dryRun)
        case "stop":
            return off("The server is shutting down" + note(i.serverNote) + ".")
        default:
            return off("The server is waiting (phase \(i.phase))" + note(i.serverNote) + ".")
        }
    }

    private static func note(_ s: String?, fallback: String = "") -> String {
        guard let s, !s.isEmpty else { return fallback }
        var t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        while t.hasSuffix(".") { t.removeLast() }
        return ": " + t
    }

    /// One short line about the headset for the menu, strip and logs.
    public static func headsetLine(_ i: GateInputs, now: Double) -> String {
        if !i.serverConnected { return "Headset unknown (no server)" }
        let prefix = i.fakeBoard ? "SIMULATED board" : "Headset"
        switch i.boardState {
        case "streaming":
            if !i.boardOk { return "\(prefix) streaming, signal problem" }
            if let since = i.streamingSince, now - since < minStreamingS { return "\(prefix) streaming (settling)" }
            return i.fakeBoard ? "SIMULATED board" : "Headset streaming"
        case "connecting": return "\(prefix) connecting"
        case "no-data": return "\(prefix) disconnected (no data)"
        case "error": return "\(prefix) error"
        default: return "\(prefix) state unknown"
        }
    }
}
