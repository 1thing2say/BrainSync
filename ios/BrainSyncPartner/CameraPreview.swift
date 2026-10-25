// The live viewfinder: the camera's preview layer, or (in the Simulator, or without a camera)
// a generated scene with a ticking clock, so the Camera app looks and behaves the same there.
// It fills whatever frame it is given (aspect fill); CameraScreens.swift frames it at 3:4,
// the shape of the photos.

import AVFoundation
import SwiftUI
import UIKit

struct CameraPreview: View {
    let capture: CameraCapture

    var body: some View {
        if capture.usesFallback {
            // Once a second: enough for the clock, and nothing else in the scene moves.
            TimelineView(.periodic(from: .now, by: 1)) { context in
                CameraFallbackScene(lens: capture.lens, date: context.date)
            }
        } else {
            CameraLayerView(session: capture.session)
                .background(Color.black)
        }
    }
}

/// AVCaptureVideoPreviewLayer as the view's own layer, so it always matches the view's bounds.
private struct CameraLayerView: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewLayerView {
        let view = PreviewLayerView()
        view.backgroundColor = .black
        view.previewLayer.videoGravity = .resizeAspectFill
        view.previewLayer.session = session
        view.isAccessibilityElement = true
        view.accessibilityLabel = "Camera viewfinder"
        return view
    }

    func updateUIView(_ view: PreviewLayerView, context: Context) {
        if view.previewLayer.session !== session {
            view.previewLayer.session = session
        }
    }
}

private final class PreviewLayerView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }

    var previewLayer: AVCaptureVideoPreviewLayer {
        // layerClass guarantees the type.
        layer as! AVCaptureVideoPreviewLayer  // swiftlint:disable:this force_cast
    }
}

/// The stand-in for a camera: a calm landscape (dusk for the back lens, a warm sky for the
/// front one), the time to the second and the lens name, so two photos never look alike.
/// Also what `CameraCapture.capture()` renders into a photo in the Simulator.
struct CameraFallbackScene: View {
    let lens: CameraLens
    let date: Date

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let h = geo.size.height
            ZStack {
                LinearGradient(colors: sky, startPoint: .top, endPoint: .bottom)
                Circle()
                    .fill(RadialGradient(colors: [sun.opacity(0.95), sun.opacity(0)],
                                         center: .center, startRadius: 0, endRadius: w * 0.32))
                    .frame(width: w * 0.64, height: w * 0.64)
                    .position(x: w * 0.68, y: h * 0.42)
                Ellipse()
                    .fill(hills[0])
                    .frame(width: w * 1.6, height: h * 0.5)
                    .position(x: w * 0.15, y: h * 0.86)
                Ellipse()
                    .fill(hills[1])
                    .frame(width: w * 1.5, height: h * 0.42)
                    .position(x: w * 0.95, y: h * 0.95)
                VStack(spacing: 6) {
                    Text(date, format: .dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits).second(.twoDigits))
                        .font(.system(size: max(28, w * 0.13), weight: .semibold, design: .rounded).monospacedDigit())
                        .foregroundStyle(.white)
                        .shadow(color: .black.opacity(0.25), radius: 8, y: 2)
                    Text(date, format: .dateTime.weekday(.wide).day().month(.wide))
                        .font(.system(size: max(13, w * 0.04), weight: .medium))
                        .foregroundStyle(.white.opacity(0.85))
                    Text("\(lens.name) · simulated".uppercased())
                        .font(.system(size: max(10, w * 0.028), weight: .medium, design: .monospaced))
                        .tracking(0.8)
                        .foregroundStyle(.white.opacity(0.7))
                        .padding(.top, 4)
                }
                .position(x: w / 2, y: h * 0.3)
            }
        }
        .clipped()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Simulated \(lens.name.lowercased())")
    }

    private var sky: [Color] {
        switch lens {
        case .back: return [Color(red: 0.10, green: 0.12, blue: 0.32), Color(red: 0.42, green: 0.30, blue: 0.62),
                            Color(red: 0.95, green: 0.55, blue: 0.45)]
        case .front: return [Color(red: 0.98, green: 0.72, blue: 0.55), Color(red: 0.93, green: 0.45, blue: 0.55),
                             Color(red: 0.45, green: 0.25, blue: 0.50)]
        }
    }

    private var sun: Color {
        lens == .back ? Color(red: 1.0, green: 0.85, blue: 0.55) : Color(red: 1.0, green: 0.95, blue: 0.80)
    }

    private var hills: [Color] {
        switch lens {
        case .back: return [Color(red: 0.12, green: 0.16, blue: 0.28), Color(red: 0.06, green: 0.09, blue: 0.18)]
        case .front: return [Color(red: 0.30, green: 0.16, blue: 0.34), Color(red: 0.18, green: 0.09, blue: 0.22)]
        }
    }
}
