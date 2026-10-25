// The Camera app's screens, as pieces the app composes: the viewfinder, the gallery, the shutter
// effect and the last-photo thumbnail. No c-VEP and no networking here.
//
// Layout: both screens are one column. On top, a 3:4 frame (the shape of the photos) with the
// live preview or the photo; below it a plain black strip that takes the rest of the height
// (at least `stripMinHeight`) and holds whatever view the app passes, meant for the two c-VEP
// squares in a row. The strip never overlaps the moving preview, and the frame sits in the same
// place on both screens, so the squares never move when the user switches between them.
//
// Nothing here blinks (PLAN rule 10): the shutter darkens the preview for a moment instead of
// flashing it white, which keeps a bright flash out of the user's eyes and out of the EEG.

import AudioToolbox
import SwiftUI
import UIKit

// MARK: - Viewfinder

/// The camera screen: the 3:4 viewfinder (preview, lens chip, last-photo thumbnail, permission
/// prompt), then `bottomStrip` on black. Increment `shutterTrigger` after each photo to play
/// the shutter effect.
struct CameraViewfinder<Strip: View>: View {
    let capture: CameraCapture
    /// Shows the newest photo's thumbnail in the viewfinder's corner; nil hides it.
    var store: PhotoStore?
    /// Change it (count up) to play the shutter effect.
    var shutterTrigger: Int
    /// Play the shutter click. A real capture already makes the system's own shutter sound, so
    /// the default plays it only with the simulated scene.
    var shutterSound: Bool
    /// The strip under the viewfinder is at least this tall.
    var stripMinHeight: CGFloat
    let bottomStrip: Strip

    init(capture: CameraCapture, store: PhotoStore? = nil, shutterTrigger: Int = 0,
         shutterSound: Bool? = nil, stripMinHeight: CGFloat = 180,
         @ViewBuilder bottomStrip: () -> Strip) {
        self.capture = capture
        self.store = store
        self.shutterTrigger = shutterTrigger
        self.shutterSound = shutterSound ?? capture.usesFallback
        self.stripMinHeight = stripMinHeight
        self.bottomStrip = bottomStrip()
    }

    var body: some View {
        CameraColumn(stripMinHeight: stripMinHeight) {
            ZStack {
                if capture.authorization == .authorized {
                    CameraPreview(capture: capture)
                        .cameraShutterEffect(trigger: shutterTrigger, sound: shutterSound)
                } else {
                    CameraPermissionView(capture: capture)
                }
            }
            .overlay(alignment: .topLeading) {
                if capture.authorization == .authorized {
                    LensChip(capture: capture)
                        .padding(10)
                }
            }
            .overlay(alignment: .bottomLeading) {
                if let store {
                    CameraLastPhotoThumbnail(store: store)
                        .padding(10)
                }
            }
            .overlay(alignment: .bottom) {
                if let note = viewfinderNote {
                    Text(note)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(Palette.text)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(.black.opacity(0.6), in: Capsule())
                        .padding(.bottom, 14)
                }
            }
        } strip: {
            bottomStrip
        }
    }

    /// Why the preview is still or frozen, if it is.
    private var viewfinderNote: String? {
        guard capture.authorization == .authorized else { return nil }
        if capture.isInterrupted { return "Camera in use elsewhere" }
        if let error = capture.lastError { return error }
        return nil
    }
}

/// "Back camera" (and "Simulated" without a real camera) in the viewfinder's corner.
private struct LensChip: View {
    let capture: CameraCapture

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: capture.lens == .back ? "camera" : "person.crop.square")
                .font(.caption)
                .foregroundStyle(Palette.text)
            MonoLabel(capture.lens.name, color: Palette.text)
            if capture.usesFallback {
                MonoLabel("Simulated", color: Palette.warn)
            }
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(.black.opacity(0.55), in: Capsule())
        .overlay(Capsule().strokeBorder(Palette.lineStrong, lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

/// Shown in the viewfinder instead of the preview until the camera may be used. The buttons are
/// for whoever holds the phone (the system prompt and Settings need a tap anyway).
struct CameraPermissionView: View {
    let capture: CameraCapture

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: denied ? "video.slash" : "camera")
                .font(.system(size: 36, weight: .light))
                .foregroundStyle(Palette.muted)
            Heading(denied ? "Camera access is off" : "Camera access", font: .title3.weight(.bold))
            Text(denied
                 ? "Turn on Camera for this app in Settings to take photos."
                 : "The camera app needs the camera to take photos. They stay on this phone.")
                .font(.subheadline)
                .foregroundStyle(Palette.muted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if denied {
                Button("Open Settings") {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        UIApplication.shared.open(url)
                    }
                }
                .buttonStyle(FilledButtonStyle(.secondary))
            } else {
                Button("Allow camera") {
                    Task {
                        if await capture.requestAccess() { capture.start() }
                    }
                }
                .buttonStyle(FilledButtonStyle(.primary))
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.panel)
    }

    private var denied: Bool { capture.authorization == .denied }
}

