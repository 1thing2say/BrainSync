// BrainSync on the iPhone: a suite of apps driven by c-VEP (SuiteModel: Home, Camera, Reels,
// against brainsync.server's PROTOCOL-v3), plus Voice, the native client of app.server's /ws/ui
// socket (PLAN.md section 6) that replaces the partner page on the phone. Each server owns its
// own state (PLAN rule 6); this app renders, draws the squares, plays the replies, listens to the
// partner and reports events.

import SwiftUI
import BrainSyncKit

@main
struct BrainSyncPartnerApp: App {
    @State private var model = AppModel()
    @State private var suite = SuiteModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            SuiteRootView()
                .environment(model)
                .environment(suite)
                .onOpenURL { url in open(url) }
                #if DEBUG
                .task { suite.applyDebugLaunchArguments() }
                #endif
                .preferredColorScheme(.dark)
                .tint(Palette.look)
        }
        .onChange(of: scenePhase) { _, phase in
            model.scenePhaseChanged(phase)
            suite.scenePhaseChanged(phase)
        }
    }

    /// A brainsync:// or https link opened from outside: a Voice partner link (app.server,
    /// `brainsync://pair` or `?key=`) goes to Voice, anything else that parses is a headset
    /// server link (brainsync.server, `#token=` or `brainsync://v3`).
    private func open(_ url: URL) {
        switch V3AnyLink.parse(url) {
        case .voice:
            suite.open(.voice)
            model.open(url)
        case .v3(let link):
            suite.open(link: link)
        case nil:
            break
        }
    }
}

/// Voice: the first-run setup until it is done, then pairing until there is a server link, then
/// the partner screen with the flicker warning on top until it is accepted in this run (rule 10).
struct VoiceRootView: View {
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
