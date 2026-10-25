// The first-run setup, one step per screen like Google's Look to Speak: what the app does,
// pairing, the flicker warning, the microphone, how the user looks (Look to Speak on this phone,
// c-VEP only), a practice round and the start (BrainSyncKit's SetupSteps holds the order). No
// step sends resume or starts anything before the last one (PLAN rule 10: the warning comes
// first). Once the phone is paired, Stop is on every step, because the app may be opened while
// the session already flickers.

import SwiftUI
import UIKit
import BrainSyncKit

struct SetupFlow: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("setupDone") private var setupDone = false
    /// The order, fixed when the setup opens: pairing halfway must not shift the steps.
    @State private var order: SetupSteps?
    @State private var step = SetupStep.welcome
    /// "I have read this" on the Safety step, in this run of the setup.
    @State private var warningRead = false
    @State private var practiced = false
    @State private var permission = MicPermission.notAsked
    @State private var asking = false

    private var steps: SetupSteps {
        order ?? SetupSteps(paired: model.link != nil, lookToSpeak: model.isCodeParadigm)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                content
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .background(Palette.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            footer
        }
        .foregroundStyle(Palette.text)
        .onAppear {
            if order == nil {
                let fixed = SetupSteps(paired: model.link != nil, lookToSpeak: model.isCodeParadigm)
                order = fixed
                step = fixed.first
            }
            permission = MicPermission.current()
        }
        .onChange(of: model.isCodeParadigm) { _, code in
            // Paired during the setup with a c-VEP server: the looking step comes in after the
            // microphone; the steps before it keep their places.
            if code, let current = order, !current.lookToSpeak {
                order = current.withLookToSpeak()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { permission = MicPermission.current() }  // back from iOS Settings
        }
    }

    // MARK: Frame

    private var header: some View {
        let progress = steps.progress(of: step) ?? SetupSteps.Progress(index: 0, count: steps.count)
        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                if let previous = steps.back(before: step) {
                    Button {
                        step = previous
                    } label: {
                        Label("Back", systemImage: "chevron.left")
                            .font(.subheadline)
                            .foregroundStyle(Palette.muted)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                Spacer()
                MonoLabel("Step \(progress.index + 1) of \(progress.count)")
            }
            .frame(minHeight: 32)
            StepIndicator(index: progress.index, count: progress.count)
        }
        .padding(.horizontal, 20)
        .padding(.top, 8)
        .padding(.bottom, 12)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Palette.line).frame(height: 1)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch step {
        case .welcome:
            WelcomeStep()
        case .pair:
            PairStep()
        case .safety:
            SafetyStep(read: $warningRead, live: model.accepted)
        case .microphone:
            MicrophoneStep(permission: permission, asking: asking, live: model.accepted,
                           ask: askPermission, openSettings: openSettings)
        case .looking:
            LookingStep()
        case .practice:
            PracticeView(lookToSpeak: model.isCodeParadigm) { practiced = true }
        case .done:
            DoneStep()
        }
    }

    private var footer: some View {
        VStack(spacing: 10) {
            if step == .done {
                Button(action: start) {
                    Text("Start the session").frame(maxWidth: .infinity)
                }
                .buttonStyle(FilledButtonStyle(.primary, large: true))
                .disabled(!warningRead)
            } else {
                Button(action: advance) {
                    Text(continueTitle).frame(maxWidth: .infinity)
                }
                .buttonStyle(FilledButtonStyle(.primary, large: true))
                .disabled(!canContinue)
            }
            if model.link != nil {
                StopButton(title: "Stop the flicker")
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 12)
        .padding(.bottom, 8)
        .background(Palette.bg)
        .overlay(alignment: .top) {
            Rectangle().fill(Palette.line).frame(height: 1)
        }
    }

    // MARK: Steps

    private var continueTitle: String {
        switch step {
        case .welcome: return "Get started"
        case .practice: return practiced ? "Continue" : "Skip the practice"
        default: return "Continue"
        }
    }

    private var canContinue: Bool {
        switch step {
        case .pair: return model.link != nil && model.stopReason == nil  // not a refused link
        case .safety: return warningRead
        default: return true
        }
    }

    private func advance() {
        guard canContinue, let next = steps.next(after: step) else { return }
        step = next
    }

    /// The only place the setup starts anything. The warning was read in this run of the app, so
    /// this is the same tap as the warning's "I understand, start": resume, and the mic prompts.
    private func start() {
        guard warningRead else { return }
        model.acceptWarning()
        setupDone = true
    }

    /// Only the iOS prompts: the mic opens when the server asks for it, after the start.
    private func askPermission() {
        asking = true
        Task { @MainActor in
            await model.listener.askPermissions()
            permission = MicPermission.current()
            asking = false
        }
    }

    private func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) {
            openURL(url)
        }
    }
}

