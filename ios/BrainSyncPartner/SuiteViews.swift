// The suite's screens: Home (pair, the flicker warning, the app tiles), the shared top bar with
// Stop, the three c-VEP squares with their labels and confidence, and the Camera and Reels apps.
// Voice is the existing partner screen (VoiceRootView), unchanged. Every app shows the same three
// squares (slots t0, t1, t2); SuiteModel turns the server's pick into the app's effect.

import Photos
import SwiftUI
import UIKit
import BrainSyncKit

/// The suite's root: the app the user is in.
struct SuiteRootView: View {
    @Environment(SuiteModel.self) private var suite

    var body: some View {
        Group {
            switch suite.app {
            case .home: SuiteHomeView()
            case .camera: CameraAppView()
            case .reels: ReelsAppView()
            case .voice:
                VoiceRootView()
                    .safeAreaInset(edge: .top, spacing: 0) {
                        HStack {
                            HomeButton()
                            Spacer()
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(Palette.bg)
                    }
            }
        }
        .preferredColorScheme(.dark)
        // A headset-server link from outside the app to another server: switch only on Pair.
        .alert("Pair with \(suite.pendingLink?.hostLabel ?? "")?",
               isPresented: Binding(get: { suite.pendingLink != nil },
                                    set: { if !$0 { suite.cancelPending() } }),
               presenting: suite.pendingLink) { newLink in
            Button("Pair") { suite.pair(with: newLink) }
            Button("Cancel", role: .cancel) { suite.cancelPending() }
        } message: { _ in
            Text("This replaces \(suite.link?.hostLabel ?? "the saved server") as the headset server.")
        }
    }
}

// MARK: - Shared pieces

/// Back to Home, by touch (the operator's hands; the apps' three squares are all in use).
struct HomeButton: View {
    @Environment(SuiteModel.self) private var suite

    var body: some View {
        Button {
            suite.goHome()
        } label: {
            Label("Home", systemImage: "square.grid.2x2")
                .font(.subheadline.weight(.semibold))
        }
        .buttonStyle(FilledButtonStyle(.secondary))
        .accessibilityHint("Leaves this app for the suite's home screen")
    }
}

/// Top bar of every c-VEP app: Home, the app's name, Stop / Resume, and the status line.
struct SuiteTopBar: View {
    @Environment(SuiteModel.self) private var suite
    let title: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                if suite.app != .home { HomeButton() }
                Text(title)
                    .font(.headline)
                Spacer()
                SuiteStopButton()
            }
            SuiteStatusLine()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Palette.bg)
    }
}

/// The connection or the decoder in one mono line (the standard's status line while it runs:
/// training progress, then the decoder's lead and choice); red while something is wrong.
struct SuiteStatusLine: View {
    @Environment(SuiteModel.self) private var suite

    var body: some View {
        let (text, bad) = line
        Text(text)
            .font(.caption.monospaced())
            .foregroundStyle(bad ? Palette.danger : Palette.muted)
            .lineLimit(2)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var line: (String, Bool) {
        if let problem = suite.driver.refreshProblem { return (problem, true) }
        if !suite.note.isEmpty { return (suite.note, suite.stopReason != nil) }
        if suite.link == nil { return ("Not paired with a BrainSync server", false) }
        if !suite.accepted { return ("Read the flicker warning on Home to start", false) }
        if suite.checkingRefresh { return ("Checking the screen runs at 60 Hz…", false) }
        if suite.paused { return ("Stopped. Resume when ready.", false) }
        if let error = suite.serverError { return (error.message.isEmpty ? error.code : error.message, true) }
        if let status = suite.status, !status.ok { return (status.text, true) }
        var text = suite.statusLine
        if suite.welcome?.fake == true {
            text += text.isEmpty ? "fake board: tap a square to look at it" : "   · fake board"
        }
        return (text, false)
    }
}

/// Stop while the session runs, Resume after it. Always reachable (rule 10).
struct SuiteStopButton: View {
    @Environment(SuiteModel.self) private var suite

