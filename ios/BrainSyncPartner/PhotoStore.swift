// The Camera app's own gallery: JPEGs in Application Support/Photos, oldest first, newest last.
// The files are named after the time they were taken in milliseconds (IMG-<13 digits>.jpg), so
// sorting by name is sorting by time and a photo's date needs no extra file. Encoding, writing
// and decoding run off the main thread, which draws the c-VEP squares.
//
// Copying each photo into the system Photos library is optional and off by default
// (`savesToPhotoLibrary`): it needs add-only Photos access, and the app works without it.

import Foundation
import ImageIO
import Observation
import Photos
import UIKit

@MainActor
@Observable
final class PhotoStore {
    /// Every photo's file, oldest first (index count - 1 is the newest).
    private(set) var photos: [URL] = []
    /// Also copy each new photo into the system Photos library (add-only access). Kept in
    /// UserDefaults under `PhotoStore.libraryKey`; off by default.
    var savesToPhotoLibrary: Bool {
        didSet { UserDefaults.standard.set(savesToPhotoLibrary, forKey: Self.libraryKey) }
    }
    /// The last thing that went wrong, in plain words; nil = fine.
    private(set) var lastError: String?
    /// Where the JPEGs live.
    let directory: URL

    nonisolated static let libraryKey = "camera.saveToPhotoLibrary"

    /// Application Support/Photos: private to the app, kept across launches and in backups.
    nonisolated static var defaultDirectory: URL {
        URL.applicationSupportDirectory.appending(path: "Photos", directoryHint: .isDirectory)
    }

    var count: Int { photos.count }
    var isEmpty: Bool { photos.isEmpty }
    /// The newest photo's file.
    var last: URL? { photos.last }

    /// The newest stamp handed out, so two photos in the same millisecond still sort in order.
    @ObservationIgnored private var lastStamp: Int64 = 0

    init(directory: URL = PhotoStore.defaultDirectory) {
        self.directory = directory
        savesToPhotoLibrary = UserDefaults.standard.bool(forKey: Self.libraryKey)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            lastError = "The photo folder could not be created."
        }
        reload()
    }

    /// Re-reads the folder.
    func reload() {
        let files = (try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles])) ?? []
        photos = files.filter { $0.pathExtension.lowercased() == "jpg" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        lastStamp = photos.compactMap(Self.stamp).max() ?? 0
    }

    // MARK: Save and delete

    /// Saves `image` as a JPEG (and to the Photos library when `savesToPhotoLibrary` is on).
    /// Returns the new photo's index (always the last), or nil if it could not be written.
    @discardableResult
    func save(_ image: UIImage, quality: CGFloat = 0.9) async -> Int? {
        let url = nextURL()
        let data: Data? = await Task.detached(priority: .userInitiated) {
            guard let data = image.jpegData(compressionQuality: quality) else { return nil }
            do {
                try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
                return data
            } catch {
                return nil
            }
        }.value
        guard let data else {
            lastError = "The photo could not be saved."
            return nil
        }
        lastError = nil
        photos.append(url)
        photos.sort { $0.lastPathComponent < $1.lastPathComponent }
        if savesToPhotoLibrary {
            await addToLibrary { request in request.addResource(with: .photo, data: data, options: nil) }
        }
        return photos.firstIndex(of: url)
    }

    /// Deletes the photo at `index` from the app's gallery (never from the Photos library).
    @discardableResult
    func delete(at index: Int) -> Bool {
        guard photos.indices.contains(index) else { return false }
        let url = photos[index]
        do {
            try FileManager.default.removeItem(at: url)
        } catch {
            // Gone already (another delete raced it) counts as deleted.
            if FileManager.default.fileExists(atPath: url.path(percentEncoded: false)) {
                lastError = "The photo could not be deleted."
                return false
            }
        }
        photos.remove(at: index)
        lastError = nil
        return true
    }

    /// Copies `image` into the system Photos library, asking for add-only access first if it was
    /// never asked. Returns whether it was added; works whatever `savesToPhotoLibrary` says.
    @discardableResult
    func saveToPhotoLibrary(_ image: UIImage) async -> Bool {
        guard let data = image.jpegData(compressionQuality: 0.9) else { return false }
        return await addToLibrary { request in request.addResource(with: .photo, data: data, options: nil) }
    }

    // MARK: Read

    func url(at index: Int) -> URL? {
        photos.indices.contains(index) ? photos[index] : nil
    }

    /// When the photo at `index` was taken (from its file name).
    func date(at index: Int) -> Date? {
        guard let url = url(at: index), let stamp = Self.stamp(url) else { return nil }
        return Date(timeIntervalSince1970: Double(stamp) / 1000)
    }

    /// The photo at `index`, decoded off the main thread and ready to draw. With
    /// `maxPixelSize` it is scaled down so its longer side is at most that many pixels (use about
    /// 2048 for the gallery, 200 for a thumbnail); nil = full size.
    func image(at index: Int, maxPixelSize: Int? = nil) async -> UIImage? {
        guard let url = url(at: index) else { return nil }
        return await Task.detached(priority: .userInitiated) {
            Self.load(url, maxPixelSize: maxPixelSize)
        }.value
    }

    // MARK: Private

    private func nextURL() -> URL {
        let now = Int64((Date().timeIntervalSince1970 * 1000).rounded(.down))
        lastStamp = max(now, lastStamp + 1)
        return directory.appending(path: String(format: "IMG-%013lld.jpg", lastStamp))
    }

    /// The milliseconds in a file name like IMG-1792809201512.jpg.
    private nonisolated static func stamp(_ url: URL) -> Int64? {
        let name = url.deletingPathExtension().lastPathComponent
        guard name.hasPrefix("IMG-") else { return nil }
        return Int64(name.dropFirst(4))
    }

    private nonisolated static func load(_ url: URL, maxPixelSize: Int?) -> UIImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        if let maxPixelSize {
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,  // apply the EXIF orientation
                kCGImageSourceShouldCacheImmediately: true,
                kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
            ]
            guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
            return UIImage(cgImage: cg)
        }
        guard let image = UIImage(contentsOfFile: url.path(percentEncoded: false)) else { return nil }
        return image.preparingForDisplay() ?? image
    }

    /// Asks for add-only Photos access if needed, then runs one creation request.
    @discardableResult
    private func addToLibrary(_ fill: @escaping @Sendable (PHAssetCreationRequest) -> Void) async -> Bool {
        var status = PHPhotoLibrary.authorizationStatus(for: .addOnly)
        if status == .notDetermined {
            status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        }
        guard status == .authorized || status == .limited else {
            lastError = "Photos access is off: the photo is saved in this app only."
            return false
        }
        do {
            try await PHPhotoLibrary.shared().performChanges {
                fill(PHAssetCreationRequest.forAsset())
            }
            return true
        } catch {
            lastError = "The photo could not be added to Photos."
            return false
        }
    }
}
