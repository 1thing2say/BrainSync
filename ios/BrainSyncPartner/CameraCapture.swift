// The camera behind the Camera app: one AVCaptureSession with a photo output, back or front lens.
// Every session call (configure, start, stop, swap the lens, take a photo) runs on the
// controller's own serial queue: startRunning blocks for up to a second, and the main thread
// draws the c-VEP squares frame by frame, so it must never wait on the camera.
//
// Without a camera (the iOS Simulator, or `forceFallback`) it shows a generated scene instead
// (`CameraFallbackScene` in CameraPreview.swift), and `capture()` returns a snapshot of that
// scene, so the whole flow (shutter, gallery, delete) can be tried without a phone.

import AVFoundation
import Observation
import SwiftUI
import UIKit

/// May the app use the camera? `denied` also covers "restricted" (parental controls, MDM).
enum CameraAuthorization: Equatable {
    case notDetermined
    case denied
    case authorized

    init(_ status: AVAuthorizationStatus) {
        switch status {
        case .authorized: self = .authorized
        case .notDetermined: self = .notDetermined
        default: self = .denied
        }
    }
}

/// Which lens the viewfinder shows.
enum CameraLens: String, CaseIterable {
    case back
    case front

    /// Shown on the viewfinder's chip and drawn into the simulated scene.
    var name: String {
        switch self {
        case .back: return "Back camera"
        case .front: return "Front camera"
        }
    }

    var flipped: CameraLens { self == .back ? .front : .back }

    var position: AVCaptureDevice.Position { self == .back ? .back : .front }

    init?(_ position: AVCaptureDevice.Position) {
        switch position {
        case .back: self = .back
        case .front: self = .front
        default: return nil
        }
    }
}

@MainActor
@Observable
final class CameraCapture {
    /// Camera permission, refreshed by `start()` and `refreshAuthorization()`.
    private(set) var authorization: CameraAuthorization
    /// The lens shown (or, before `start()`, the one that will be).
    private(set) var lens: CameraLens
    /// The preview is live (or the simulated scene is up).
    private(set) var isRunning = false
    /// A photo is being taken; a second `capture()` meanwhile returns nil.
    private(set) var isCapturing = false
    /// `flip()` is swapping the lens; a second flip meanwhile is ignored.
    private(set) var isSwitching = false
    /// A phone call, Split View or another app took the camera; the preview is frozen until it
    /// gives it back (the session restarts by itself).
    private(set) var isInterrupted = false
    /// No real camera: the simulated scene stands in (always in the Simulator).
    let usesFallback: Bool
    /// The last thing that went wrong, in plain words, for a small note on screen; nil = fine.
    private(set) var lastError: String?

    /// The capture session, for `CameraPreview`'s preview layer. Never touch it from the main
    /// thread except to hand it to a preview layer.
    var session: AVCaptureSession { controller.session }

    @ObservationIgnored private let controller = CameraSessionController()
    /// `start()` was called and `stop()` was not: a permission answer or a media-services
    /// reset that arrives later starts (or restarts) the session only then.
    @ObservationIgnored private var wanted = false
    @ObservationIgnored private let observers = ObserverBag()

    /// - Parameters:
    ///   - lens: the lens to start with.
    ///   - forceFallback: use the simulated scene even on a phone with a camera (previews, demos).
    init(lens: CameraLens = .back, forceFallback: Bool = false) {
        self.lens = lens
        #if targetEnvironment(simulator)
        usesFallback = true
        #else
        usesFallback = forceFallback || !CameraSessionController.hasCamera
        #endif
        authorization = usesFallback ? .authorized
            : CameraAuthorization(AVCaptureDevice.authorizationStatus(for: .video))
        observeSession()
    }

    // MARK: Permission

    /// Re-reads the permission (the user may have changed it in Settings meanwhile).
    func refreshAuthorization() {
        guard !usesFallback else { return }
        authorization = CameraAuthorization(AVCaptureDevice.authorizationStatus(for: .video))
    }

