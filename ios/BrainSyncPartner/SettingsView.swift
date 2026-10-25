// Settings that stay on this phone (UserDefaults): text size, haptics, a speaker check, the
// microphone permission, the server and the setup. Mute and the session itself are held by the
// server (PLAN rule 6), so they stay on the main screen; Stop is on this sheet too.

import SwiftUI
import UIKit
import BrainSyncKit

/// How big the phrase cards' text is, on top of the system's Dynamic Type: a lower bound, so a
/// larger system setting still wins.
enum TextSize: String, CaseIterable, Identifiable {
    case standard, large, extraLarge

    var id: String { rawValue }

    var title: String {
        switch self {
        case .standard: return "Standard"
        case .large: return "Large"
        case .extraLarge: return "Extra large"
        }
    }

    var range: PartialRangeFrom<DynamicTypeSize> {
        switch self {
        case .standard: return DynamicTypeSize.xSmall...
        case .large: return DynamicTypeSize.xxLarge...
        case .extraLarge: return DynamicTypeSize.accessibility2...
        }
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("textSize") private var textSize: TextSize = .standard
    @AppStorage("hapticPick") private var hapticPick = true
    @AppStorage("hapticSpeak") private var hapticSpeak = true
    @AppStorage("setupDone") private var setupDone = true
    @State private var permission = MicPermission.notAsked

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Text size", selection: $textSize) {
                        ForEach(TextSize.allCases) { size in
                            Text(size.title).tag(size)
                        }
                    }
                    .pickerStyle(.segmented)
                    Text("Yes, let's go outside.")
                        .font(.title2.weight(.semibold))
                        .dynamicTypeSize(textSize.range)
                        .padding(.vertical, 4)
                } header: {
                    MonoLabel("Phrase text")
                } footer: {
                    Text("The reply cards on the main screen. A larger iOS text size still wins.")
                }
                .listRowBackground(Palette.panel)

                Section {
                    Toggle("Vibrate when a reply is picked", isOn: $hapticPick)
                    Toggle("Vibrate when a reply is spoken", isOn: $hapticSpeak)
                } header: {
                    MonoLabel("Haptics")
                }
                .tint(Palette.say)
                .listRowBackground(Palette.panel)

                Section {
                    Button("Test the speaker") { model.testSpeaker() }
                        .disabled(!model.canTestSpeaker)
                } header: {
                    MonoLabel("Speaker")
                } footer: {
                    Text("Says a test sentence with the phone's own voice. It works while the mic is off (before the session starts, paused or muted), so the mic never hears it as you.")
                }
                .listRowBackground(Palette.panel)

                Section {
                    PermissionRow(permission: permission)
                    if permission == .denied {
                        Button("Open iOS Settings", action: openSettings)
                    }
                } header: {
                    MonoLabel("Microphone")
                } footer: {
                    Text("The microphone and speech recognition turn what you say into text. Typing always works.")
                }
                .listRowBackground(Palette.panel)

                if model.isCodeParadigm {
                    LookToSpeakSettings()
                        .listRowBackground(Palette.panel)
                }

                Section {
                    if let link = model.link {
                        LabeledContent("Server") {
                            Text(link.base.host ?? link.base.absoluteString)
                                .font(.callout.monospaced())
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                        LabeledContent("Status", value: model.connected ? "Connected" : "Not connected")
                    }
                    Button("Pair again") {
                        dismiss()
                        model.forgetLink()
                    }
                } header: {
                    MonoLabel("Server")
                }
                .listRowBackground(Palette.panel)

                Section {
                    Button("Show setup again") {
                        dismiss()
                        setupDone = false
                    }
                } header: {
                    MonoLabel("Setup")
                } footer: {
                    Text("The welcome, the flicker warning, the microphone and the practice round.")
                }
                .listRowBackground(Palette.panel)
            }
            .scrollContentBackground(.hidden)
            .background(Palette.bg.ignoresSafeArea())
            .foregroundStyle(Palette.text)
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                // The sheet covers the main screen's Stop: keep one here.
                StopButton()
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(Palette.bg)
            }
        }
        .onAppear { permission = MicPermission.current() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { permission = MicPermission.current() }  // back from iOS Settings
        }
    }

    private func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) {
            openURL(url)
        }
    }
}

/// Look to Speak on this phone (c-VEP, INTERFACE-v2-draft.md section 9): Recalibrate, and what
/// the operator checks when selections go wrong: the calibration score, the phone's refresh check
/// and the clock that turns its frame times into the laptop's.
private struct LookToSpeakSettings: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Section {
            Button("Recalibrate") { model.recalibrate() }
                .disabled(!model.connected)
            LabeledContent("Calibration", value: calibration)
            LabeledContent("Refresh", value: refresh)
            if let report = model.displayReport, !report.message.isEmpty {
                Text(report.message)
                    .font(.footnote)
                    .foregroundStyle(report.ok ? Palette.muted : Palette.warn)
            }
            LabeledContent("Clock offset", value: model.clock.offset.map { Self.ms($0, signed: true) } ?? "No pong yet")
            LabeledContent("Best round trip", value: model.clock.best.map { Self.ms($0.roundTrip) } ?? "–")
            if model.markersWithoutClock > 0 {
                Text("\(model.markersWithoutClock) cycle markers not sent: there was no clock yet.")
                    .font(.footnote)
                    .foregroundStyle(Palette.warn)
            }
        } header: {
            MonoLabel("Look to Speak (c-VEP)")
        } footer: {
            Text("Recalibrate runs the calibration again before the next page: about 20 seconds, the user looks at the left card. The phone flickers only at exactly 60 Hz; the clock turns its frame times into the laptop's.")
        }
    }

    private var calibration: String {
        guard let status = model.status else { return "–" }
        if let score = status.calibrationAccuracy {
            let percent = Int((score * 100).rounded())
            return score < StateLine.lowScore ? "\(percent) % (low)" : "\(percent) %"
        }
        return status.trained ? "Trained" : "Not yet"
    }

    private var refresh: String {
        guard let report = model.displayReport else { return "Not measured yet" }
        return String(format: "%.2f Hz · ", report.refreshHz) + (report.ok ? "OK" : "Not OK")
    }

    private static func ms(_ seconds: Double, signed: Bool = false) -> String {
        String(format: signed ? "%+.1f ms" : "%.1f ms", seconds * 1000)
    }
}

/// Whether the microphone and speech recognition are allowed, as one line.
struct PermissionRow: View {
    let permission: MicPermission

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(Palette.text)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        switch permission {
        case .granted: return Palette.say
        case .denied: return Palette.warn
        case .notAsked: return Palette.muted
        }
    }

    private var text: String {
        switch permission {
        case .granted: return "Allowed"
        case .denied: return "Blocked: allow Microphone and Speech Recognition for BCI Partner in iOS Settings."
        case .notAsked: return "Not asked yet"
        }
    }
}
