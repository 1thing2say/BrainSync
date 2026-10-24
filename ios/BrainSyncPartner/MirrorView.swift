// The mirror of the user's screen (ui/app.js renderPage): what the PsychoPy display shows, drawn
// still. Nothing here ever flickers (rule 10): a flickering target is only a steady highlight.

import SwiftUI
import BrainSyncKit

/// The partner page's colours (ui/style.css :root).
enum Palette {
    static let bg = Color(red: 0x0f / 255, green: 0x11 / 255, blue: 0x15 / 255)
    static let panel = Color(red: 0x17 / 255, green: 0x1a / 255, blue: 0x21 / 255)
    static let surface = Color(red: 0x23 / 255, green: 0x28 / 255, blue: 0x33 / 255)
    static let surfaceHi = Color(red: 0x2b / 255, green: 0x31 / 255, blue: 0x40 / 255)
    static let text = Color(red: 0xe8 / 255, green: 0xea / 255, blue: 0xee / 255)
    static let muted = Color(red: 0x90 / 255, green: 0x97 / 255, blue: 0xa3 / 255)
    static let line = Color(red: 0x2a / 255, green: 0x2f / 255, blue: 0x3a / 255)
    static let say = Color(red: 0x3f / 255, green: 0xb2 / 255, blue: 0x7f / 255)
    static let next = Color(red: 0x5b / 255, green: 0x8d / 255, blue: 0xef / 255)
    static let look = Color(red: 0x6c / 255, green: 0xb6 / 255, blue: 0xff / 255)
    static let warn = Color(red: 0xe0 / 255, green: 0xa4 / 255, blue: 0x3a / 255)
    static let danger = Color(red: 0xe0 / 255, green: 0x52 / 255, blue: 0x52 / 255)
    static let pick = Color(red: 0xff / 255, green: 0xd3 / 255, blue: 0x4d / 255)
}

struct MirrorView: View {
    @Environment(AppModel.self) private var model

    private static let headings = ["page": "Pick a reply", "confirm": "Say this?", "list": "Highlighted reply"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("User's screen")
                    .font(.caption.weight(.semibold))
                    .textCase(.uppercase)
                    .foregroundStyle(Palette.muted)
                Spacer()
                if !model.phase.isEmpty {
                    PhasePill(phase: model.phase)
                }
            }

            if model.stateName == "SPEAKING" {
                speakingBox
            } else if let started = model.trialStarted {
                SelectionTimer(started: started, max: model.maxTrialS)
            }

