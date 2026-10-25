// The user's screen on the phone, like Google's Look to Speak (INTERFACE-v2-draft.md section 9):
// two big cards side by side, left (s1) and right (s2), each holding half of the phrases still in
// play. The user looks at one; the headset decodes which from the c-VEP code each card flashes
// (FlickerEngine), and that half stays until one phrase is left; then Say it (left) / Back
// (right). Same look as the web session page (ui/style.css): black, black cards with a 1px line
// and 12px corners, white text with a black outline over the flicker layer, the state line in the
// still centre, no banners. Nothing here animates; only the engine's layers change, frame by frame.
//
// Safety (PLAN rule 10): nothing flickers before the warning is accepted (AppModel), a tap
// anywhere while it flickers stops it at once and sends Stop, and a small red Stop is always at
// the bottom. The partner tools (conversation, typing, mic, Correct / Wrong) open over it.

import SwiftUI
import BrainSyncKit

struct LookToSpeakView: View {
    @Environment(AppModel.self) private var model
    @State private var showTools = false

    var body: some View {
        VStack(spacing: 12) {
            LookScreen(page: model.page, picked: model.pickedSlot,
                       speaking: model.speakingSentence, stateLine: model.stateLine,
                       engine: model.flicker)
            controls
        }
        .padding(.horizontal, 12)
        .padding(.top, 12)
        .padding(.bottom, 6)
        .background(Color.black.ignoresSafeArea())
        .foregroundStyle(Color.white)
        .overlay {
            if model.tapStops {
                // Over everything, Stop and the tools button included: any tap stops the flicker.
                Color.clear
                    .contentShape(Rectangle())
                    .ignoresSafeArea()
                    .onTapGesture { model.tapToStop() }
                    .accessibilityElement()
                    .accessibilityLabel("Stop the flicker")
                    .accessibilityAddTraits(.isButton)
                    .accessibilityAction { model.tapToStop() }
            }
        }
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .onAppear { model.setUserScreen(visible: !showTools) }
        .onDisappear { model.userScreenClosed() }
        .onChange(of: showTools) { _, open in
            model.setUserScreen(visible: !open)  // covered: no flicker under the tools
        }
        .sheet(isPresented: $showTools) {
            PartnerView(onClose: { showTools = false })
                .environment(model)
        }
    }

    /// Stop (small, red, always there), Resume while paused, and the partner tools.
    private var controls: some View {
        HStack(spacing: 10) {
            Button {
                model.stopPressed()
            } label: {
                Label("Stop", systemImage: "stop.fill")
            }
            .buttonStyle(FilledButtonStyle(.danger))
            .accessibilityHint("Turns the flicker off and pauses the session")

            if ["PAUSED", "IDLE"].contains(model.stateName) {
                Button("Resume") { model.resumePressed() }
                    .buttonStyle(FilledButtonStyle(.secondary))
                    .disabled(!model.resumeEnabled)
            }
            Spacer(minLength: 0)
            Button {
                showTools = true
            } label: {
                Label("Partner", systemImage: "person.2")
            }
            .buttonStyle(FilledButtonStyle(.secondary))
            .accessibilityLabel("Partner tools")
            .accessibilityHint("Conversation, typing, the microphone and Correct or Wrong")
        }
    }
}

/// What the user sees, drawn from a page: the centre (the sentence to confirm or being spoken, the
/// calibration instruction, the state line) above the two cards. Full size on the user's screen
/// with the engine's overlays; `compact` and without an engine (always still) in the partner
/// tools' mirror and the practice round.
struct LookScreen: View {
    let page: ShowPage?
    let picked: String?
    /// The sentence being spoken; nil when nothing plays.
    let speaking: String?
    let stateLine: StateLine
    /// The flicker's owner; nil = the cards never flicker (mirror, practice).
    var engine: FlickerEngine? = nil
    var compact = false
    /// Keyboard mode in the mirror: a tap on a card is its slot's key. nil = cards are not buttons.
    var onTap: ((String) -> Void)? = nil