    var body: some View {
        if suite.paused {
            Button("Resume") { suite.resumePressed() }
                .buttonStyle(FilledButtonStyle(.primary))
        } else {
            Button("Stop") { suite.stopPressed() }
                .buttonStyle(FilledButtonStyle(.danger))
                .accessibilityHint("Turns the flicker off at once and pauses decoding")
        }
    }
}

/// One c-VEP target: the flickering square (painted by the driver, never by SwiftUI), the cue box
/// while training, a ring on the square just picked, and under it the label and the standard's
/// confidence meter (`███░░░░░ +0.42 <`).
struct SuiteTarget: View {
    @Environment(SuiteModel.self) private var suite
    let index: Int
    let size: CGFloat

    var body: some View {
        let slot = SuiteSlots.ids[index]
        let enabled = index < suite.enabled.count ? suite.enabled[index] : true
        let label = index < suite.labels.count ? suite.labels[index] : slot
        VStack(spacing: 6) {
            CodeFlickerSquare(slot: slot, lag: suite.lag(of: index), driver: suite.driver)
                .frame(width: size, height: size)
                // Rings sit outside the square, so its own light never changes.
                .overlay { ring(slot) }
                .contentShape(Rectangle())
                .onTapGesture { suite.tapSquare(slot) }
            Text(label.isEmpty ? " " : label)
                .font(.headline)
                .foregroundStyle(enabled ? Palette.text : Palette.muted)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(meter(slot))
                .font(.caption2.monospaced())
                .foregroundStyle(Palette.muted)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label.isEmpty ? "Empty" : label) square")
    }

    @ViewBuilder
    private func ring(_ slot: String) -> some View {
        if suite.cueSlot == slot {
            RoundedRectangle(cornerRadius: 4).strokeBorder(Palette.look, lineWidth: 4).padding(-9)
        } else if suite.pickedSlot == slot {
            RoundedRectangle(cornerRadius: 4).strokeBorder(Palette.pick, lineWidth: 4).padding(-9)
        } else if suite.gazeSlot == slot {
            RoundedRectangle(cornerRadius: 4)
                .strokeBorder(Palette.muted, style: StrokeStyle(lineWidth: 2, dash: [4, 4]))
                .padding(-7)
        }
    }

    private func meter(_ slot: String) -> String {
        guard let score = suite.score(of: slot) else { return " " }
        return CodeVep.bar(score) + " " + CodeVep.scoreText(score) + (suite.isBest(slot) ? " <" : "  ")
    }
}

/// The three targets in a row, as large as the width allows (Home and Camera).
struct SuiteTargetRow: View {
    var maxSize: CGFloat = 112

