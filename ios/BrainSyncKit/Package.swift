// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "BrainSyncKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "BrainSyncKit", targets: ["BrainSyncKit"]),
    ],
    targets: [
        .target(name: "BrainSyncKit"),
        .testTarget(
            name: "BrainSyncKitTests",
            dependencies: ["BrainSyncKit"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