// MARK: - Shutter effect

extension View {
    /// The shutter: the view darkens for a moment and comes back (no white flash), plus the
    /// camera click (system sound 1108) when `sound` is on. Plays each time `trigger` changes,
    /// never on first appearance.
    func cameraShutterEffect<T: Equatable>(trigger: T, sound: Bool = true) -> some View {
        modifier(CameraShutterEffect(trigger: trigger, sound: sound))
    }
}

private struct CameraShutterEffect<T: Equatable>: ViewModifier {
    let trigger: T
    let sound: Bool

    func body(content: Content) -> some View {
        content
            .keyframeAnimator(initialValue: 0.0, trigger: trigger) { view, dim in
                view.overlay(Color.black.opacity(dim).allowsHitTesting(false))
            } keyframes: { _ in
                LinearKeyframe(0.75, duration: 0.05)
                CubicKeyframe(0.0, duration: 0.3)
            }
            .onChange(of: trigger) {
                if sound { AudioServicesPlaySystemSound(1108) }
            }
    }
}

// MARK: - Last photo

/// The newest photo as a small rounded square (an empty frame when there is none). It loads its
/// own thumbnail whenever the newest photo changes.
struct CameraLastPhotoThumbnail: View {
    let store: PhotoStore
    var size: CGFloat = 52

    @State private var thumbnail: UIImage?

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 8)
        ZStack {
            Palette.surface
            if let thumbnail {
                Image(uiImage: thumbnail)
                    .resizable()
                    .scaledToFill()
            } else {
                Image(systemName: "photo")
                    .font(.body)
                    .foregroundStyle(Palette.muted)
            }
        }
        .frame(width: size, height: size)
        .clipShape(shape)
        .overlay(shape.strokeBorder(Palette.lineStrong, lineWidth: 1))
        .task(id: store.last) {
            thumbnail = store.isEmpty ? nil
                : await store.image(at: store.count - 1, maxPixelSize: Int(size * 3))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(store.isEmpty ? "No photos yet"
                            : "Last photo, \(store.count) photo\(store.count == 1 ? "" : "s")")
    }
}

// MARK: - Gallery

/// The gallery: "Photo i of n" and the photo at `index` (0-based, oldest first) in the same 3:4
/// frame as the viewfinder, then `bottomStrip` on black. With `confirmingDelete` the photo is
/// dimmed under a "Delete this photo?" card; the app's strip then offers Delete and Keep.
struct CameraGalleryView<Strip: View>: View {
    let store: PhotoStore
    let index: Int
    var confirmingDelete: Bool
    /// The line under "Delete this photo?", naming how to answer.
    var confirmHint: String
    var stripMinHeight: CGFloat
    let bottomStrip: Strip

    @State private var image: UIImage?

    init(store: PhotoStore, index: Int, confirmingDelete: Bool = false,
         confirmHint: String = "Look at Delete to remove it, or Keep to keep it.",
         stripMinHeight: CGFloat = 180, @ViewBuilder bottomStrip: () -> Strip) {
        self.store = store
        self.index = index
        self.confirmingDelete = confirmingDelete
        self.confirmHint = confirmHint
        self.stripMinHeight = stripMinHeight
        self.bottomStrip = bottomStrip()
    }

    var body: some View {
        CameraColumn(stripMinHeight: stripMinHeight) {
            ZStack {
                Palette.bg
                if store.isEmpty {
                    VStack(spacing: 10) {
                        Image(systemName: "photo.on.rectangle")
                            .font(.system(size: 36, weight: .light))
                            .foregroundStyle(Palette.muted)
                        Text("No photos yet")
                            .font(.headline)
                            .foregroundStyle(Palette.muted)
                    }
                } else if let image {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                        .accessibilityLabel(photoLabel)
                }
                if confirmingDelete && !store.isEmpty {
                    DeleteConfirmCard(hint: confirmHint)
                }
            }
            .overlay(alignment: .topLeading) {
                if !store.isEmpty {
                    GalleryHeader(position: clamped + 1, count: store.count, date: store.date(at: clamped))
                        .padding(10)
                }
            }
        } strip: {
            bottomStrip
        }
        // The previous photo stays up until the next one is decoded: no black frame between.
        .task(id: store.url(at: clamped)) {
            guard !store.isEmpty else {
                image = nil
                return
            }
            if let loaded = await store.image(at: clamped, maxPixelSize: 2048) {
                image = loaded
            }
        }
    }

