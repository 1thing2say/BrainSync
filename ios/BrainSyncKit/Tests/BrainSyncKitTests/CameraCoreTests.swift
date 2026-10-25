import XCTest
@testable import BrainSyncKit

final class CameraCoreTests: XCTestCase {
    /// Runs the looks in order and returns the effects.
    private func run(_ core: inout CameraCore, _ slots: [String]) -> [CameraCore.Effect?] {
        slots.map { core.select($0) }
    }

    func testViewfinderLabels() {
        var camera = CameraCore()
        XCTAssertEqual(camera.screen, .viewfinder)
        XCTAssertEqual(camera.labels, ["Shutter", "More"])
        XCTAssertEqual(camera.enabled, [true, true])
        XCTAssertEqual(camera.trainingLabel, "Shutter")
        XCTAssertEqual(camera.select("t1"), .openMenu)
        XCTAssertEqual(camera.screen, .cameraMenu)
        XCTAssertEqual(camera.labels, ["Flip camera", "Back"])
        camera.photoAdded()
        XCTAssertEqual(camera.labels, ["Flip camera", "Gallery"])
    }

    func testNeverMoreThanTwoLabelsOnAnyScreen() {
        var camera = CameraCore(photoCount: 2)
        for slots in [[], ["t1"], ["t1", "t1"], ["t1", "t1", "t1"], ["t1", "t1", "t1", "t0"]] {
            camera = CameraCore(photoCount: 2)
            _ = run(&camera, slots)
            XCTAssertEqual(camera.labels.count, 2, "\(camera.screen)")
            XCTAssertEqual(camera.enabled.count, 2, "\(camera.screen)")
        }
    }

    func testShutterCapturesWithTheCurrentLensAndWaitsForPhotoAdded() {
        var camera = CameraCore()
        XCTAssertEqual(camera.select("t0"), .capture(.back))
        XCTAssertEqual(camera.photoCount, 0, "the photo counts once the app saved it")
        camera.photoAdded()
        XCTAssertEqual(camera.photoCount, 1)
        XCTAssertEqual(camera.screen, .viewfinder)
    }

    func testFlipTogglesLensFromTheMenu() {
        var camera = CameraCore()
        XCTAssertEqual(run(&camera, ["t1", "t0", "t0", "t1", "t0", "t0"]),
                       [.openMenu, .flip(to: .front), .capture(.front),
                        .openMenu, .flip(to: .back), .capture(.back)])
        XCTAssertEqual(camera.screen, .viewfinder)
        XCTAssertEqual(CameraCore(lens: .front).lens, .front)
    }

    func testMenuWithoutPhotosGoesBack() {
        var camera = CameraCore()
        XCTAssertEqual(run(&camera, ["t1", "t1"]), [.openMenu, .closeMenu])
        XCTAssertEqual(camera.screen, .viewfinder)
    }

    func testUnknownSlotsIgnoredOnEveryScreen() {
        var camera = CameraCore(photoCount: 2)
        for setup in [[], ["t1"], ["t1", "t1"], ["t1", "t1", "t1"], ["t1", "t1", "t1", "t0"]] {
            camera = CameraCore(photoCount: 2)
            _ = run(&camera, setup)
            let before = camera
            for slot in ["t2", "t3", "s1", "", "T0"] {
                XCTAssertNil(camera.select(slot), slot)
            }
            XCTAssertEqual(camera, before)
        }
    }

    func testGalleryOpensOnNewestAndNextWrapsToOlder() {
        var camera = CameraCore(photoCount: 3)
        XCTAssertEqual(run(&camera, ["t1", "t1"]), [.openMenu, .openGallery(photo: 2)])
        XCTAssertEqual(camera.screen, .gallery)
        XCTAssertTrue(camera.screen.showsPhoto)
        XCTAssertEqual(camera.labels, ["Next photo", "More"])
        XCTAssertEqual(run(&camera, ["t0", "t0", "t0", "t0"]),
                       [.showPhoto(1), .showPhoto(0), .showPhoto(2), .showPhoto(1)])
        XCTAssertEqual(camera.photoIndex, 1)
    }

