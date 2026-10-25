// A practice round for the partner, offline and with sample data: no server, nothing sent,
// nothing spoken. It walks through one reply with the same cards as the mirror, one step per tap
// of Next (no timers), with a caption for each step. The highlight is steady, never blinking.
// With a c-VEP server it walks through Look to Speak on this phone instead (LookToSpeakPractice):
// the same two cards as the user's screen, drawn still, never flickering.

import SwiftUI
import BrainSyncKit

struct PracticeView: View {
    /// Look to Speak on this phone (c-VEP) instead of the laptop's diamond.
    let lookToSpeak: Bool
    /// Called once the walk-through reaches its last step.
    var onFinish: (() -> Void)?

    @AppStorage("textSize") private var textSize: TextSize = .standard
    @State private var stage = Stage.heard

    init(lookToSpeak: Bool = false, onFinish: (() -> Void)? = nil) {
        self.lookToSpeak = lookToSpeak
        self.onFinish = onFinish
    }

    private enum Stage: Int, CaseIterable {
        case heard, options, looking, picked, confirm, speaking
    }

    private static let question = "Do you want to go outside?"
    private static let options = [
        UISlot(slot: "s1", role: .option, label: "Go outside", utterance: "Yes, let's go outside."),
        UISlot(slot: "s2", role: .option, label: "Not now", utterance: "Not right now."),
        UISlot(slot: "s3", role: .option, label: "After lunch", utterance: "Maybe after lunch."),
        UISlot(slot: "s4", role: .next, label: "More options"),
    ]
    private static let confirm = [
        UISlot(slot: "s1", role: .say, label: "Say it"),
        UISlot(slot: "s4", role: .back, label: "Back"),
    ]

    var body: some View {
        if lookToSpeak {
            LookToSpeakPractice(onFinish: onFinish)
        } else {
            diamondPractice
        }
    }

    private var diamondPractice: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                MonoLabel("Practice · \(stage.rawValue + 1) of \(Stage.allCases.count)")
                Spacer()
                MonoLabel("Sample, offline")
            }
            Callout(title: title, icon: "info.circle", tint: Palette.next) {
                Text(caption)
                    .font(.subheadline)
                    .foregroundStyle(Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 12) {
                PartnerSaidCard(text: Self.question)
                screen
            }
            .dynamicTypeSize(textSize.range)

            HStack(spacing: 10) {
                if let previous = Stage(rawValue: stage.rawValue - 1) {
                    Button("Previous") { stage = previous }
                        .buttonStyle(FilledButtonStyle(.secondary))
                }
                Spacer()
                if let next = Stage(rawValue: stage.rawValue + 1) {
                    Button("Next") { go(to: next) }
                        .buttonStyle(FilledButtonStyle(.primary))
                } else {
                    Button("Start over") { stage = .heard }
                        .buttonStyle(FilledButtonStyle(.secondary))
                }
            }
        }
    }

    /// The user's screen at this step, as the mirror would show it.
    @ViewBuilder
    private var screen: some View {
        switch stage {
        case .heard:
            Text("Finding replies…")
                .foregroundStyle(Palette.muted)
                .frame(maxWidth: .infinity, minHeight: 96)
                .card()
        case .options:
            PhraseStack(slots: Self.options, picked: nil, active: false)
        case .looking:
            PhraseStack(slots: Self.options, picked: nil, active: true)
        case .picked:
            PhraseStack(slots: Self.options, picked: "s1", active: false)
        case .confirm:
            ConfirmPanel(sentence: Self.options[0].utterance, slots: Self.confirm, picked: nil, active: true)
        case .speaking:
            SpeakingCard(text: Self.options[0].utterance)
        }
    }

    private var title: String {
        switch stage {
        case .heard: return "You speak"
        case .options: return "Replies appear"
        case .looking: return "The user looks"
        case .picked: return "A reply is picked"
        case .confirm: return "The user confirms"
        case .speaking: return "The phone speaks"
        }
    }

    private var caption: String {
        switch stage {
        case .heard:
            return "Talk normally. The phone hears your question and sends it to the laptop, which writes replies that fit it."
        case .options:
            return "The user's screen shows three replies, each beside its own flickering square, and More options for a new page. This phone shows the same screen, still. The chip says where each square sits."
        case .looking:
            return "While the squares flicker, the cards here turn a little lighter. They never flash. The user looks at the reply they want for a few seconds."
        case .picked:
            return "When the headset is sure, that card gets a yellow ring and Picked. Nothing is said yet."
        case .confirm:
            return "The user confirms with Say it, or looks at Back to choose again. Only two squares flicker now."
        case .speaking:
            return "After Say it, the phone speaks the reply aloud, then listens to you again. Stop at the bottom pauses everything at any time."
        }
    }

    private func go(to next: Stage) {
        stage = next
        if next == Stage.allCases.last { onFinish?() }
    }
}

/// Look to Speak on this phone, step by step with sample phrases: the calibration, a page of six
/// phrases and More options split over two cards, three looks to one phrase, Say it, Speaking.
/// The cards are the user's screen's own (LookScreen), drawn still: nothing here flickers.
private struct LookToSpeakPractice: View {
    var onFinish: (() -> Void)?
    @State private var stage = Stage.calibrate