    /// Shows the system's camera prompt if it was never answered; returns whether the camera may
    /// be used. A refused prompt is final: only the Settings app can turn it back on.
    @discardableResult
    func requestAccess() async -> Bool {
        guard !usesFallback else { return true }
        refreshAuthorization()
        if authorization == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .video)
            refreshAuthorization()
        }
        return authorization == .authorized
    }

    // MARK: Session

    /// Starts the preview. Asks for permission first if it was never asked; does nothing while
    /// it is denied. Returns at once: the session starts on its own queue.
    func start() {
        wanted = true
        if usesFallback {
            isRunning = true
            return
        }
        refreshAuthorization()
        switch authorization {
        case .denied:
            lastError = "Camera access is off."
        case .notDetermined:
            Task {
                if await requestAccess(), wanted { start() }
            }
        case .authorized:
            controller.start(position: lens.position) { [weak self] running, position, message in
                // DispatchQueue.main keeps the queue's order: a stop() right after a start()
                // always lands after it, so isRunning ends false.
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard let self else { return }
                        self.isRunning = running
                        if let position, let used = CameraLens(position) { self.lens = used }
                        self.lastError = message
                    }
                }
            }
        }
    }

    /// Stops the preview (the app went to the background, or another screen is up). Returns at
    /// once; nothing flickers or freezes on the main thread while the session winds down.
    func stop() {
        wanted = false
        if usesFallback {
            isRunning = false
            return
        }
        controller.stop { [weak self] in
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.isRunning = false }
            }
        }
    }

    /// Swaps between the back and the front lens. Ignored while a photo is being taken or a
    /// swap is still running. A phone with only one lens keeps it (`lastError` says why).
    func flip() {
        guard !isSwitching, !isCapturing else { return }
        let target = lens.flipped
        if usesFallback {
            lens = target
            return
        }
        isSwitching = true
        controller.use(position: target.position) { [weak self] ok in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.isSwitching = false
                    if ok {
                        self.lens = target
                        self.lastError = nil
                    } else {
                        self.lastError = "This phone has no \(target.name.lowercased())."
                    }
                }
            }
        }
    }

    // MARK: Photo

    /// Takes one photo (portrait, as the preview shows it). nil when the camera is not running,
    /// a photo is already being taken, or the capture failed (`lastError` says which). With the
    /// simulated scene it is a snapshot of that scene at this moment.
    func capture() async -> UIImage? {
        guard !isCapturing else { return nil }
        if usesFallback {
            return Self.snapshot(lens: lens, date: .now)
        }
        guard isRunning else {
            lastError = "The camera is not running."
            return nil
        }
        isCapturing = true
        defer { isCapturing = false }
        let data: Data? = await withCheckedContinuation { continuation in
            controller.capture { data in continuation.resume(returning: data) }
        }
        guard let data, let image = UIImage(data: data) else {
            lastError = "The photo could not be taken."
            return nil
        }
        lastError = nil
        return image
    }

    /// The simulated scene as a 3:4 portrait photo (1170 x 1560 pixels).
    private static func snapshot(lens: CameraLens, date: Date) -> UIImage? {
        let renderer = ImageRenderer(content:
            CameraFallbackScene(lens: lens, date: date)
                .frame(width: 390, height: 520))
        renderer.scale = 3
        renderer.isOpaque = true
        return renderer.uiImage
    }

    // MARK: Interruptions

    private func observeSession() {
        guard !usesFallback else { return }
        let center = NotificationCenter.default
        observers.add(center.addObserver(forName: AVCaptureSession.wasInterruptedNotification,
                                         object: session, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.isInterrupted = true }
        })
        observers.add(center.addObserver(forName: AVCaptureSession.interruptionEndedNotification,
                                         object: session, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.isInterrupted = false }
        })
        // Media services reset (rare, but it kills the session): start again if still wanted.
        observers.add(center.addObserver(forName: AVCaptureSession.runtimeErrorNotification,
                                         object: session, queue: .main) { [weak self] note in
            let error = note.userInfo?[AVCaptureSessionErrorKey] as? AVError
            MainActor.assumeIsolated {
                guard let self else { return }
                self.isRunning = false
                if error?.code == .mediaServicesWereReset, self.wanted {
                    self.start()
                } else {
                    self.lastError = "The camera stopped."
                }
            }
        })
    }
}

/// Removes its notification observers when the camera goes away.
private final class ObserverBag {
    private var tokens: [NSObjectProtocol] = []

    func add(_ token: NSObjectProtocol) { tokens.append(token) }

    deinit {
        tokens.forEach(NotificationCenter.default.removeObserver)
    }
}

// MARK: - The session, on its own queue

