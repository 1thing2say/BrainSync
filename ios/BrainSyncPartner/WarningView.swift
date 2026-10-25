// The flicker warning (PLAN rule 10, same wording as ui/index.html). Resume stays disabled until
// it is accepted, and its button is the first tap: it starts the session (resume, like the web
// page; held until connected) and asks for the microphone and speech recognition. Stop works
// without accepting. With a c-VEP server it is this phone that flickers (full-contrast black and
// white, INTERFACE-v2-draft.md section 9), and the warning says so; nothing on the phone flickers
// before it is accepted.

import SwiftUI

struct WarningView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ZStack {
            Palette.bg.opacity(0.85).ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Heading("Flicker warning")
                    Callout(title: "Warning", icon: "exclamationmark.triangle.fill", tint: Palette.warn) {
                        FlickerWarningText()
                    }
                    Button {
                        model.acceptWarning()
                    } label: {
                        Text("I understand, start")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(FilledButtonStyle(.primary, large: true))
                    .padding(.top, 4)
                    // STOP stays reachable without accepting (and without the resume that
                    // accepting sends): the app may be relaunched while a session flickers.
                    StopButton(title: "Stop the flicker")
                }
                .padding(20)
                .background(Palette.panel, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
                .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius)
                    .strokeBorder(Palette.line, lineWidth: 1))
                .padding(16)
                .frame(maxHeight: .infinity)
            }
        }
        .foregroundStyle(Palette.text)
        .accessibilityAddTraits(.isModal)
    }
}

/// The warning's words, shared with the setup's Safety step. Before the server has said which
/// kind of session it runs (the setup, before pairing), it warns about both.
struct FlickerWarningText: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.isCodeParadigm {
                Text("**This phone's screen flickers.** It becomes the user's screen: two large cards that flash in a full-contrast black and white pattern (c-VEP), an irregular on/off up to 30 times a second, both cards at once. Flashing like this can trigger seizures. Nobody with photosensitive epilepsy or flicker sensitivity should look at it.")
                Text("**Tap anywhere** on the flickering screen, or **Stop**, turns it off at once. It also stops when the app leaves the screen or loses the laptop.")
            } else {
                Text("The user's screen flickers at 11 to 17 Hz, mostly in the range most likely to trigger photosensitive seizures, with up to four squares at once. Nobody with photosensitive epilepsy or flicker sensitivity should look at it.")
                Text("**Esc** on the flicker display or **Stop** here turns the flicker off immediately.")
                Text("If the laptop runs Look to Speak on the phone (c-VEP), this phone's own screen flickers instead: two cards flashing in a full-contrast black and white pattern. **Tap anywhere** on it to stop.")
            }
            Text("While the session runs, the microphone listens to the conversation partner and the iPhone turns the speech into text (on the phone itself when it supports that, otherwise through Apple's speech recognition). Press **Mute** any time.")
        }
        .foregroundStyle(Palette.text)
        .fixedSize(horizontal: false, vertical: true)
    }
}