    private enum Stage: Int, CaseIterable {
        case calibrate, page, firstLook, secondPage, secondLook, lastTwo, lastLook, confirm, speaking
    }

    private static let question = "Do you want to go outside?"
    private static let phrases = [
        MenuOption(label: "Go outside", utterance: "Yes, let's go outside."),
        MenuOption(label: "Not now", utterance: "Not right now."),
        MenuOption(label: "After lunch", utterance: "Maybe after lunch."),
        MenuOption(label: "Coat", utterance: "Could you get my coat first?"),
        MenuOption(label: "Tired", utterance: "I'm a bit tired today."),
        MenuOption(label: "Weather", utterance: "What is the weather like?"),
        MenuOption(label: "More options", utterance: ""),
    ]
    private static let cards = [
        UISlot(slot: "s1", role: .left, label: "Left"),
        UISlot(slot: "s2", role: .right, label: "Right"),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                MonoLabel("Practice · \(stage.rawValue + 1) of \(Stage.allCases.count)")
                Spacer()
                MonoLabel("Sample, offline")
            }
            Callout(title: title, icon: "info.circle", tint: Palette.next) {
                Text(caption)
                    .font(.subheadline)
                    .foregroundStyle(Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if stage != .calibrate {
                PartnerSaidCard(text: Self.question)
            }
            LookScreen(page: page, picked: picked,
                       speaking: stage == .speaking ? Self.phrases[0].utterance : nil,
                       stateLine: stage == .page ? StateLine(text: "Calibrated: 84 %", tone: .idle) : .empty,
                       compact: true)
                .padding(12)
                .background(Color.black, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
                .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius).strokeBorder(Palette.line, lineWidth: 1))

            HStack(spacing: 10) {
                if let previous = Stage(rawValue: stage.rawValue - 1) {
                    Button("Previous") { stage = previous }
                        .buttonStyle(FilledButtonStyle(.secondary))
                }
                Spacer()
                if let next = Stage(rawValue: stage.rawValue + 1) {
                    Button("Next") { go(to: next) }
                        .buttonStyle(FilledButtonStyle(.primary))
                } else {
                    Button("Start over") { stage = .calibrate }
                        .buttonStyle(FilledButtonStyle(.secondary))
                }
            }
        }
    }

    /// The page the server would send at this step.
    private var page: ShowPage {
        switch stage {
        case .calibrate:
            return ShowPage(listId: "calibrate", page: 1, mode: .calibrate,
                            slots: [UISlot(slot: "s1", role: .left, label: "Look here"),
                                    UISlot(slot: "s2", role: .right, label: "")],
                            text: "Look at the left card", cue: "s1")
        case .page, .firstLook:
            return halves(Self.phrases)
        case .secondPage, .secondLook:
            return halves(Array(Self.phrases.prefix(4)))
        case .lastTwo, .lastLook:
            return halves(Array(Self.phrases.prefix(2)))
        case .confirm, .speaking:
            return ShowPage(listId: "practice", page: 1, mode: .confirm,
                            slots: [UISlot(slot: "s1", role: .say, label: "Say it"),
                                    UISlot(slot: "s2", role: .back, label: "Back")],
                            text: Self.phrases[0].utterance)
        }
    }

    private func halves(_ items: [MenuOption]) -> ShowPage {
        ShowPage(listId: "practice", page: 1, mode: .halves, slots: Self.cards, options: items,
                 split: (items.count + 1) / 2)
    }

    /// The card with the white ring: the one the headset picked.
    private var picked: String? {
        switch stage {
        case .firstLook, .secondLook, .lastLook: return "s1"
        default: return nil
        }
    }

    private var title: String {
        switch stage {
        case .calibrate: return "First, the calibration"
        case .page: return "Replies appear"
        case .firstLook: return "The user looks left"
        case .secondPage: return "That half stays"
        case .secondLook: return "Left again"
        case .lastTwo: return "Two left"
        case .lastLook: return "One left"
        case .confirm: return "The user confirms"
        case .speaking: return "The phone speaks"
        }
    }

    private var caption: String {
        switch stage {
        case .calibrate:
            return "Once per session, after a still baseline: both cards flicker for about 20 seconds and the user keeps looking at the left card, marked Look here. The phone shows the score after it."
        case .page:
            return "Six replies to what you said, and More options, split over the two cards. The user reads them while the screen is still."
        case .firstLook:
            return "The cards flicker (not here: this practice is still). The user looks at the card with the reply they want. When the headset is sure, that card gets a white ring."
        case .secondPage:
            return "The left card's replies are split again over both cards."
        case .secondLook:
            return "The reply is still on the left, so the user looks left again."
        case .lastTwo:
            return "Two replies left, one on each card."
        case .lastLook:
            return "The user looks at their reply. One reply is left: it is the pick. Nothing is said yet."
        case .confirm:
            return "The reply in the middle, Say it on the left, Back on the right. Back shows all the replies again; Say it speaks."
        case .speaking:
            return "The phone speaks the reply aloud, then listens to you again. A tap anywhere on the flickering screen, or Stop, pauses everything."
        }
    }

    private func go(to next: Stage) {
        stage = next
        if next == Stage.allCases.last { onFinish?() }
    }
}