/// Owns the AVCaptureSession and touches it only on `queue`, so it is safe to call from any
/// thread. Completions run on that queue; `CameraCapture` hops them back to the main thread.
private final class CameraSessionController: NSObject, @unchecked Sendable {
    let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "org.brainsync.camera.session", qos: .userInitiated)
    private let photoOutput = AVCapturePhotoOutput()
    private var input: AVCaptureDeviceInput?
    private var configured = false
    /// Delegates of photos in flight, kept alive until AVFoundation is done with them.
    private var inFlight: [Int64: PhotoCaptureDelegate] = [:]

    /// A real camera exists (false in the Simulator and on a Mac running the iPad app without one).
    static var hasCamera: Bool {
        device(.back) != nil || device(.front) != nil
    }

    /// The plain wide-angle lens: a virtual multi-lens camera would open on the ultra wide.
    static func device(_ position: AVCaptureDevice.Position) -> AVCaptureDevice? {
        AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position)
    }

    /// Configures the session on first use (falling back to the other lens if `position` has
    /// none), then starts it. Reports whether it runs, the lens in use, and an error message.
    func start(position: AVCaptureDevice.Position,
               completion: @escaping @Sendable (Bool, AVCaptureDevice.Position?, String?) -> Void) {
        queue.async {
            if !self.configured, let message = self.configure(position) {
                completion(false, nil, message)
                return
            }
            if !self.session.isRunning {
                self.session.startRunning()
            }
            let running = self.session.isRunning
            completion(running, self.input?.device.position, running ? nil : "The camera did not start.")
        }
    }

    func stop(completion: @escaping @Sendable () -> Void) {
        queue.async {
            if self.session.isRunning {
                self.session.stopRunning()
            }
            completion()
        }
    }

    /// Swaps the input to the lens at `position`. Before the first start it only succeeds (the
    /// lens is then picked by `start`); without such a lens it keeps the old input.
    func use(position: AVCaptureDevice.Position, completion: @escaping @Sendable (Bool) -> Void) {
        queue.async {
            guard self.configured else {
                completion(Self.device(position) != nil)
                return
            }
            guard let device = Self.device(position),
                  let newInput = try? AVCaptureDeviceInput(device: device) else {
                completion(false)
                return
            }
            self.session.beginConfiguration()
            if let old = self.input { self.session.removeInput(old) }
            var ok = false
            if self.session.canAddInput(newInput) {
                self.session.addInput(newInput)
                self.input = newInput
                ok = true
            } else if let old = self.input, self.session.canAddInput(old) {
                self.session.addInput(old)
            }
            self.orientPhotoConnection()
            self.session.commitConfiguration()
            completion(ok)
        }
    }

    /// Takes one JPEG. nil if the session is not running or the capture failed.
    func capture(completion: @escaping @Sendable (Data?) -> Void) {
        queue.async {
            guard self.configured, self.session.isRunning,
                  self.photoOutput.connection(with: .video)?.isActive == true else {
                completion(nil)
                return
            }
            let settings: AVCapturePhotoSettings
            if self.photoOutput.availablePhotoCodecTypes.contains(.jpeg) {
                settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
            } else {
                settings = AVCapturePhotoSettings()
            }
            settings.photoQualityPrioritization = .balanced
            let id = settings.uniqueID
            let delegate = PhotoCaptureDelegate(deliver: completion) { [weak self] in
                guard let self else { return }
                self.queue.async { self.inFlight[id] = nil }
            }
            self.inFlight[id] = delegate
            self.photoOutput.capturePhoto(with: settings, delegate: delegate)
        }
    }

    /// Runs on `queue`. Returns an error message, or nil once the session is configured.
    private func configure(_ position: AVCaptureDevice.Position) -> String? {
        let other: AVCaptureDevice.Position = position == .back ? .front : .back
        guard let device = Self.device(position) ?? Self.device(other) else {
            return "No camera found."
        }
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        session.sessionPreset = .photo
        let newInput: AVCaptureDeviceInput
        do {
            newInput = try AVCaptureDeviceInput(device: device)
        } catch {
            return "The camera could not be opened."
        }
        guard session.canAddInput(newInput), session.canAddOutput(photoOutput) else {
            return "The camera could not be set up."
        }
        session.addInput(newInput)
        session.addOutput(photoOutput)
        input = newInput
        // Balanced: a little denoising, without the long wait of .quality after each shot.
        photoOutput.maxPhotoQualityPrioritization = .balanced
        orientPhotoConnection()
        configured = true
        return nil
    }

    /// The app runs in portrait only, so photos are rotated 90 degrees from the sensor, as the
    /// preview shows them (the preview layer is portrait by default).
    private func orientPhotoConnection() {
        guard let connection = photoOutput.connection(with: .video),
              connection.isVideoRotationAngleSupported(90) else { return }
        connection.videoRotationAngle = 90
    }
}

/// One photo's delegate: hands the JPEG over once, then tells the controller it can be dropped.
private final class PhotoCaptureDelegate: NSObject, AVCapturePhotoCaptureDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var deliver: (@Sendable (Data?) -> Void)?
    private let finished: @Sendable () -> Void

    init(deliver: @escaping @Sendable (Data?) -> Void, finished: @escaping @Sendable () -> Void) {
        self.deliver = deliver
        self.finished = finished
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto,
                     error: Error?) {
        hand(error == nil ? photo.fileDataRepresentation() : nil)
    }

    func photoOutput(_ output: AVCapturePhotoOutput,
                     didFinishCaptureFor resolvedSettings: AVCaptureResolvedPhotoSettings, error: Error?) {
        hand(nil)  // only if no photo came (a failed capture skips the processing callback)
        finished()
    }

    private func hand(_ data: Data?) {
        lock.lock()
        let callback = deliver
        deliver = nil
        lock.unlock()
        callback?(data)
    }
}
