// The flicker warning (PLAN rule 10, same wording as ui/index.html). Resume stays disabled until
// it is accepted, and its button is the first tap: it starts the session (resume, like the web
// page; held until connected) and asks for the microphone and speech recognition. Stop works
// without accepting.

import SwiftUI

struct WarningView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ZStack {
            Color.black.opacity(0.75).ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Flicker warning")
                        .font(.title.weight(.bold))
                    Text("The user's screen flickers at 12 to 20 Hz, mostly in the range most likely to trigger photosensitive seizures, with up to four squares at once. Nobody with photosensitive epilepsy or flicker sensitivity should look at it.")
                    Text("**Esc** on the flicker display or **Stop** here turns the flicker off immediately.")
                    Text("While the session runs, the microphone listens to the conversation partner and the iPhone turns the speech into text (on the phone itself when it supports that, otherwise through Apple's speech recognition). Press **Mute** any time.")
                    Button {
                        model.acceptWarning()
                    } label: {
                        Text("I understand, start")
                            .font(.headline)
                            .frame(maxWidth: .infinity, minHeight: 50)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Palette.say)
                    .padding(.top, 6)
                    // STOP stays reachable without accepting (and without the resume that
                    // accepting sends): the app may be relaunched while a session flickers.
                    Button(role: .destructive) {
                        model.stopPressed()
                    } label: {
                        Text(model.connected ? "Stop the flicker" : "Not connected: press Esc on the display")
                            .font(.headline)
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.bordered)
                    .tint(Palette.danger)
                    .disabled(!model.connected)
                }
                .padding(20)
                .background(Palette.panel, in: RoundedRectangle(cornerRadius: 18))
                .padding(16)
                .frame(maxHeight: .infinity)
            }
        }
        .foregroundStyle(Palette.text)
        .accessibilityAddTraits(.isModal)
    }
}