    var body: some View {
        GeometryReader { geo in
            let size = max(40, min(maxSize, (geo.size.width - 4 * 24) / 3, geo.size.height - 70))
            HStack(spacing: 24) {
                ForEach(0..<SuiteSlots.count, id: \.self) { i in
                    SuiteTarget(index: i, size: size)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// The three targets stacked (Reels' right rail): Up on top, Like in the middle, Down below.
struct SuiteTargetColumn: View {
    var size: CGFloat = 76

    var body: some View {
        VStack(spacing: 28) {
            ForEach(0..<SuiteSlots.count, id: \.self) { i in
                SuiteTarget(index: i, size: size)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Home

struct SuiteHomeView: View {
    @Environment(SuiteModel.self) private var suite
    @State private var showSettings = false

    var body: some View {
        VStack(spacing: 0) {
            SuiteTopBar(title: "BrainSync")
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if suite.link == nil {
                        SuitePairCard()
                    } else if !suite.accepted {
                        SuiteWarningCard()
                    }
                    if suite.accepted && suite.driver.refreshProblem != nil && !suite.checkingRefresh {
                        Button("Check the screen again") { suite.checkRefreshThenConnect() }
                            .buttonStyle(FilledButtonStyle(.secondary))
                    }
                    if suite.stopReason == .busy {
                        Button("Take over the headset") { suite.connect(takeover: true) }
                            .buttonStyle(FilledButtonStyle(.secondary))
                    } else if suite.stopReason != nil {
                        Button("Pair again") { suite.forgetLink() }
                            .buttonStyle(FilledButtonStyle(.secondary))
                    }
                    MonoLabel("Apps")
                    HStack(spacing: 12) {
                        ForEach(SuiteHomeCore.apps, id: \.self) { app in
                            AppTile(app: app)
                        }
                    }
                    Text("Look at a square below to open its app, or tap a tile. Inside an app the three squares do that app's actions; Home goes back.")
                        .font(.footnote)
                        .foregroundStyle(Palette.muted)
                    Button("Settings") { showSettings = true }
                        .buttonStyle(FilledButtonStyle(.secondary))
                }
                .padding(16)
            }
            if suite.accepted {
                SuiteTargetRow()
                    .frame(height: 200)
                    .padding(.bottom, 8)
                    .background(Palette.bg)
            }
        }
        .background(Palette.bg.ignoresSafeArea())
        .foregroundStyle(Palette.text)
        // The squares are covered while Settings is up, and its Flicker lab draws the same code:
        // hold the session paused so no look there counts as a Home pick.
        .sheet(isPresented: $showSettings) {
            SuiteSettingsView()
                .onAppear { suite.hold("settings", true) }
                .onDisappear { suite.hold("settings", false) }
        }
    }
}

private struct AppTile: View {
    @Environment(SuiteModel.self) private var suite
    let app: SuiteApp

    var body: some View {
        Button {
            suite.open(app)
        } label: {
            VStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.title)
                Text(app.title)
                    .font(.subheadline.weight(.semibold))
            }
            .frame(maxWidth: .infinity, minHeight: 84)
        }
        .buttonStyle(FilledButtonStyle(.secondary))
    }

    private var icon: String {
        switch app {
        case .camera: return "camera"
        case .reels: return "play.rectangle.on.rectangle"
        case .voice: return "waveform"
        case .home: return "square.grid.2x2"
        }
    }
}

/// Pair with the BrainSync server (brainsync.server prints `http://…:8765/#token=…`; the Jetson's
/// tunnel link is in ~/brainsync-link.txt).
struct SuitePairCard: View {
    @Environment(SuiteModel.self) private var suite
    @State private var text = ""
    @State private var problem = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Heading("Connect to the headset server", font: .title2.weight(.bold))
            Text("Start `python -m brainsync.server` (c-VEP is its default) on the Mac or the Jetson, then paste the link it prints, `http://…:8765/#token=…`, or the tunnel link.")
                .font(.subheadline)
                .foregroundStyle(Palette.muted)
            TextField("http://192.168.1.20:8765/#token=…", text: $text, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.callout.monospaced())
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .lineLimit(1...3)
                .padding(10)
                .background(Palette.surface, in: RoundedRectangle(cornerRadius: Palette.radius))
            if !problem.isEmpty {
                Text(problem).font(.footnote).foregroundStyle(Palette.danger)
            }
            HStack {
                Button("Paste") {
                    if let s = UIPasteboard.general.string { text = s }
                }
                .buttonStyle(FilledButtonStyle(.secondary))
                Button("Pair") {
                    problem = suite.pair(text: text.trimmingCharacters(in: .whitespacesAndNewlines))
                        ? "" : "That is not a BrainSync server link."
                }
                .buttonStyle(FilledButtonStyle(.primary))
                .disabled(text.isEmpty)
            }
        }
        .card()
    }
}

/// The flicker warning (rule 10) for the c-VEP squares; its button starts the session.
struct SuiteWarningCard: View {
    @Environment(SuiteModel.self) private var suite

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Callout(title: "Flicker warning", icon: "exclamationmark.triangle.fill", tint: Palette.warn) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("The three squares flash black and white in a code, changing up to 60 times a second. Flashing in this range can trigger seizures in people with photosensitive epilepsy. Nobody with epilepsy or flicker sensitivity should look at them.")
                    Text("**Stop** turns the flicker off at once, and it stops by itself whenever the app leaves the screen.")
                }
                .fixedSize(horizontal: false, vertical: true)
            }
            Button {
                suite.acceptWarning()
            } label: {
                Text("I understand, start").frame(maxWidth: .infinity)
            }
            .buttonStyle(FilledButtonStyle(.primary, large: true))
            Text("Server: \(suite.link?.hostLabel ?? "")")
                .font(.footnote)
                .foregroundStyle(Palette.muted)
        }
        .card()
    }
}