/// One thin bar per step: done and current ones lit, steady.
struct StepIndicator: View {
    let index: Int
    let count: Int

    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<count, id: \.self) { i in
                Capsule()
                    .fill(i <= index ? Palette.text : Palette.lineStrong)
                    .frame(height: 3)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Step \(index + 1) of \(count)")
    }
}

// MARK: - The steps

private struct WelcomeStep: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            MonoLabel("BCI Voice partner")
            Heading("What this app does", font: .largeTitle.weight(.bold))
            Text("You are the conversation partner. This phone is your side of the conversation; setting it up takes a minute.")
                .foregroundStyle(Palette.muted)
            if model.isCodeParadigm {
                FeatureRow(icon: "eye", title: "The user picks replies with their eyes",
                           text: "This phone becomes the user's screen, like Look to Speak: two cards, each with half of the replies. The user looks at one card, then the other, until one reply is left, then confirms it.")
                FeatureRow(icon: "person.2", title: "Your tools stay one tap away",
                           text: "The Partner button opens the conversation, typing and the mic, with a still copy of the user's screen.")
            } else {
                FeatureRow(icon: "eye", title: "The user picks replies with their eyes",
                           text: "On the laptop's flicker screen each reply sits beside its own flickering square. The user looks at the one they want, then confirms it.")
                FeatureRow(icon: "iphone", title: "This phone shows what they see",
                           text: "A still copy of the user's screen: the replies on offer and the one picked. Nothing on this phone flickers.")
            }
            FeatureRow(icon: "ear", title: "It listens to you",
                       text: "Speak normally. What you say becomes text, and new replies are written for it.")
            FeatureRow(icon: "speaker.wave.2", title: "It plays the replies",
                       text: "A reply is spoken aloud only after the user confirms it.")
        }
    }
}

private struct FeatureRow: View {
    let icon: String
    let title: String
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon)
                .font(.body)
                .foregroundStyle(Palette.text)
                .frame(width: 36, height: 36)
                .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.radius))
                .overlay(RoundedRectangle(cornerRadius: Palette.radius).strokeBorder(Palette.line, lineWidth: 1))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(text)
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct PairStep: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Heading("Connect to the laptop", font: .largeTitle.weight(.bold))
            Text("The laptop runs the BrainSync server. This phone needs its address and access key, which come in the app link.")
                .foregroundStyle(Palette.muted)
            if let link = model.link {
                HStack(spacing: 8) {
                    Circle()
                        .fill(dot)
                        .frame(width: 8, height: 8)
                    Text(note)
                        .font(.subheadline.weight(.semibold))
                    Text(link.base.host ?? link.base.absoluteString)
                        .font(.caption.monospaced())
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .card(padding: 12)
            }
            PairForm(showsCancel: false)
        }
    }

    /// A refused link (expired key, another partner page took over) never reconnects by itself,
    /// so it says why, in red, and the form below stays for a new link.
    private var note: String {
        if model.connected { return "Connected" }
        return model.stopReason != nil ? model.connectionNote : "Paired, connecting…"
    }

    private var dot: Color {
        if model.connected { return Palette.say }
        return model.stopReason != nil ? Palette.danger : Palette.warn
    }
}

private struct SafetyStep: View {
    @Binding var read: Bool
    /// The warning was accepted earlier in this run (setup shown again from Settings), so the
    /// session may be running and the laptop flickering right now.
    let live: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Heading("Flicker warning", font: .largeTitle.weight(.bold))
            Callout(title: "Warning", icon: "exclamationmark.triangle.fill", tint: Palette.warn) {
                FlickerWarningText()
            }
            Toggle(isOn: $read) {
                Text("I have read this warning")
                    .font(.headline)
            }
            .tint(Palette.say)
            .padding(14)
            .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.radius))
            .overlay(RoundedRectangle(cornerRadius: Palette.radius)
                .strokeBorder(read ? Palette.say.opacity(0.5) : Palette.line, lineWidth: 1))
            Text(live ? "The session may already be running: Stop below turns the flicker off."
                      : "Nothing starts yet: the session starts at the end of this setup.")
                .font(.footnote)
                .foregroundStyle(Palette.muted)
        }
    }
}

