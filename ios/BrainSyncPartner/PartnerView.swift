// The partner screen, laid out like the web page's phone layout (ui/style.css, max-width 600px):
// the banner, the partner's part (mic, conversation, typing), the mirror, the operator footer,
// and Stop pinned at the bottom where it is always visible.

import SwiftUI
import BrainSyncKit

struct PartnerView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 0) {
            BannerBar()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let reason = model.stopReason {
                        StoppedCard(reason: reason)
                    }
                    MicCardView()
                    ConversationView()
                    TypedInput()
                    MirrorView()
                    OperatorFooter()
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .background(Palette.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ControlBar()
        }
        .foregroundStyle(Palette.text)
    }
}

/// The coloured status line over everything (ui/view_core.js banner), or the connection state.
struct BannerBar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let banner = model.banner
        let text = model.connected ? banner.text : model.connectionNote
        let color = model.connected ? banner.tone.color : (model.stopReason == nil ? Tone.warn.color : Tone.stop.color)
        HStack(spacing: 8) {
            Text(text.isEmpty ? "Connecting…" : text)
                .font(.headline)
                .lineLimit(3)
                .accessibilityAddTraits(.updatesFrequently)
            Spacer(minLength: 4)
            ForEach(model.health?.flags ?? [], id: \.self) { flag in
                Text(flag)
                    .font(.caption2.weight(.bold))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .background(Palette.warn.opacity(0.9), in: Capsule())
                    .foregroundStyle(Color.black)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color)
        .foregroundStyle(Color.white)
    }
}

/// Another partner page took over, or the link's key no longer works.
struct StoppedCard: View {
    @Environment(AppModel.self) private var model
    let reason: UIConnection.StopReason

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            switch reason {
            case .replaced:
                Text("Replaced by another partner page").font(.headline)
                Text("The partner page was opened in a browser or on another phone, which now runs the session. Only one partner page can be connected at a time.")
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
                Button("Reconnect (use this phone instead)") { model.connect() }
                    .buttonStyle(.borderedProminent)
            case .badKey:
                Text("Link expired — pair again").font(.headline)
                Text("The server refused this phone's access key. It changes every time the server starts: open the new app link from the laptop's page.")
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
                Button("Pair") { model.forgetLink() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Palette.danger.opacity(0.18), in: RoundedRectangle(cornerRadius: 14))
    }
}

/// The partner's mic: on by itself during the session; their words show as they talk.
struct MicCardView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let card = MicCard.make(state: model.listener.state, heard: model.listener.heard, muted: model.micMuted)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle()
                    .fill(card.tone == .look ? Palette.say : (card.tone == .warn ? Palette.warn : Palette.muted))
                    .frame(width: 10, height: 10)
                Text(card.title).font(.headline)
            }
            if !card.heard.isEmpty {
                Text("“\(card.heard)”")
                    .font(.title3)
                    .foregroundStyle(model.listener.state == "sent" ? Palette.text : Palette.muted)
            }
            if !card.detail.isEmpty {
                Text(card.detail)
                    .font(.footnote)
                    .foregroundStyle(Palette.muted)
            }
            HStack(spacing: 10) {
                Button(model.micMuted ? "Unmute" : "Mute") { model.toggleMute() }
                    .buttonStyle(.bordered)
                    .tint(model.micMuted ? Palette.warn : Palette.muted)
                    .accessibilityValue(model.micMuted ? "muted" : "on")
                Button(model.sttMode == "always" ? "Restart mic" : "Listen") { model.restartMic() }
                    .buttonStyle(.bordered)
            }
            .disabled(!model.connected)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Palette.panel, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(card.tone.color.opacity(0.8), lineWidth: 1))
    }
}

/// Partner lines and the user's spoken lines (under the user's first name).
struct ConversationView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Conversation")
                .font(.caption.weight(.semibold))
                .textCase(.uppercase)
                .foregroundStyle(Palette.muted)
            if model.lines.isEmpty {
                Text("Nothing said yet.")
                    .font(.footnote)
                    .foregroundStyle(Palette.muted)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            ForEach(model.lines) { line in
                                LineRow(line: line, userName: model.userName).id(line.id)
                            }
                        }
                    }
                    .frame(maxHeight: 280)
                    .defaultScrollAnchor(.bottom)
                    .onChange(of: model.lines.count) { _, _ in
                        if let last = model.lines.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                        }
                    }
                }
            }
        }
    }
}

struct LineRow: View {
    let line: ConversationLine
    let userName: String

    var body: some View {
        VStack(alignment: line.fromPartner ? .leading : .trailing, spacing: 2) {
            Text(line.fromPartner ? "Partner" : userName)
                .font(.caption2.weight(.bold))
                .foregroundStyle(line.fromPartner ? Palette.next : Palette.say)
            Text(line.text)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(line.fromPartner ? Palette.surface : Palette.say.opacity(0.22),
                            in: RoundedRectangle(cornerRadius: 12))
        }
        .frame(maxWidth: .infinity, alignment: line.fromPartner ? .leading : .trailing)
    }
}