// MARK: - Camera

struct CameraAppView: View {
    @Environment(SuiteModel.self) private var suite

    var body: some View {
        VStack(spacing: 0) {
            SuiteTopBar(title: "Camera")
            switch suite.camera.screen {
            case .viewfinder:
                CameraViewfinder(capture: suite.capture, store: suite.photos,
                                 shutterTrigger: suite.shutterTrigger, stripMinHeight: 200) {
                    SuiteTargetRow()
                }
            case .gallery, .confirmDelete:
                CameraGalleryView(store: suite.photos, index: suite.camera.photoIndex,
                                  confirmingDelete: suite.camera.screen == .confirmDelete,
                                  stripMinHeight: 200) {
                    SuiteTargetRow()
                }
            }
        }
        .background(Palette.bg.ignoresSafeArea())
        .foregroundStyle(Palette.text)
    }
}

// MARK: - Reels

struct ReelsAppView: View {
    @Environment(SuiteModel.self) private var suite

    var body: some View {
        VStack(spacing: 0) {
            SuiteTopBar(title: "Reels")
            ReelFeedView(index: suite.reels.index, liked: suite.reels.liked, paused: suite.reels.paused,
                         dimmed: suite.dimReels && suite.driver.isFlickering,
                         topBumps: suite.reelTopBumps, railWidth: 124) {
                SuiteTargetColumn()
            }
        }
        .background(Palette.bg.ignoresSafeArea())
        .foregroundStyle(Palette.text)
    }
}

// MARK: - Settings

struct SuiteSettingsView: View {
    @Environment(SuiteModel.self) private var suite
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var suite = suite
        @Bindable var photos = suite.photos
        NavigationStack {
            Form {
                Section("Headset server") {
                    Text(suite.link?.hostLabel ?? "Not paired")
                        .font(.callout.monospaced())
                    if suite.link != nil {
                        Button("Forget this server", role: .destructive) { suite.forgetLink() }
                    }
                    if let hz = suite.driver.measuredHz {
                        Text("Screen: \(String(format: "%.2f", hz)) Hz, \(suite.driver.dropped) late frames")
                            .font(.footnote)
                            .foregroundStyle(Palette.muted)
                    }
                }
                Section {
                    Toggle("Dim the reel while choosing", isOn: $suite.dimReels)
                } footer: {
                    Text("Moving pictures next to the squares weaken the brain's response to them.")
                }
                Section {
                    Toggle("Also save photos to Photos", isOn: $photos.savesToPhotoLibrary)
                        .onChange(of: photos.savesToPhotoLibrary) { _, on in
                            // Ask now, while nothing flickers: a prompt at the first photo would
                            // make the app inactive mid-session and pause it.
                            if on { PHPhotoLibrary.requestAuthorization(for: .addOnly) { _ in } }
                        }
                }
                Section {
                    NavigationLink("Flicker lab") { FlickerLabView() }
                } footer: {
                    Text("Three squares with the measured refresh, late frames and cycle times, to check the phone's timing.")
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}