private struct MicrophoneStep: View {
    let permission: MicPermission
    let asking: Bool
    /// As on the Safety step: the session may already be running.
    let live: Bool
    let ask: () -> Void
    let openSettings: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Heading("The microphone", font: .largeTitle.weight(.bold))
            Text("While the session runs, this phone listens to you and turns what you say into text, on the phone itself when it can, otherwise through Apple's speech recognition. The user's replies are written for what you said.")
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 12) {
                FeatureRow(icon: "speaker.slash", title: "Off while a reply plays",
                           text: "So the phone never hears itself.")
                FeatureRow(icon: "mic.slash", title: "Mute any time",
                           text: "Mute on the main screen turns it off. Typing always works.")
            }
            PermissionRow(permission: permission)
                .card(padding: 12)
            switch permission {
            case .notAsked:
                Button(action: ask) {
                    Text(asking ? "Asking…" : "Allow microphone and speech recognition")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(FilledButtonStyle(.secondary))
                .disabled(asking)
                Text(live ? "This only asks iOS for access. The mic opens when the server asks for it."
                          : "This only asks iOS for access. The phone starts listening once the session starts.")
                    .font(.footnote)
                    .foregroundStyle(Palette.muted)
            case .denied:
                Button(action: openSettings) {
                    Text("Open iOS Settings").frame(maxWidth: .infinity)
                }
                .buttonStyle(FilledButtonStyle(.secondary))
            case .granted:
                EmptyView()
            }
        }
    }
}

private struct DoneStep: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Heading("Ready", font: .largeTitle.weight(.bold))
            Text(model.isCodeParadigm
                 ? "Start the session when the user is ready, the headset is on and the phone is in front of them. The screen stays still for a short baseline, then the calibration (about 20 seconds on the left card), then the first replies."
                 : "Start the session when the user is ready and the headset is on. The user's screen stays still for a short baseline, then shows the first replies.")
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
            if model.isCodeParadigm {
                FeatureRow(icon: "hand.tap", title: "Tap anywhere to stop the flicker",
                           text: "A tap on the flickering screen, or the red Stop at the bottom, turns it off at once and pauses.")
            } else {
                FeatureRow(icon: "hand.raised", title: "Stop is always at the bottom",
                           text: "It turns the flicker off and pauses. Esc on the flicker display does the same.")
            }
            FeatureRow(icon: "mic.slash", title: "Mute and typing",
                       text: "Mute turns the mic off; typing always works.")
            FeatureRow(icon: "gearshape", title: "Settings",
                       text: "The gear: text size, haptics, the speaker test, the server and this setup.")
            if !model.connected {
                Callout(title: "Not connected", icon: "info.circle", tint: Palette.next) {
                    Text("The phone is not connected to the laptop right now. If the session has not started once it connects, press Resume.")
                        .font(.subheadline)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// Look to Speak on this phone (c-VEP): how the user picks, and the calibration that comes first.
private struct LookingStep: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Heading("How the user picks", font: .largeTitle.weight(.bold))
            Text("This phone is the user's screen. Hold it in front of them, in portrait, at reading distance, with the brightness up.")
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
            FeatureRow(icon: "rectangle.split.2x1", title: "Look at the left or right card",
                       text: "The replies are split between two cards. The user looks at the card that holds the reply they want for a few seconds; that half stays and is split again, until one reply is left.")
            FeatureRow(icon: "checkmark.circle", title: "Then confirm",
                       text: "Say it (left) speaks the reply; Back (right) shows all the replies again. Nothing is spoken without Say it.")
            FeatureRow(icon: "scope", title: "First, a 20-second calibration",
                       text: "After the start and a still baseline, both cards flicker for about 20 seconds while the user keeps looking at the left card (Look here). It teaches the headset this user's response; the score shows when it is done, 70 % or more is good. Settings > Recalibrate runs it again.")
            FeatureRow(icon: "hand.tap", title: "Tap anywhere to stop",
                       text: "A tap on the flickering screen turns it off at once and pauses. It also stops when the app leaves the screen.")
            Callout(title: "60 Hz", icon: "info.circle", tint: Palette.next) {
                Text("The app runs the flicker at exactly 60 frames a second, also on ProMotion iPhones. If the phone cannot hold it (Low Power Mode, a hot phone), the screen says so and does not flicker.")
                    .font(.subheadline)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