    func testNextWithOnePhotoStays() {
        var camera = CameraCore(photoCount: 1)
        XCTAssertEqual(run(&camera, ["t1", "t1", "t0", "t0"]),
                       [.openMenu, .openGallery(photo: 0), .showPhoto(0), .showPhoto(0)])
    }

    func testBackToCameraAndReopenOnNewest() {
        var camera = CameraCore(photoCount: 3)
        XCTAssertEqual(run(&camera, ["t1", "t1", "t0", "t1"]),
                       [.openMenu, .openGallery(photo: 2), .showPhoto(1), .openMenu])
        XCTAssertEqual(camera.screen, .galleryMenu)
        XCTAssertEqual(camera.labels, ["Delete", "Back to camera"])
        XCTAssertEqual(camera.select("t1"), .closeGallery)
        XCTAssertEqual(camera.screen, .viewfinder)
        camera.photoAdded()
        XCTAssertEqual(run(&camera, ["t1", "t1"]), [.openMenu, .openGallery(photo: 3)])
    }

    func testDeleteAsksFirstAndKeepReturns() {
        var camera = CameraCore(photoCount: 2)
        XCTAssertEqual(run(&camera, ["t1", "t1", "t1", "t0"]),
                       [.openMenu, .openGallery(photo: 1), .openMenu, .confirmDelete(photo: 1)])
        XCTAssertEqual(camera.screen, .confirmDelete)
        XCTAssertEqual(camera.labels, ["Delete", "Keep"])
        XCTAssertEqual(camera.enabled, [true, true])
        XCTAssertEqual(camera.select("t1"), .keep(photo: 1))
        XCTAssertEqual(camera.screen, .gallery)
        XCTAssertEqual(camera.photoCount, 2)
        XCTAssertEqual(camera.photoIndex, 1)
    }

    func testDeleteShowsTheNextOlderPhoto() {
        // (photos, photo shown when Delete is confirmed, deleted, next shown, count after)
        let table: [(Int, Int, Int, Int?, Int)] = [
            (3, 2, 2, 1, 2),    // newest: the one before it
            (3, 1, 1, 0, 2),    // middle: the older one keeps its number
            (3, 0, 0, 1, 2),    // oldest: wraps to the newest, now numbered 1
            (2, 0, 0, 0, 1),    // one left
            (1, 0, 0, nil, 0),  // the last photo: back to the viewfinder
        ]
        for (count, shown, deleted, next, after) in table {
            var camera = CameraCore(photoCount: count)
            _ = run(&camera, ["t1", "t1"])
            while camera.photoIndex != shown { _ = camera.select("t0") }
            XCTAssertEqual(run(&camera, ["t1", "t0"]), [.openMenu, .confirmDelete(photo: shown)])
            XCTAssertEqual(camera.select("t0"), .delete(photo: deleted, next: next), "\(count)/\(shown)")
            XCTAssertEqual(camera.photoCount, after)
            if let next {
                XCTAssertEqual(camera.screen, .gallery)
                XCTAssertEqual(camera.photoIndex, next)
            } else {
                XCTAssertEqual(camera.screen, .viewfinder)
                XCTAssertEqual(run(&camera, ["t1"]), [.openMenu])
                XCTAssertEqual(camera.labels[1], "Back")
            }
        }
    }

    func testDeleteEverythingOneByOne() {
        var camera = CameraCore(photoCount: 3)
        _ = run(&camera, ["t1", "t1"])
        var deletes = 0
        while camera.screen == .gallery {
            _ = run(&camera, ["t1", "t0", "t0"])
            deletes += 1
        }
        XCTAssertEqual(deletes, 3)
        XCTAssertEqual(camera.photoCount, 0)
        XCTAssertEqual(camera.screen, .viewfinder)
    }

    func testResetGoesToViewfinderKeepingPhotosAndLens() {
        var camera = CameraCore(photoCount: 2, lens: .front)
        _ = run(&camera, ["t1", "t1", "t1", "t0"])
        XCTAssertEqual(camera.screen, .confirmDelete)
        camera.reset()
        XCTAssertEqual(camera.screen, .viewfinder)
        XCTAssertEqual(camera.photoCount, 2)
        XCTAssertEqual(camera.lens, .front)
    }

    func testNegativePhotoCountIsZero() {
        XCTAssertEqual(CameraCore(photoCount: -1).photoCount, 0)
    }
}
