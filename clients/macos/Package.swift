// swift-tools-version: 6.0
// BrainPoint Desktop: control the focused macOS app with SSVEP through the Accessibility API.
// See PROTOCOL-v3.md (point mode, v3.1 "ax" scans) at the repo root.
import PackageDescription

let package = Package(
    name: "BrainPoint",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "BrainPoint", targets: ["BrainPoint"]),
        .executable(name: "bp-probe", targets: ["bp-probe"]),
    ],
    targets: [
        // Pure logic (Foundation + CoreGraphics only): protocol shapes, flicker luminance,
        // geometry, hints and the control gate. Unit-tested.
        .target(name: "BrainPointCore"),
        // AppKit + Accessibility: focus watching, the AX walk, pressing, the overlay, safety.
        .target(name: "BrainPointPlatform", dependencies: ["BrainPointCore"]),
        // The menu-bar app that wires Core and Platform to the server.
        .executableTarget(name: "BrainPoint", dependencies: ["BrainPointCore", "BrainPointPlatform"]),
        // Phase 0 measurements: overlay frame timing and AX coverage.
        .executableTarget(name: "bp-probe", dependencies: ["BrainPointCore", "BrainPointPlatform"]),
        .testTarget(name: "BrainPointCoreTests", dependencies: ["BrainPointCore"],
                    resources: [.copy("Fixtures")]),
    ]
)