    var body: some View {
        VStack(spacing: compact ? 10 : 16) {
            centre
            HStack(spacing: compact ? 8 : 12) {
                card(at: 0)
                card(at: 1)
            }
            .frame(maxHeight: compact ? nil : .infinity)
            .frame(height: compact ? 240 : nil)
        }
    }

    // MARK: Centre

    /// The still middle of the screen. In halves mode only the state line, which keeps its room
    /// even when empty, so the cards never move when it changes.
    @ViewBuilder
    private var centre: some View {
        VStack(spacing: compact ? 6 : 10) {
            if let speaking {
                MonoLabel("Speaking", color: Palette.muted)
                Text(speaking)
                    .font(.system(size: compact ? 20 : 32, weight: .semibold))
                    .tracking(-0.5)
            } else if let text = centreText {
                Text(text)
                    .font(.system(size: compact ? 20 : (page?.mode == .calibrate ? 26 : 32),
                                  weight: .semibold))
                    .tracking(-0.5)
            }
            Text(stateLine.text)
                .font(.system(size: compact ? 14 : 19, weight: .semibold))
                .foregroundStyle(stateColor)
                .lineLimit(2)
                .opacity(stateLine.text.isEmpty ? 0 : 1)
                .accessibilityHidden(stateLine.text.isEmpty)
                .accessibilityAddTraits(.updatesFrequently)
        }
        .multilineTextAlignment(.center)
        .foregroundStyle(Color.white)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, minHeight: compact ? 24 : 56)
        .accessibilityElement(children: .combine)
    }

    /// The confirm screen's sentence, or the calibration's instruction.
    private var centreText: String? {
        guard let page, page.mode == .confirm || page.mode == .calibrate else { return nil }
        let text = page.text.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    private var stateColor: Color {
        switch stateLine.tone {
        case .warn: return Palette.warn
        case .stop: return Palette.danger
        default: return Palette.muted
        }
    }

    // MARK: Cards

    /// Card 0 is the left one (s1), card 1 the right one (s2).
    @ViewBuilder
    private func card(at index: Int) -> some View {
        let slot = slotId(index)
        let cued = page?.mode == .calibrate && page?.cue == slot
        LookCard(slot: slot, side: index == 0 ? "Left" : "Right", content: content(slot: slot, index: index),
                 ringed: picked == slot || cued, engine: engine, compact: compact,
                 onTap: onTap.map { tap in { tap(slot) } })
    }

    /// The page's slot on that side: s1 left and s2 right (draft section 9), else in page order.
    private func slotId(_ index: Int) -> String {
        let wanted = index == 0 ? "s1" : "s2"
        guard let slots = page?.slots, !slots.isEmpty else { return wanted }
        if slots.contains(where: { $0.slot == wanted }) { return wanted }
        return index < slots.count ? slots[index].slot : wanted
    }

    private func content(slot: String, index: Int) -> LookCard.Content {
        guard let page else { return .title("") }
        let uiSlot = page.slots.first { $0.slot == slot }
        switch page.mode {
        case .halves:
            let halves = page.halves
            return .phrases(index == 0 ? halves.left : halves.right)
        case .calibrate:
            if page.cue == slot {
                let label = uiSlot?.label ?? ""
                return .cue(label.isEmpty ? "Look here" : label)
            }
            return .title(uiSlot?.label ?? "")
        case .confirm, .page, .list:
            let sentence = uiSlot?.utterance.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return .title(sentence.isEmpty ? (uiSlot?.label ?? "") : sentence)
        }
    }
}

