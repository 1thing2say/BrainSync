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
/// partner screen with the flicker warning on top until it is accepted in this run (rule 10).
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
    }
}
