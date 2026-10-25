import AppKit
import BrainPointCore
import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers

/// The Screen Recording permission (System Settings > Privacy & Security > Screen & System
/// Audio Recording). A screenshot for `scan.image` needs it; without it captures return nil.
public enum ScreenCapturePermission {
    /// True when this process may capture the screen. Never prompts.
    public static var granted: Bool { CGPreflightScreenCaptureAccess() }

    /// Asks for the permission (the system prompt, once per process; afterwards the user has to
    /// turn it on in System Settings and usually relaunch the app).
    public static func request() { _ = CGRequestScreenCaptureAccess() }
}

/// A screenshot ready for `scan.image` (PROTOCOL-v3.md section 2): JPEG, base64.
public struct CapturedImage: Sendable {
    public var jpegBase64: String
    /// Image size in pixels (long edge at most the requested `maxEdge`).
    public var width: Int
    public var height: Int
    /// What the image shows, in global points (top-left origin): the requested rect, cut to the
    /// display it is on. Equal to the request unless the rect hung off the display.
    public var rect: CGRect

    public init(jpegBase64: String, width: Int, height: Int, rect: CGRect = .zero) {
        self.jpegBase64 = jpegBase64
        self.width = width
        self.height = height
        self.rect = rect
    }
}

public enum WindowCapture {
    /// The region `rect` (global points, top-left origin) of the screen, scaled so the long edge
    /// is <= `maxEdge` pixels (never scaled above the display's own pixels), JPEG quality 0.8,
    /// with `redact` rects (global points) painted black (password fields). BrainPoint's own
    /// windows (the overlay, the strip) are left out. Uses ScreenCaptureKit
    /// (`SCScreenshotManager.captureImage`). Returns nil without the Screen Recording permission
    /// (it never prompts by itself), when the rect is on no display, or when the capture fails.
    /// A rect spanning two displays is cut to the one holding its centre (see `CapturedImage.rect`).
    public static func capture(rect: CGRect, maxEdge: Int = 1280, redact: [CGRect] = []) async -> CapturedImage? {
        guard ScreenCapturePermission.granted else { return nil }
        guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        else { return nil }
        let displays = content.displays
        guard let di = CaptureMath.displayIndex(for: rect, displays: displays.map(\.frame)) else { return nil }
        let display = displays[di]
        guard let source = CaptureMath.sourceRect(rect, onDisplay: display.frame) else { return nil }
        let captured = source.offsetBy(dx: display.frame.minX, dy: display.frame.minY)

        // Pixels per point on that display (2 on Retina), so a small window keeps its detail.
        var scale = 1.0
        if let mode = CGDisplayCopyDisplayMode(display.displayID), mode.width > 0 {
            scale = max(1.0, Double(mode.pixelWidth) / Double(mode.width))
        }
        guard let size = CaptureMath.scaledSize(width: Double(source.width) * scale,
                                                height: Double(source.height) * scale,
                                                maxEdge: maxEdge) else { return nil }

        let pid = ProcessInfo.processInfo.processIdentifier
        let filter: SCContentFilter
        if let me = content.applications.first(where: { $0.processID == pid }) {
            filter = SCContentFilter(display: display, excludingApplications: [me], exceptingWindows: [])
        } else {
            let mine = content.windows.filter { $0.owningApplication?.processID == pid }
            filter = SCContentFilter(display: display, excludingWindows: mine)
        }
        let config = SCStreamConfiguration()
        config.sourceRect = source
        config.width = size.w
        config.height = size.h
        config.showsCursor = false
        config.scalesToFit = true
        config.capturesAudio = false

        guard let raw = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
        else { return nil }
        // Never send an unredacted image: if blacking out fails, send nothing.
        let image = redact.isEmpty ? raw : blackOut(raw, redact: redact, captured: captured)
        guard let image, let jpeg = jpegData(image, quality: 0.8) else { return nil }
        return CapturedImage(jpegBase64: jpeg.base64EncodedString(), width: image.width,
                             height: image.height, rect: captured)
    }

    /// `image` (showing `captured`, global points) with each `redact` rect filled black.
    /// Nil when the drawing context cannot be made.
    static func blackOut(_ image: CGImage, redact: [CGRect], captured: CGRect) -> CGImage? {
        let w = image.width, h = image.height
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        ctx.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
        for r in redact {
            guard let px = CaptureMath.imageRect(of: r, captured: captured, imageW: w, imageH: h) else { continue }
            ctx.fill(CaptureMath.flipped(px, imageH: h))
        }
        return ctx.makeImage()
    }

    static func jpegData(_ image: CGImage, quality: Double) -> Data? {
        let data = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil)
        else { return nil }
        let props = [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary
        CGImageDestinationAddImage(dest, image, props)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return data as Data
    }
}
