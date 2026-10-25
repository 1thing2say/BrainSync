// A practice round for the partner, offline and with sample data: no server, nothing sent,
// nothing spoken. It walks through one reply with the same cards as the mirror, one step per tap
// of Next (no timers), with a caption for each step. The highlight is steady, never blinking.

import SwiftUI
import BrainSyncKit

struct PracticeView: View {
    /// Called once the walk-through reaches its last step.
    var onFinish: (() -> Void)?

    @AppStorage("textSize") private var textSize: TextSize = .standard
    @State private var stage = Stage.heard

    init(onFinish: (() -> Void)? = nil) {
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
