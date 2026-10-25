// The mirror of the user's screen (ui/app.js renderPage), phrase first like Look to Speak: each
// reply is a big full-width card, with a chip saying where its target sits on the user's diamond.
// Nothing here ever flickers (rule 10): a flickering target is only a steady lighter fill.

import SwiftUI
import BrainSyncKit

struct MirrorView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("textSize") private var textSize: TextSize = .standard

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                MonoLabel("User's screen · no flicker")
                Spacer()
                if !model.phase.isEmpty {
                    PhasePill(phase: model.phase)
                }
            }

            if model.stateName == "SPEAKING" {
                SpeakingCard(text: model.stateDetail.isEmpty ? model.speaker.speakingText : model.stateDetail)
            } else {
                if let started = model.trialStarted {
                    SelectionTimer(started: started, max: model.maxTrialS)
                }
                if let card = model.training {
                    TrainingCardView(card: card)
                }
                if let page = model.page {
                    pageView(page)
                } else {
                    Text("Waiting for the first page…")
                        .foregroundStyle(Palette.muted)
                        .frame(maxWidth: .infinity, minHeight: 120)
                        .card()
                }
            }
        }
        .dynamicTypeSize(textSize.range)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("User's screen, mirror without flicker")
    }

    @ViewBuilder
    private func pageView(_ page: ShowPage) -> some View {
        let flickering = model.phase == "flicker" || model.phase == "train"
        MonoLabel(heading(page))
        if model.layout == "cvep" && page.mode != .list {
            // c-VEP: the user's three squares in a row, each with its sentence and confidence bar.
            if page.mode == .confirm && !page.text.isEmpty {
                Text("“\(page.text)”")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .card(padding: 14)
            }
            SquareRow(order: model.screenSlots, slots: page.slots, picked: model.pickedSlot,
                      cue: model.training?.done == false ? model.trainingSlot : nil,
                      active: flickering, bars: model.confidence, keyboard: model.keyboardMode,
                      place: model.place) { slot in model.tapSlot(slot) }
        } else {
            classicView(page, flickering: flickering)
        }
    }

    @ViewBuilder
    private func classicView(_ page: ShowPage, flickering: Bool) -> some View {
        switch page.mode {
        case .page:
            PhraseStack(slots: page.slots, picked: model.pickedSlot, active: flickering,
                        keyboard: model.keyboardMode) { slot in model.tapSlot(slot) }
        case .confirm:
            ConfirmPanel(sentence: page.text, slots: page.slots, picked: model.pickedSlot,
                         active: flickering, keyboard: model.keyboardMode) { slot in model.tapSlot(slot) }
        case .list:
            listView(page, flickering: flickering)
        }
    }

    /// List mode (the fallback): the options with one highlighted, then SAY and NEXT.
    @ViewBuilder
    private func listView(_ page: ShowPage, flickering: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(page.options.enumerated()), id: \.offset) { index, option in
                let current = index == page.highlightIndex
                Text(option.utterance)
                    .font(current ? .title3.weight(.semibold) : .body)
                    .foregroundStyle(current ? Palette.text : Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
                    .background(current ? Palette.surfaceHi : Palette.surface,
                                in: RoundedRectangle(cornerRadius: Palette.radius))
                    .overlay(RoundedRectangle(cornerRadius: Palette.radius)
                        .strokeBorder(current ? Palette.look : Palette.line, lineWidth: current ? 2 : 1))
            }
        }
        HStack(alignment: .top, spacing: 10) {
            ForEach(page.slots, id: \.slot) { slot in
                PhraseCard(slot: slot, picked: model.pickedSlot == slot.slot, active: flickering,
                           keyboard: model.keyboardMode, positioned: false) {
                    model.tapSlot(slot.slot)
                }
            }
        }
    }

    private func heading(_ page: ShowPage) -> String {
        switch page.mode {
        case .list:
            let index = (page.highlightIndex ?? 0) + 1
            return "Page \(page.page) · option \(index) of \(page.options.count)"
        case .page:
            return "Page \(page.page) · Pick a reply"
        case .confirm:
            return "Page \(page.page) · Say this?"
        }
    }
}

/// c-VEP (docs/CVEP-STANDARD.md): the user's squares in a row, in slot order. A slot that does
/// not flicker this trial (the middle one on the confirm screen) stays as an empty square. Each
/// square is a steady card (a lighter fill while its target flickers, never a flash), with its
/// sentence or label and the decoder's confidence bar under it.
struct SquareRow: View {
    let order: [String]
    let slots: [UISlot]
    let picked: String?
    /// The training square (the cue), while the template is recorded.
    let cue: String?
    let active: Bool
    let bars: [String: ConfidenceBar]
    var keyboard = false
    var place: (String) -> String = { _ in "" }
    var onTap: (String) -> Void = { _ in }

    var body: some View {
        let bySlot = Dictionary(slots.map { ($0.slot, $0) }, uniquingKeysWith: { a, _ in a })
        HStack(alignment: .top, spacing: 8) {
            ForEach(order, id: \.self) { slot in
                column(slot: slot, ui: bySlot[slot])
                    .frame(maxWidth: .infinity)
            }
        }
    }

