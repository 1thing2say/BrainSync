// Pairing: the laptop's partner page offers the app link (brainsync://pair?url=...&key=...).
// Tapping it (or AirDropping it to the phone) opens this app and pairs; pasting the link, or the
// web phone link (<base>/?key=...), works too. The form is shared by the setup's Pair step and
// this screen ("Pair again", a link that expired).

import SwiftUI
import UIKit
import BrainSyncKit

struct PairView: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                MonoLabel("BCI Voice partner")
                Heading("Connect to the laptop", font: .largeTitle.weight(.bold))
                Text("Connect this phone to the BrainSync server on the laptop. Only one partner page runs the session at a time: this app replaces the laptop's or another phone's page.")
                    .foregroundStyle(Palette.muted)
                PairForm()
            }
            .padding(20)
        }
        .background(Palette.bg.ignoresSafeArea())
        .foregroundStyle(Palette.text)
    }
}

/// The two steps and the link field. `showsCancel`: offer going back to the saved server.
struct PairForm: View {
    @Environment(AppModel.self) private var model
    var showsCancel = true
    @State private var text = ""
    @State private var problem = ""
    @FocusState private var focused: Bool

    init(showsCancel: Bool = true) {
        self.showsCancel = showsCancel
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 8) {
                MonoLabel("1 · On the laptop")
                Text("Start the server with `python -m app.server --tunnel` (or with server.host 0.0.0.0 on the same Wi-Fi), open the partner page and use its app link.")
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
                MonoLabel("2 · On this phone")
                    .padding(.top, 4)
                Text("Tap the app link (AirDrop or Messages it to the phone), or paste the app link or the phone link here.")
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
            }

            TextField("brainsync://pair?… or https://…/?key=…", text: $text, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.callout.monospaced())
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .lineLimit(1...4)
                .focused($focused)
                .onSubmit(pair)
                .padding(12)
                .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.radius))
                .overlay(RoundedRectangle(cornerRadius: Palette.radius)
                    .strokeBorder(focused ? Palette.look : Palette.line, lineWidth: 1))

            HStack(spacing: 10) {
                Button("Paste") {
                    if let pasted = UIPasteboard.general.string {
                        text = pasted
                        pair()
                    } else {
                        problem = "The clipboard has no text."
                    }
                }
                .buttonStyle(FilledButtonStyle(.secondary))
                Button("Connect", action: pair)
                    .buttonStyle(FilledButtonStyle(.primary))
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Spacer()
                if showsCancel && model.link != nil {
                    Button("Cancel") {
                        model.cancelPairing()
                    }
                    .buttonStyle(FilledButtonStyle(.secondary))
                }
            }

            if !problem.isEmpty {
                Text(problem)
                    .font(.subheadline)
                    .foregroundStyle(Palette.warn)
            }
        }
    }

    private func pair() {
        if model.pair(text: text) {
            problem = ""
            text = ""
        } else {
            problem = "That is not a BrainSync link. It looks like brainsync://pair?url=…&key=… or https://….trycloudflare.com/?key=…"
        }
    }
}