            if let page = model.page {
                pageView(page)
            } else {
                Text("Waiting for the first page…")
                    .foregroundStyle(Palette.muted)
                    .frame(maxWidth: .infinity, minHeight: 120)
            }
        }
        .padding(14)
        .background(Palette.panel, in: RoundedRectangle(cornerRadius: 16))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("User's screen, mirror without flicker")
    }

    private var speakingBox: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Speaking")
                .font(.caption.weight(.bold))
                .textCase(.uppercase)
                .foregroundStyle(Palette.say)
            Text("“\(model.stateDetail.isEmpty ? model.speaker.speakingText : model.stateDetail)”")
                .font(.title2.weight(.semibold))
                .foregroundStyle(Palette.text)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Palette.say.opacity(0.15), in: RoundedRectangle(cornerRadius: 12))
    }

    @ViewBuilder
    private func pageView(_ page: ShowPage) -> some View {
        let mode = String(describing: page.mode)
        if model.stateName != "SPEAKING" {
            VStack(alignment: .leading, spacing: 4) {
                Text(heading(page, mode: mode))
                    .font(.footnote)
                    .foregroundStyle(Palette.muted)
                if mode != "page" && !page.text.isEmpty {
                    Text(page.text)
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(Palette.text)
                }
            }
        }
        if mode == "list" {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(page.options.enumerated()), id: \.offset) { index, option in
                    let current = index == page.highlightIndex
                    Text(option.utterance)
                        .font(current ? .body.weight(.semibold) : .body)
                        .foregroundStyle(current ? Palette.text : Palette.muted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(current ? Palette.surfaceHi : Palette.surface,
                                    in: RoundedRectangle(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10)
                            .stroke(current ? Palette.look : Color.clear, lineWidth: 2))
                }
            }
            HStack(spacing: 10) {
                ForEach(page.slots, id: \.slot) { slot in target(slot) }
            }
        } else {
            diamond(page.slots)
        }
    }

    private func heading(_ page: ShowPage, mode: String) -> String {
        if mode == "list" {
            let index = (page.highlightIndex ?? 0) + 1
            return "Page \(page.page) · option \(index) of \(page.options.count)"
        }
        return "Page \(page.page) · \(Self.headings[mode] ?? mode)"
    }

    /// s1 top, s2 left, s3 right, s4 bottom, as on the user's screen.
    @ViewBuilder
    private func diamond(_ slots: [UISlot]) -> some View {
        let bySlot = Dictionary(slots.map { ($0.slot, $0) }, uniquingKeysWith: { first, _ in first })
        let known: Set<String> = ["s1", "s2", "s3", "s4"]
        VStack(spacing: 10) {
            if let top = bySlot["s1"] { target(top) }
            if bySlot["s2"] != nil || bySlot["s3"] != nil {
                HStack(alignment: .top, spacing: 10) {
                    if let left = bySlot["s2"] { target(left) }
                    if let right = bySlot["s3"] { target(right) }
                }
            }
            if let bottom = bySlot["s4"] { target(bottom) }
            ForEach(slots.filter { !known.contains($0.slot) }, id: \.slot) { extra in target(extra) }
        }
    }

    private func target(_ slot: UISlot) -> some View {
        TargetBox(slot: slot,
                  picked: model.pickedSlot == slot.slot,
                  active: model.phase == "flicker",
                  keyboard: model.keyboardMode) {
            model.tapSlot(slot.slot)
        }
    }
}

/// One target. The sentence is what gets spoken, so it is the big text and the label is its tag;
/// targets without a sentence (More options, Say it, Back) show the label big.
struct TargetBox: View {
    let slot: UISlot
    let picked: Bool
    let active: Bool
    let keyboard: Bool
    let onTap: () -> Void

    var body: some View {
        let sentence = slot.utterance.trimmingCharacters(in: .whitespacesAndNewlines)
        let role = String(describing: slot.role)
        let accent = (role == "next" || role == "back") ? Palette.next : Palette.say
        let content = VStack(alignment: .leading, spacing: 4) {
            HStack {
                if !sentence.isEmpty {
                    Text(slot.label)
                        .font(.caption2.weight(.bold))
                        .textCase(.uppercase)
                        .foregroundStyle(accent)
                }
                Spacer(minLength: 0)
                if picked {
                    Text("Picked")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Palette.pick)
                } else if keyboard {
                    Text("key " + String(slot.slot.dropFirst()))
                        .font(.caption2)
                        .foregroundStyle(Palette.muted)
                }
            }
            Text(sentence.isEmpty ? slot.label : sentence)
                .font(sentence.isEmpty ? .headline : .body.weight(.semibold))
                .foregroundStyle(Palette.text)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(10)
        .frame(maxWidth: .infinity, minHeight: 72, alignment: .topLeading)
        .background(active ? Palette.surfaceHi : Palette.surface, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .stroke(picked ? Palette.pick : accent.opacity(active ? 0.9 : 0.4), lineWidth: picked ? 3 : 1.5))

        if keyboard {
            Button(action: onTap) { content }
                .buttonStyle(.plain)
                .disabled(!active)
        } else {
            content
        }
    }
}

struct PhasePill: View {
    let phase: String

    var body: some View {
        let color: Color = {
            switch phase {
            case "flicker": return Palette.look
            case "cue": return Palette.next
            case "baseline": return Palette.warn
            default: return Palette.muted
            }
        }()
        Text(phase == "flicker" ? "flickering" : phase)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .foregroundStyle(color)
            .overlay(Capsule().stroke(color, lineWidth: 1))
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