    @ViewBuilder
    private func column(slot: String, ui: UISlot?) -> some View {
        let shape = RoundedRectangle(cornerRadius: Palette.radius)
        let isPicked = picked == slot
        let isCue = cue == slot
        let flickers = active && (ui != nil || cue != nil)
        let content = VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 4) {
                PositionChip(text: place(slot))
                Spacer(minLength: 0)
                if isPicked { MonoLabel("Picked", color: Palette.pick) }
                else if keyboard && ui != nil { MonoLabel("key " + String(slot.dropFirst())) }
            }
            // The square itself: a steady fill, lighter while it flickers on the user's screen.
            shape.fill(flickers ? Palette.surfaceHi : Palette.surface)
                .overlay(shape.strokeBorder(isPicked ? Palette.pick : isCue ? Palette.look : Palette.lineStrong,
                                            style: StrokeStyle(lineWidth: isPicked || isCue ? 3 : 1,
                                                               dash: ui == nil && cue == nil ? [4, 3] : [])))
                .aspectRatio(1, contentMode: .fit)
                .frame(maxWidth: 72)
            if let ui {
                let sentence = ui.utterance.trimmingCharacters(in: .whitespacesAndNewlines)
                Text(sentence.isEmpty ? ui.label : sentence)
                    .font(sentence.isEmpty ? .headline : .callout.weight(.semibold))
                    .foregroundStyle(ui.role == .next || ui.role == .back ? Palette.next
                                     : ui.role == .say ? Palette.say : Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let bar = bars[slot] {
                ConfidenceBarView(bar: bar)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius)
            .strokeBorder(isPicked ? Palette.pick : Palette.line, lineWidth: isPicked ? 2 : 1))
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isPicked ? .isSelected : [])

        if keyboard, ui != nil {
            Button { onTap(slot) } label: { content }
                .buttonStyle(.plain)
                .disabled(!active)
        } else {
            content
        }
    }
}

/// The decoder's confidence under a square: a steady bar of the correlation (0...1) and its
/// signed score; blue on this decision's best, yellow once committed. Not a percentage.
struct ConfidenceBarView: View {
    let bar: ConfidenceBar

    var body: some View {
        HStack(spacing: 6) {
            ProgressView(value: bar.fraction)
                .tint(bar.committed ? Palette.pick : bar.best ? Palette.look : Palette.muted)
            Text(bar.text)
                .font(.caption.monospacedDigit())
                .foregroundStyle(Palette.muted)
        }
        .accessibilityLabel("confidence \(bar.text)")
    }
}

/// c-VEP's training step: where to look, the cycles so far, then the template's accuracy.
struct TrainingCardView: View {
    let card: TrainingCard

    var body: some View {
        let color: Color = card.tone == "ok" ? Palette.say : card.tone == "bad" ? Palette.danger : Palette.look
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(card.title)
                    .font(.headline)
                    .foregroundStyle(Palette.text)
                Spacer(minLength: 8)
                MonoLabel(card.detail)
            }
            ProgressView(value: card.fraction)
                .tint(color)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius).strokeBorder(color.opacity(0.6), lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

/// Where each slot's target sits on the user's diamond screen (PLAN section 5).
enum SlotPosition {
    static func name(_ slot: String) -> String {
        switch slot {
        case "s1": return "Top"
        case "s2": return "Left"
        case "s3": return "Right"
        case "s4": return "Bottom"
        default: return ""
        }
    }
}

/// One page's targets, phrase first: the option cards big and full width, then NEXT smaller.
/// Takes plain slots, so the practice round draws the same cards from sample data.
struct PhraseStack: View {
    let slots: [UISlot]
    let picked: String?
    let active: Bool
    var keyboard = false
    var onTap: (String) -> Void = { _ in }

    var body: some View {
        VStack(spacing: 10) {
            ForEach(ordered, id: \.slot) { slot in
                PhraseCard(slot: slot, picked: picked == slot.slot, active: active, keyboard: keyboard) {
                    onTap(slot.slot)
                }
            }
        }
    }

    /// The options in the server's order (s1, s2, s3), NEXT last.
    private var ordered: [UISlot] {
        slots.filter { $0.role != .next } + slots.filter { $0.role == .next }
    }
}

/// The confirm screen: the picked sentence large, then Say it and Back side by side.
struct ConfirmPanel: View {
    let sentence: String
    let slots: [UISlot]
    let picked: String?
    let active: Bool
    var keyboard = false
    var onTap: (String) -> Void = { _ in }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !sentence.isEmpty {
                Text("“\(sentence)”")
                    .font(.title.weight(.semibold))
                    .tracking(-0.5)
                    .foregroundStyle(Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .card(padding: 16)
            }
            HStack(alignment: .top, spacing: 10) {
                ForEach(slots, id: \.slot) { slot in
                    PhraseCard(slot: slot, picked: picked == slot.slot, active: active, keyboard: keyboard) {
                        onTap(slot.slot)
                    }
                }
            }
        }
    }
}

