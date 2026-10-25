// A debug screen for the c-VEP flicker on this phone: the two standard squares (lags 0 and 31
// frames; never more than two), the measured refresh, dropped frames, missed cycle starts and the live frame
// index, so the timing can be checked on a device before a headset session. It needs no
// server. It has its own warning: nothing flickers until "I understand, start" is tapped, and
// Stop, leaving the screen or leaving the app stops it at once (PLAN rule 10).

import BrainSyncKit
import SwiftUI

struct FlickerLabView: View {
    @Environment(\.scenePhase) private var scenePhase
    @State private var driver = CodeFlickerDriver()
    @State private var accepted = false
    @State private var layout = 0
    @State private var onsets = 0
    @State private var lastReport = ""

    private static let slots = SuiteSlots.ids   // two squares, never more

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Heading("Flicker lab")
                Text("The c-VEP squares as the server's apps use them: one 63-bit code, the second square 31 frames behind the first, one bit a frame at 60 Hz.")
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)

                squares
                readout
                controls
            }
            .padding(16)
        }
        .background(Palette.bg.ignoresSafeArea())
        .foregroundStyle(Palette.text)
        .navigationTitle("Flicker lab")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            driver.onOnset = { _, _ in onsets += 1 }
            driver.onFrames = { hz, dropped in
                lastReport = String(format: "%.3f Hz, %d dropped", hz, dropped)
            }
            driver.startProbe()
        }
        .onDisappear {
            driver.stop()
            driver.stopProbe()
        }
        // Leaving the app stops the flicker and the probe (the driver does it); measure again
        // on return, still: the flicker waits for another tap on Start.
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { driver.startProbe() }
        }
    }

    private var squares: some View {
        HStack(spacing: 16) {
            ForEach(Array(Self.slots.enumerated()), id: \.offset) { k, slot in
                VStack(spacing: 6) {
                    CodeFlickerSquare(slot: slot, lag: CodeVep.lag(ofTarget: k), driver: driver)
                        .aspectRatio(1, contentMode: .fit)
                    MonoLabel("\(slot) · lag \(CodeVep.lag(ofTarget: k))")
                }
                .frame(maxWidth: .infinity)
            }
        }
        .padding(16)
        .background(Color.black, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
    }

    private var readout: some View {
        VStack(alignment: .leading, spacing: 8) {
            row("Refresh", refreshText, tint: refreshTint)
            row("Dropped frames", "\(driver.dropped)")
            row("Missed cycle starts", "\(driver.missedCycles)")
            row("Frame n", driver.liveFrame.map { "\($0)  (cycle \($0 / CodeVep.cycleFrames), bit \($0 % CodeVep.cycleFrames))" } ?? "still")
            row("Cycles reported", "\(driver.cyclesReported)")
            row("Onsets", "\(onsets)")
            row("Last frames report", lastReport.isEmpty ? "every 5 s while flickering" : lastReport)
        }
        .font(.callout.monospacedDigit())
        .card()
    }

    private func row(_ name: String, _ value: String, tint: Color = Palette.text) -> some View {
        HStack(alignment: .firstTextBaseline) {
            MonoLabel(name)
            Spacer(minLength: 12)
            Text(value).foregroundStyle(tint).multilineTextAlignment(.trailing)
        }
    }

    private var refreshText: String {
        guard let hz = driver.measuredHz else { return "measuring…" }
        let off = abs(hz - 60) > 1
        return String(format: "%.2f Hz", hz) + (off ? "  (not 60 Hz)" : "  ok")
    }

    private var refreshTint: Color {
        guard let hz = driver.measuredHz else { return Palette.muted }
        return abs(hz - 60) > 1 ? Palette.warn : Palette.say
    }

    @ViewBuilder
    private var controls: some View {
        if !accepted {
            Callout(title: "Warning", icon: "exclamationmark.triangle.fill", tint: Palette.warn) {
                Text("These squares flash black and white up to 30 times a second. Nobody with photosensitive epilepsy or flicker sensitivity should look at them. Stop, leaving this screen or leaving the app turns them off at once.")
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button {
                accepted = true
            } label: {
                Text("I understand").frame(maxWidth: .infinity)
            }
            .buttonStyle(FilledButtonStyle(.primary, large: true))
        } else if driver.isFlickering {
            Button {
                driver.stop()
            } label: {
                Text("Stop").frame(maxWidth: .infinity)
            }
            .buttonStyle(FilledButtonStyle(.danger, large: true))
        } else {
            Button {
                layout += 1
                driver.start(layoutId: layout)
            } label: {
                Text("Start the flicker").frame(maxWidth: .infinity)
            }
            .buttonStyle(FilledButtonStyle(.primary, large: true))
        }
    }
}

#Preview {
    NavigationStack { FlickerLabView() }
}
