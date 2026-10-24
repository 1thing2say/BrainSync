// Pairing: the laptop's partner page offers the app link (brainsync://pair?url=...&key=...).
// Tapping it (or AirDropping it to the phone) opens this app and pairs; pasting the link, or the
// web phone link (<base>/?key=...), works too.

import SwiftUI
import UIKit
import BrainSyncKit

struct PairView: View {
    @Environment(AppModel.self) private var model
    @State private var text = ""
    @State private var problem = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("BCI Voice partner")
                    .font(.largeTitle.weight(.bold))
                Text("Connect this phone to the BrainSync server on the laptop. Only one partner page runs the session at a time: this app replaces the laptop's or another phone's page.")
                    .foregroundStyle(Palette.muted)

                VStack(alignment: .leading, spacing: 8) {
                    Text("1. On the laptop").font(.headline)
                    Text("Start the server with `python -m app.server --tunnel` (or with server.host 0.0.0.0 on the same Wi-Fi), open the partner page and use its app link.")
                        .font(.subheadline)
                        .foregroundStyle(Palette.muted)
                    Text("2. On this phone").font(.headline)
                    Text("Tap the app link (AirDrop or Messages it to the phone), or paste the app link or the phone link here.")
                        .font(.subheadline)
                        .foregroundStyle(Palette.muted)
                }

                TextField("brainsync://pair?… or https://…/?key=…", text: $text, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .lineLimit(1...4)
                    .onSubmit(pair)

                HStack(spacing: 10) {
                    Button("Paste") {
                        if let pasted = UIPasteboard.general.string {
                            text = pasted
                            pair()
                        } else {
                            problem = "The clipboard has no text."
                        }
                    }
                    .buttonStyle(.bordered)
                    Button("Connect", action: pair)
                        .buttonStyle(.borderedProminent)
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Spacer()
                    if model.link != nil {
                        Button("Cancel") {
                            model.cancelPairing()
                        }
                    }
                }

                if !problem.isEmpty {
                    Text(problem)
                        .font(.subheadline)
                        .foregroundStyle(Palette.warn)
                }
            }
            .padding(20)
        }
        .background(Palette.bg.ignoresSafeArea())
        .foregroundStyle(Palette.text)
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
