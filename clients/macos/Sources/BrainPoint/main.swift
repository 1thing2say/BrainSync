import AppKit

// BrainPoint Desktop: a menu-bar agent that lets the user control the focused macOS app with
// SSVEP, through the BrainSync server (PROTOCOL-v3.md point mode, v3.1 "ax" scans).
MainActor.assumeIsolated {
    let app = NSApplication.shared
    let controller = AppController()
    app.delegate = controller
    app.setActivationPolicy(.accessory)
    withExtendedLifetime(controller) { app.run() }
}