/// Typing always works, with or without the mic.
struct TypedInput: View {
    @Environment(AppModel.self) private var model
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 8) {
            TextField("Type what the partner says", text: $text)
                .textFieldStyle(.roundedBorder)
                .submitLabel(.send)
                .focused($focused)
                .onSubmit(send)
            Button("Send", action: send)
                .buttonStyle(.bordered)
                .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty || !model.connected)
        }
    }

    private func send() {
        model.sendTyped(text)
        text = ""
    }
}

/// For whoever runs the demo: one health line (details on a tap), Correct / Wrong, the server.
struct OperatorFooter: View {
    @Environment(AppModel.self) private var model
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let health = model.health {
                Button {
                    expanded.toggle()
                } label: {
                    HStack(spacing: 8) {
                        Circle().fill(color(health.level)).frame(width: 10, height: 10)
                        Text(health.text).font(.subheadline).multilineTextAlignment(.leading)
                        Spacer()
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .foregroundStyle(Palette.muted)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityHint("Shows the details")
                if expanded, let status = model.status {
                    StatusDetails(status: status)
                }
            }
            HStack(spacing: 10) {
                Text("Last reply:").font(.footnote).foregroundStyle(Palette.muted)
                Button("Correct") { model.judge(correct: true) }
                    .buttonStyle(.bordered)
                Button("Wrong") { model.judge(correct: false) }
                    .buttonStyle(.bordered)
                Text(model.judged).font(.footnote).foregroundStyle(Palette.muted)
            }
            .disabled(model.lastSpokenId == nil || !model.connected)
            if let link = model.link {
                HStack {
                    Text(link.base.host ?? link.base.absoluteString)
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button("Pair again") { model.forgetLink() }
                        .font(.caption)
                }
            }
        }
        .padding(14)
        .background(Palette.panel, in: RoundedRectangle(cornerRadius: 16))
    }

    private func color(_ level: Health.Level) -> Color {
        switch level {
        case .ok: return Palette.say
        case .warn: return Palette.warn
        case .bad: return Palette.danger
        }
    }
}

/// The status badges (ui/app.js renderStatus).
struct StatusDetails: View {
    let status: UIStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            FlowBadges(badges: badges)
            if !status.decoderName.isEmpty || !status.decoderMessage.isEmpty {
                Text([status.decoderName.isEmpty ? "" : "Decoder: \(status.decoderName)", status.decoderMessage]
                    .filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
            }
        }
    }

    private var badges: [(String, Bool?)] {
        var list: [(String, Bool?)] = []
        if !status.decoderConnected {
            list.append(("NO DECODER", false))
        } else {
            list.append((status.decoderOk ? "DECODER OK" : "DECODER NOT OK", status.decoderOk))
        }
        list.append((status.displayOk ? "DISPLAY OK" : "DISPLAY OFF", status.displayOk))
        list.append((status.boardOk ? "BOARD OK" : "BOARD ?", status.boardOk))
        switch String(describing: status.llm) {
        case "ok": list.append(("LLM OK", true))
        case "waiting": list.append(("LLM READY", nil))
        case "failed": list.append(("LLM FAILED", false))
        default: list.append(("LLM OFF", nil))
        }
        let mic = String(describing: status.mic)
        if status.micMuted {
            list.append(("MIC MUTED", nil))
        } else if mic == "listening" {
            list.append(("MIC ON", true))
        } else if ["denied", "nomic", "offline", "unsupported", "error", "needs_tap"].contains(mic) {
            list.append(("MIC: \(mic.uppercased())", false))
        }
        return list
    }
}

struct FlowBadges: View {
    let badges: [(String, Bool?)]

    var body: some View {
        // Two per row keeps them readable on a phone without a custom layout.
        let rows = stride(from: 0, to: badges.count, by: 2).map { Array(badges[$0..<min($0 + 2, badges.count)]) }
        VStack(alignment: .leading, spacing: 6) {
            ForEach(rows.indices, id: \.self) { row in
                HStack(spacing: 6) {
                    ForEach(rows[row].indices, id: \.self) { index in
                        let badge = rows[row][index]
                        Text(badge.0)
                            .font(.caption2.weight(.bold))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(tint(badge.1).opacity(0.2), in: Capsule())
                            .foregroundStyle(tint(badge.1))
                    }
                }
            }
        }
    }

    private func tint(_ ok: Bool?) -> Color {
        switch ok {
        case true?: return Palette.say
        case false?: return Palette.danger
        case nil: return Palette.muted
        }
    }
}

/// Stop (always visible, big and red) and Resume.
struct ControlBar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 10) {
            Button {
                model.stopPressed()
            } label: {
                Text(model.connected ? "Stop" : "Not connected: press Esc on the display")
                    .font(model.connected ? .title2.weight(.bold) : .subheadline.weight(.bold))
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.borderedProminent)
            .tint(Palette.danger)
            .accessibilityHint("Turns the flicker off and pauses the session")

            Button {
                model.resumePressed()
            } label: {
                Text("Resume")
                    .font(.headline)
                    .frame(minWidth: 90, minHeight: 56)
            }
            .buttonStyle(.bordered)
            .disabled(!model.resumeEnabled)
        }
        .padding(.horizontal, 14)
        .padding(.top, 10)
        .padding(.bottom, 6)
        .background(.ultraThinMaterial)
    }
}