/// One target as a card. The big text is what gets spoken; the tag is its short label. A picked
/// card gets the yellow ring and "Picked"; a flickering one only a steady lighter fill.
struct PhraseCard: View {
    /// Top / Left / Right / Bottom on the user's screen; "" hides the chip.
    let position: String
    /// The short label, shown small when there is a sentence; "" hides it.
    let tag: String
    let text: String
    let accent: Color
    /// Option cards are big; NEXT is smaller.
    var prominent = true
    var picked = false
    var active = false
    /// Keyboard mode: "key 1" on the card.
    var keyHint: String?
    /// Keyboard mode only: a tap is that target's key. nil = not a button.
    var onTap: (() -> Void)?

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Palette.cardRadius)
        let content = VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                if !position.isEmpty {
                    PositionChip(text: position)
                }
                if !tag.isEmpty {
                    MonoLabel(tag, color: accent)
                }
                Spacer(minLength: 0)
                if picked {
                    MonoLabel("Picked", color: Palette.pick)
                } else if let keyHint {
                    MonoLabel(keyHint)
                }
            }
            Text(text)
                .font(prominent ? .title2.weight(.semibold) : .headline)
                .foregroundStyle(prominent || picked ? Palette.text : accent)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(prominent ? 16 : 12)
        .frame(maxWidth: .infinity, minHeight: prominent ? 96 : 56, alignment: .topLeading)
        .background(active ? Palette.surfaceHi : Palette.surface, in: shape)
        .overlay(shape.strokeBorder(picked ? Palette.pick : (active ? Palette.lineStrong : Palette.line),
                                    lineWidth: picked ? 3 : 1))
        .overlay {
            // The one ring allowed: a faint halo outside the picked card, steady.
            if picked {
                RoundedRectangle(cornerRadius: Palette.cardRadius + 3)
                    .strokeBorder(Palette.pick.opacity(0.25), lineWidth: 3)
                    .padding(-4)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(picked ? .isSelected : [])

        if let onTap {
            Button(action: onTap) { content }
                .buttonStyle(.plain)
                .disabled(!active)
        } else {
            content
        }
    }
}

extension PhraseCard {
    /// A card for a server slot. Targets without a sentence (More options, Say it, Back) show
    /// their label as the big text.
    init(slot: UISlot, picked: Bool, active: Bool, keyboard: Bool, positioned: Bool = true,
         onTap: @escaping () -> Void) {
        let sentence = slot.utterance.trimmingCharacters(in: .whitespacesAndNewlines)
        let accent = (slot.role == .next || slot.role == .back) ? Palette.next : Palette.say
        self.init(position: positioned ? SlotPosition.name(slot.slot) : "",
                  tag: sentence.isEmpty ? "" : slot.label,
                  text: sentence.isEmpty ? slot.label : sentence,
                  accent: accent,
                  prominent: slot.role != .next,
                  picked: picked,
                  active: active,
                  keyHint: keyboard ? "key " + String(slot.slot.dropFirst()) : nil,
                  onTap: keyboard ? onTap : nil)
    }
}

/// Where a target sits on the user's screen, as a small outlined chip.
struct PositionChip: View {
    let text: String

    var body: some View {
        MonoLabel(text)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Palette.lineStrong, lineWidth: 1))
    }
}

/// The reply being spoken, as one large card.
struct SpeakingCard: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "speaker.wave.2.fill")
                    .font(.footnote)
                    .foregroundStyle(Palette.say)
                MonoLabel("Speaking", color: Palette.say)
            }
            Text("“\(text)”")
                .font(.title.weight(.semibold))
                .tracking(-0.5)
                .foregroundStyle(Palette.text)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, minHeight: 140, alignment: .topLeading)
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius).strokeBorder(Palette.say, lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

struct PhasePill: View {
    let phase: String

    var body: some View {
        let color: Color = {
            switch phase {
            case "flicker", "train": return Palette.look
            case "cue": return Palette.text
            case "baseline": return Palette.warn
            case "paused": return Palette.danger
            default: return Palette.muted
            }
        }()
        MonoLabel(phase == "flicker" ? "flickering" : phase == "train" ? "training" : phase, color: color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .overlay(Capsule().strokeBorder(color.opacity(0.6), lineWidth: 1))
    }
}

/// How long the user has been looking, out of stim.max_trial_s: a steady fill, never a flash.
struct SelectionTimer: View {
    let started: Date
    let max: Double

    var body: some View {
        TimelineView(.periodic(from: started, by: 0.2)) { context in
            let elapsed = Swift.max(0, context.date.timeIntervalSince(started))
            let fraction = max > 0 ? Swift.min(1, elapsed / max) : 0
            HStack(spacing: 8) {
                ProgressView(value: fraction)
                    .tint(Palette.look)
                Text("\(Int(elapsed)) s")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(Palette.muted)
            }
        }
    }
}
