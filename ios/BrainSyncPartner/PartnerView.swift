// The partner screen, phrase first like Google's Look to Speak: a slim status bar (with the
// Settings gear), what the partner said last, the replies on the user's screen as big cards,
// then the mic, the conversation, typing and the operator footer. Stop stays pinned at the
// bottom where it is always visible.

import SwiftUI
import BrainSyncKit

struct PartnerView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hapticPick") private var hapticPick = true
    @AppStorage("hapticSpeak") private var hapticSpeak = true
    @State private var showSettings = false

    var body: some View {
        VStack(spacing: 0) {
            BannerBar { showSettings = true }
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let reason = model.stopReason {
                        StoppedCard(reason: reason)
                    }
                    if let said = model.lastPartnerLine {
                        PartnerSaidCard(text: said)
                    }
                    MirrorView()
                    MicCardView()
                    ConversationView()
                    TypedInput()
                    OperatorFooter()
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .background(Palette.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ControlBar()
        }
        .foregroundStyle(Palette.text)
        .sheet(isPresented: $showSettings) {
            SettingsView()
                .environment(model)
        }
        // A tap on the wrist for whoever holds the phone: a pick, and a reply starting.
        .sensoryFeedback(.selection, trigger: model.pickedSlot) { old, new in
            hapticPick && new != nil && new != old
        }
        .sensoryFeedback(.success, trigger: model.stateName) { old, new in
            hapticSpeak && new == "SPEAKING" && old != "SPEAKING"
        }
    }
}

/// A slim status line over everything (ui/view_core.js banner), or the connection state: the
/// tone colours its left edge and its text. The gear opens Settings.
struct BannerBar: View {
    @Environment(AppModel.self) private var model
    let onSettings: () -> Void

    var body: some View {
        let banner = model.banner
        let text = model.connected ? banner.text : model.connectionNote
        let tone = model.connected ? banner.tone : (model.stopReason == nil ? Tone.warn : Tone.stop)
        HStack(spacing: 10) {
            Text(text.isEmpty ? "Connecting…" : text)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(tone == .idle ? Palette.muted : tone.color)
                .lineLimit(2)
                .accessibilityAddTraits(.updatesFrequently)
            Spacer(minLength: 4)
            ForEach(model.health?.flags ?? [], id: \.self) { flag in
                MonoLabel(flag, color: Palette.warn)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Palette.warn.opacity(0.6), lineWidth: 1))
            }
            Button(action: onSettings) {
                Image(systemName: "gearshape")
                    .font(.body)
                    .foregroundStyle(Palette.muted)
                    .frame(width: 36, height: 36)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Settings")
        }
        .padding(.leading, 16)
        .padding(.trailing, 8)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.panel)
        .overlay(alignment: .leading) {
            Rectangle().fill(tone.color).frame(width: 3)
        }
        .overlay(alignment: .bottom) {
            Rectangle().fill(Palette.line).frame(height: 1)
        }
    }
}

/// What the partner said last, big, so the replies below read as answers to it.
struct PartnerSaidCard: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            MonoLabel("Partner said", color: Palette.next)
            Text(text)
                .font(.title3.weight(.semibold))
                .tracking(-0.3)
                .foregroundStyle(Palette.text)
                .fixedSize(horizontal: false, vertical: true)
        }
        .card()
        .accessibilityElement(children: .combine)
    }
}

/// Another partner page took over, or the link's key no longer works.
struct StoppedCard: View {
    @Environment(AppModel.self) private var model
    let reason: UIConnection.StopReason

    var body: some View {
        Callout(title: reason == .replaced ? "Not connected" : "Link expired",
                icon: "exclamationmark.octagon.fill", tint: Palette.danger) {
            switch reason {
            case .replaced:
                Text("Replaced by another partner page").font(.headline)
                Text("The partner page was opened in a browser or on another phone, which now runs the session. Only one partner page can be connected at a time.")
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
                Button("Reconnect (use this phone instead)") { model.connect() }
                    .buttonStyle(FilledButtonStyle(.primary))
            case .badKey:
                Text("Link expired — pair again").font(.headline)
                Text("The server refused this phone's access key. It changes every time the server starts: open the new app link from the laptop's page.")
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
                Button("Pair") { model.forgetLink() }
                    .buttonStyle(FilledButtonStyle(.primary))
            }
        }
    }
}

