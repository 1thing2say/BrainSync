import CoreGraphics
import Foundation
import Testing
@testable import BrainPointCore

private let win = CGRect(x: 100, y: 50, width: 800, height: 600)

@Test func toScreenAndBack() {
    let r = NormRect(x: 0.25, y: 0.5, w: 0.1, h: 0.2)
    let s = Geometry.toScreen(r, window: win)
    #expect(s == CGRect(x: 300, y: 350, width: 80, height: 120))
    let back = Geometry.toNorm(s, window: win)
    #expect(abs(back.x - 0.25) < 1e-12 && abs(back.y - 0.5) < 1e-12 && abs(back.w - 0.1) < 1e-12 && abs(back.h - 0.2) < 1e-12)
    #expect(Geometry.pointToScreen(x: 0.5, y: 0.5, window: win) == CGPoint(x: 500, y: 350))
}

@Test func geometryClamps() {
    // hanging over the right and bottom edges: cut at the window
    #expect(Geometry.toScreen(NormRect(x: 0.9, y: 0.95, w: 0.3, h: 0.3), window: win) == CGRect(x: 820, y: 620, width: 80, height: 30))
    // negative origin and NaN
    #expect(Geometry.toScreen(NormRect(x: -0.5, y: .nan, w: 1, h: 0.5), window: win) == CGRect(x: 100, y: 50, width: 400, height: 300))
    #expect(Geometry.toScreen(NormRect(x: 0, y: 0, w: 1, h: 1), window: .zero).size == .zero)
    #expect(Geometry.pointToScreen(x: 2, y: -1, window: win) == CGPoint(x: 900, y: 50))
    #expect(Geometry.toNorm(CGRect(x: 0, y: 0, width: 50, height: 50), window: win) == NormRect(x: 0, y: 0, w: 0, h: 0))
    #expect(Geometry.toNorm(CGRect(x: 1, y: 1, width: 1, height: 1), window: .zero) == NormRect(x: 0, y: 0, w: 0, h: 0))
    #expect(Geometry.toWindow(CGRect(x: 850, y: 600, width: 100, height: 100), window: win) == CGRect(x: 750, y: 550, width: 50, height: 50))
    #expect(Geometry.toWindow(CGRect(x: 0, y: 0, width: 50, height: 50), window: win) == nil)
    #expect(Geometry.toWindow(CGRect(x: 200, y: 200, width: 0, height: 10), window: win) == nil)
}

private func item(_ id: Int, _ frame: CGRect, label: String = "OK", role: String = "AXButton",
                  subrole: String = "", enabled: Bool = true, actions: [String] = ["AXPress"]) -> AXItem {
    AXItem(id: id, role: role, subrole: subrole, label: label, frame: frame, enabled: enabled, actions: actions)
}

@Test func hintsAreWindowRelativeRoundedAndFiltered() {
    let longLabel = String(repeating: "é", count: 130)
    let items = [
        item(0, CGRect(x: 110.4, y: 60.6, width: 80.2, height: 30.5), label: "  Sign\n in  ", subrole: "AXCloseButton"),
        item(1, CGRect(x: 300, y: 300, width: 0, height: 20)),                 // zero size
        item(2, CGRect(x: 2000, y: 300, width: 40, height: 20)),               // outside the window
        item(3, CGRect(x: 880, y: 640, width: 40, height: 20), label: longLabel, enabled: false,
             actions: (0..<12).map { "A\($0)" }),                                // cut at the window's corner
        item(4, CGRect(x: 120, y: 70, width: 0.3, height: 0.3)),               // rounds to nothing
    ]
    let s = WindowSnapshot(appName: "TextEdit", bundleID: "com.apple.TextEdit", pid: 42, windowTitle: "Untitled",
                           windowFrame: win, items: items, walkMs: 12, truncated: false)
    let h = Hints.make(s)
    #expect(h.count == 2)
    #expect(h[0]["id"] as? Int == 0)
    #expect(h[0]["x"] as? Int == 10 && h[0]["y"] as? Int == 11)
    #expect(h[0]["w"] as? Int == 80 && h[0]["h"] as? Int == 31)
    #expect(h[0]["text"] as? String == "Sign in")
    #expect(h[0]["role"] as? String == "AXButton")
    #expect(h[0]["tag"] as? String == "AXCloseButton")
    #expect(h[0]["enabled"] as? Bool == true)
    #expect(h[0]["actions"] as? [String] == ["AXPress"])
    #expect(h[1]["id"] as? Int == 3)
    #expect(h[1]["x"] as? Int == 780 && h[1]["w"] as? Int == 20 && h[1]["h"] as? Int == 10)
    #expect((h[1]["text"] as? String)?.unicodeScalars.count == 120)
    #expect((h[1]["actions"] as? [String])?.count == 8)
    #expect(h[1]["enabled"] as? Bool == false)
    #expect(Codec.encode(["hints": h]) != nil)
}

@Test func hintsCapAt200() {
    let items = (0..<250).map { item($0, CGRect(x: 100 + Double($0 % 20) * 10, y: 50 + Double($0 / 20) * 10, width: 8, height: 8)) }
    let s = WindowSnapshot(appName: "A", bundleID: "a", pid: 1, windowTitle: "", windowFrame: win, items: items, walkMs: 1, truncated: true)
    #expect(Hints.make(s).count == 200)
}