/// One card: black, its flicker layer over it (white at the engine's opacity), the text over
/// both in white with a black outline. A picked card, or the calibration's cue card, gets the one
/// ring on the screen: white, steady.
struct LookCard: View {
    enum Content: Equatable {
        /// Halves mode: the phrases on this card, "More options" muted.
        case phrases([MenuOption])
        /// Say it / Back, or nothing.
        case title(String)
        /// Calibration: the card to look at ("Look here").
        case cue(String)
    }

    let slot: String
    /// "Left" / "Right", for VoiceOver.
    let side: String
    let content: Content
    let ringed: Bool
    var engine: FlickerEngine? = nil
    var compact = false
    var onTap: (() -> Void)? = nil

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Palette.cardRadius)
        let card = ZStack {
            shape.fill(Color.black)
            if let engine {
                FlickerOverlay(slot: slot, engine: engine)
                    .accessibilityHidden(true)
            }
            inner
                .padding(compact ? 10 : 14)
                .outlined()
        }
        .clipShape(shape)
        .overlay(shape.strokeBorder(ringed ? Color.white : Palette.lineStrong, lineWidth: ringed ? 2 : 1))
        .overlay {
            if ringed {
                RoundedRectangle(cornerRadius: Palette.cardRadius + 4)
                    .strokeBorder(Color.white.opacity(0.2), lineWidth: 4)
                    .padding(-5)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
        .accessibilityAddTraits(ringed ? .isSelected : [])

        if let onTap {
            Button(action: onTap) { card }
                .buttonStyle(.plain)
        } else {
            card
        }
    }

    @ViewBuilder
    private var inner: some View {
        switch content {
        case .phrases(let items):
            PhraseColumn(items: items, compact: compact)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        case .title(let text):
            Text(text)
                .font(.system(size: compact ? 22 : 36, weight: .bold))
                .tracking(-0.5)
                .multilineTextAlignment(.center)
                .minimumScaleFactor(0.5)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .cue(let text):
            VStack(spacing: compact ? 8 : 14) {
                Image(systemName: "eye")
                    .font(.system(size: compact ? 22 : 40, weight: .semibold))
                Text(text)
                    .font(.system(size: compact ? 20 : 32, weight: .bold))
                    .tracking(-0.5)
                    .multilineTextAlignment(.center)
                    .minimumScaleFactor(0.5)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var accessibilityText: String {
        switch content {
        case .phrases(let items):
            let words = items.map { $0.isMoreOptions ? Self.moreLabel($0) : $0.utterance }
            return "\(side) card: " + (words.isEmpty ? "empty" : words.joined(separator: ". "))
        case .title(let text):
            return "\(side) card" + (text.isEmpty ? "" : ": \(text)")
        case .cue(let text):
            return "\(side) card: \(text)"
        }
    }

    static func moreLabel(_ item: MenuOption) -> String {
        let label = item.label.trimmingCharacters(in: .whitespaces)
        return label.isEmpty ? "More options" : label
    }
}

/// A card's phrases, the full sentences (what gets spoken, PLAN rule 2) at the largest size that
/// fits the card, "More options" muted.
private struct PhraseColumn: View {
    let items: [MenuOption]
    let compact: Bool

    var body: some View {
        let sizes: [CGFloat] = compact ? [17, 15, 13] : [26, 22, 19, 16]
        ViewThatFits(in: .vertical) {
            ForEach(sizes, id: \.self) { size in
                column(size: size, fixed: true)
            }
            column(size: compact ? 11 : 14, fixed: false)
        }
    }

    private func column(size: CGFloat, fixed: Bool) -> some View {
        VStack(alignment: .leading, spacing: size * 0.7) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                Text(item.isMoreOptions ? LookCard.moreLabel(item) : item.utterance)
                    .font(.system(size: size, weight: item.isMoreOptions ? .medium : .semibold))
                    .foregroundStyle(item.isMoreOptions ? Palette.muted : Color.white)
                    .lineSpacing(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: fixed)
                    .minimumScaleFactor(fixed ? 1 : 0.6)
            }
        }
    }
}