/// The partner's mic, compact: on by itself during the session; their words show as they talk.
struct MicCardView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let card = MicCard.make(state: model.listener.state, heard: model.listener.heard, muted: model.micMuted)
        let listening = card.tone == .look
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle()
                    .fill(listening ? Palette.say : (card.tone == .warn ? Palette.warn : Palette.muted))
                    .frame(width: 8, height: 8)
                Text(card.title)
                    .font(.subheadline.weight(.semibold))
                Spacer(minLength: 4)
                Button(model.micMuted ? "Unmute" : "Mute") { model.toggleMute() }
                    .buttonStyle(FilledButtonStyle(.secondary))
                    .accessibilityValue(model.micMuted ? "muted" : "on")
                Button(model.sttMode == "always" ? "Restart mic" : "Listen") { model.restartMic() }
                    .buttonStyle(FilledButtonStyle(.secondary))
            }
            .disabled(!model.connected)
            if !card.heard.isEmpty {
                Text("“\(card.heard)”")
                    .font(.body)
                    .foregroundStyle(model.listener.state == "sent" ? Palette.text : Palette.muted)
            }
            if !card.detail.isEmpty {
                Text(card.detail)
                    .font(.footnote)
                    .foregroundStyle(Palette.muted)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.panel, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius)
            .strokeBorder(listening ? Palette.say.opacity(0.5) : (model.micMuted ? Palette.pick.opacity(0.5) : Palette.line),
                          lineWidth: 1))
    }
}

/// Partner lines and the user's spoken lines (under the user's first name).
struct ConversationView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            MonoLabel("Conversation")
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
                            withAnimation(reduceMotion ? nil : .default) {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
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
        let shape = RoundedRectangle(cornerRadius: 10)
        VStack(alignment: line.fromPartner ? .leading : .trailing, spacing: 3) {
            MonoLabel(line.fromPartner ? "Partner" : userName, color: line.fromPartner ? Palette.next : Palette.say)
            Text(line.text)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(line.fromPartner ? Palette.surface : Palette.say.opacity(0.12), in: shape)
                .overlay(shape.strokeBorder(line.fromPartner ? Palette.line : Palette.say.opacity(0.35),
                                            lineWidth: 1))
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
                .textFieldStyle(.plain)
                .submitLabel(.send)
                .focused($focused)
                .onSubmit(send)
                .padding(.horizontal, 12)
                .frame(minHeight: 44)
                .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.radius))
                .overlay(RoundedRectangle(cornerRadius: Palette.radius)
                    .strokeBorder(focused ? Palette.look : Palette.line, lineWidth: 1))
            Button("Send", action: send)
                .buttonStyle(FilledButtonStyle(.primary))
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
        VStack(alignment: .leading, spacing: 12) {
            MonoLabel("Operator")
            if let health = model.health {
                Button {
                    expanded.toggle()
                } label: {
                    HStack(spacing: 8) {
                        Circle().fill(color(health.level)).frame(width: 8, height: 8)
                        Text(health.text).font(.subheadline).multilineTextAlignment(.leading)
                        Spacer()
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.footnote)
                            .foregroundStyle(Palette.muted)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityHint("Shows the details")
                if expanded, let status = model.status {
                    StatusDetails(status: status)
                }
            }
            HStack(spacing: 8) {
                Text("Last reply").font(.footnote).foregroundStyle(Palette.muted)
                Button("Correct") { model.judge(correct: true) }
                    .buttonStyle(FilledButtonStyle(.secondary))
                Button("Wrong") { model.judge(correct: false) }
                    .buttonStyle(FilledButtonStyle(.secondary))
                Text(model.judged).font(.footnote).foregroundStyle(Palette.muted)
            }
            .disabled(model.lastSpokenId == nil || !model.connected)
            if let link = model.link {
                Text(link.base.host ?? link.base.absoluteString)
                    .font(.caption.monospaced())
                    .foregroundStyle(Palette.muted)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .card()
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
                        MonoLabel(badge.0, color: tint(badge.1))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .overlay(RoundedRectangle(cornerRadius: 4)
                                .strokeBorder(tint(badge.1).opacity(0.5), lineWidth: 1))
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
            StopButton()

            Button {
                model.resumePressed()
            } label: {
                Text("Resume")
                    .frame(minWidth: 80)
            }
            .buttonStyle(FilledButtonStyle(.secondary, large: true))
            .disabled(!model.resumeEnabled)
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 6)
        .background(Palette.bg)
        .overlay(alignment: .top) {
            Rectangle().fill(Palette.line).frame(height: 1)
        }
    }
}

/// The big red Stop, shared by the main screen, the setup, the warning and Settings. It works
/// before the warning is accepted (the app may be opened while the laptop already flickers).
struct StopButton: View {
    @Environment(AppModel.self) private var model
    let title: String

    init(title: String = "Stop") {
        self.title = title
    }

    var body: some View {
        Button {
            model.stopPressed()
        } label: {
            Text(model.connected ? title : "Not connected: press Esc on the display")
                .font(model.connected ? .title3.weight(.bold) : .subheadline.weight(.bold))
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(FilledButtonStyle(.danger, large: true))
        // Never disabled: while not connected a tap still drops a resume waiting to go out.
        .accessibilityHint("Turns the flicker off and pauses the session")
    }
}
