// BrainSync partner app: a native client of the server's /ws/ui socket (PLAN.md section 6). It
// replaces the partner page on the phone. The server owns all state (PLAN rule 6); this app
// renders, plays the replies, listens to the partner and reports events.

import SwiftUI
import BrainSyncKit

@main
struct BrainSyncPartnerApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .onOpenURL { url in model.open(url) }
                .preferredColorScheme(.dark)
                .tint(Palette.look)
        }
        .onChange(of: scenePhase) { _, phase in
            model.scenePhaseChanged(phase)
        }
    }
}

/// The first-run setup until it is done, then pairing until there is a server link, then the
/// main screen with the flicker warning on top until it is accepted in this run (rule 10). The
/// main screen is the user's Look to Speak screen when the server sent a c-VEP `stimulus` (this
/// phone flickers), otherwise the partner screen (the laptop flickers, the default config).
struct RootView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("setupDone") private var setupDone = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            if !setupDone {
                SetupFlow()
            } else if model.link == nil || model.showPairing {
                PairView()
            } else if model.isCodeParadigm {
                LookToSpeakView()
                    .overlay {
                        if !model.accepted {
                            WarningView()
                        }
                    }
            } else {
                PartnerView()
                    .overlay {
                        if !model.accepted {
                            WarningView()
                        }
                    }
            }
        }
        .animation(reduceMotion ? nil : .default, value: model.accepted)
        // A link from outside the app to another server: switch only on Pair (AppModel.open).
        .alert("Pair with \(model.pendingLink?.hostLabel ?? "")?",
               isPresented: Binding(get: { model.pendingLink != nil },
                                    set: { if !$0 { model.cancelPending() } }),
               presenting: model.pendingLink) { newLink in
            Button("Pair") { model.pair(with: newLink) }
            Button("Cancel", role: .cancel) { model.cancelPending() }
        } message: { _ in
            Text("This replaces \(model.link?.hostLabel ?? "the saved server").")
        }
    }
}