    /// `index` kept inside the gallery (it may point past the end right after a delete).
    private var clamped: Int {
        min(max(index, 0), max(store.count - 1, 0))
    }

    private var photoLabel: String {
        "Photo \(clamped + 1) of \(store.count)"
    }
}

/// "Photo 3 of 7" and when it was taken.
private struct GalleryHeader: View {
    let position: Int
    let count: Int
    let date: Date?

    var body: some View {
        HStack(spacing: 8) {
            MonoLabel("Photo \(position) of \(count)", color: Palette.text)
            if let date {
                Text(date, format: .dateTime.day().month(.abbreviated).hour().minute())
                    .font(.caption2.monospaced())
                    .foregroundStyle(Palette.muted)
            }
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(.black.opacity(0.55), in: Capsule())
        .overlay(Capsule().strokeBorder(Palette.lineStrong, lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

/// The photo dimmed, with the question on a card in the middle.
private struct DeleteConfirmCard: View {
    let hint: String

    var body: some View {
        ZStack {
            Color.black.opacity(0.65)
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Image(systemName: "trash")
                        .foregroundStyle(Palette.danger)
                    MonoLabel("Delete", color: Palette.danger)
                }
                Heading("Delete this photo?", font: .title2.weight(.bold))
                Text(hint)
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                Text("It is removed from this app for good.")
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
            }
            .card(padding: 16)
            .padding(.horizontal, 24)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isModal)
    }
}

// MARK: - Shared layout

/// The column both screens share: the 3:4 frame on top (first claim on the height, but never
/// leaving the strip less than `stripMinHeight`), then the strip on plain black.
private struct CameraColumn<Frame: View, Strip: View>: View {
    let stripMinHeight: CGFloat
    @ViewBuilder let frame: Frame
    @ViewBuilder let strip: Strip

    var body: some View {
        VStack(spacing: 0) {
            frame
                .aspectRatio(3.0 / 4.0, contentMode: .fit)
                .clipShape(RoundedRectangle(cornerRadius: Palette.cardRadius))
                .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius)
                    .strokeBorder(Palette.line, lineWidth: 1))
                .padding(.horizontal, 8)
                .padding(.top, 4)
                .frame(maxWidth: .infinity)
                .layoutPriority(1)
            strip
                .frame(maxWidth: .infinity, minHeight: stripMinHeight, maxHeight: .infinity)
                .background(Color.black)
        }
        .background(Color.black.ignoresSafeArea())
    }
}

// MARK: - Previews

/// Three plain boxes where the c-VEP squares go (previews only).
private struct StripPlaceholder: View {
    let labels: [String]

    var body: some View {
        HStack(spacing: 18) {
            ForEach(labels, id: \.self) { label in
                VStack(spacing: 8) {
                    RoundedRectangle(cornerRadius: 6)
                        .strokeBorder(Palette.lineStrong, lineWidth: 1)
                        .frame(width: 72, height: 72)
                    MonoLabel(label)
                }
            }
        }
    }
}

#Preview("Viewfinder") {
    let capture = CameraCapture(forceFallback: true)
    let store = PhotoStore(directory: FileManager.default.temporaryDirectory
        .appending(path: "camera-preview-photos", directoryHint: .isDirectory))
    return CameraViewfinder(capture: capture, store: store) {
        StripPlaceholder(labels: ["Shutter", "Flip", "Gallery"])
    }
    .onAppear { capture.start() }
}

#Preview("Gallery, confirm delete") {
    let store = PhotoStore(directory: FileManager.default.temporaryDirectory
        .appending(path: "camera-preview-photos", directoryHint: .isDirectory))
    return CameraGalleryView(store: store, index: 0, confirmingDelete: true) {
        StripPlaceholder(labels: ["Delete", "Back", "Keep"])
    }
    .task {
        if store.isEmpty, let photo = await CameraCapture(forceFallback: true).capture() {
            await store.save(photo)
        }
    }
}
