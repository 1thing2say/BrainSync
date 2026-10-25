import Foundation

/// The Camera app's logic: which screen is up, which lens is in use, how many photos there are
/// and which one the gallery shows. It never touches the camera or the photo files: each
/// selection returns an `Effect` the app carries out, and the app reports a finished capture
/// with `photoAdded()`.
///
/// Photos are numbered in the order they were taken: 0 is the oldest, `photoCount - 1` the
/// newest. The gallery opens on the newest, and Next photo steps back in time (to the next
/// older one), wrapping from the oldest to the newest, like swiping through a camera roll.
///
/// | Screen        | t0         | t1             | t2                      |
/// | ------------- | ---------- | -------------- | ----------------------- |
/// | viewfinder    | Shutter    | Flip camera    | Gallery (No photos: off)|
/// | gallery       | Next photo | Back to camera | Delete                  |
/// | confirmDelete | Delete     | (off)          | Keep                    |
///
/// Delete needs two looks (Delete, then Delete again on the confirm screen), so a single
/// misread never loses a photo. Keep sits on t2, where the user is already looking after
/// choosing Delete, so the likeliest repeat look keeps the photo.
public struct CameraCore: SuiteAppCore, Equatable, Sendable {
    public enum Screen: Equatable, Sendable {
        case viewfinder, gallery, confirmDelete
    }

    public enum Lens: Equatable, Sendable {
        case back, front

        var flipped: Lens { self == .back ? .front : .back }
    }

    /// What the app does after a selection. Photo numbers follow the order described above.
    public enum Effect: Equatable, Sendable {
        /// Take a photo with this lens; call `photoAdded()` once it is saved.
        case capture(Lens)
        /// Switch the viewfinder to this lens.
        case flip(to: Lens)
        /// Open the gallery on this photo.
        case openGallery(photo: Int)
        /// Show this photo in the gallery.
        case showPhoto(Int)
        /// Leave the gallery for the viewfinder.
        case closeGallery
        /// Ask whether to delete this photo (the confirm screen).
        case confirmDelete(photo: Int)
        /// Delete this photo, then show `next` in the gallery, or the viewfinder when `next` is
        /// nil (it was the last photo). `next` is numbered after the deletion.
        case delete(photo: Int, next: Int?)
        /// The photo stays; back to the gallery on it.
        case keep(photo: Int)
    }

    public private(set) var screen: Screen = .viewfinder
    public private(set) var lens: Lens
    public private(set) var photoCount: Int
    /// The photo the gallery and the confirm screen show (meaningless in the viewfinder).
    public private(set) var photoIndex: Int = 0

    /// `photoCount`: photos already saved when the app opens.
    public init(photoCount: Int = 0, lens: Lens = .back) {
        self.photoCount = max(0, photoCount)
        self.lens = lens
    }

    /// A capture finished and the photo is saved: it is the newest one.
    public mutating func photoAdded() {
        photoCount += 1
    }

    /// Back to the viewfinder, keeping the photos and the lens (for example when the app is
    /// opened again from Home).
    public mutating func reset() {
        screen = .viewfinder
    }

    public var labels: [String] {
        switch screen {
        case .viewfinder: return ["Shutter", "Flip camera", photoCount > 0 ? "Gallery" : "No photos"]
        case .gallery: return ["Next photo", "Back to camera", "Delete"]
        case .confirmDelete: return ["Delete", "", "Keep"]
        }
    }

    public var enabled: [Bool] {
        switch screen {
        case .viewfinder: return [true, true, photoCount > 0]
        case .gallery: return [true, true, true]
        case .confirmDelete: return [true, false, true]
        }
    }

    public var trainingLabel: String { "Shutter" }

    public mutating func select(_ slot: String) -> Effect? {
        guard let target = SuiteSlots.index(of: slot), enabled[target] else { return nil }
        switch (screen, target) {
        case (.viewfinder, 0):
            return .capture(lens)
        case (.viewfinder, 1):
            lens = lens.flipped
            return .flip(to: lens)
        case (.viewfinder, _):
            screen = .gallery
            photoIndex = photoCount - 1
            return .openGallery(photo: photoIndex)
        case (.gallery, 0):
            photoIndex = older(than: photoIndex, count: photoCount)
            return .showPhoto(photoIndex)
        case (.gallery, 1):
            screen = .viewfinder
            return .closeGallery
        case (.gallery, _):
            screen = .confirmDelete
            return .confirmDelete(photo: photoIndex)
        case (.confirmDelete, 0):
            let deleted = photoIndex
            photoCount -= 1
            guard photoCount > 0 else {
                screen = .viewfinder
                photoIndex = 0
                return .delete(photo: deleted, next: nil)
            }
            // Show the next older photo, as Next photo would have; numbers above `deleted`
            // shift down by one, numbers below it stay.
            screen = .gallery
            photoIndex = older(than: deleted, count: photoCount)
            return .delete(photo: deleted, next: photoIndex)
        case (.confirmDelete, _):
            screen = .gallery
            return .keep(photo: photoIndex)
        }
    }

    /// The photo before `index`, wrapping from the oldest (0) to the newest.
    private func older(than index: Int, count: Int) -> Int {
        index > 0 ? index - 1 : count - 1
    }
}
