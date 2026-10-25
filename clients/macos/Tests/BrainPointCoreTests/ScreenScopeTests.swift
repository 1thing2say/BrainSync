import CoreGraphics
import Foundation
import Testing
@testable import BrainPointCore

// Screen scope (Phase 4): the scan's viewport is the screen holding the focused window, and
// the hints add the menu bar, open menus, the Dock and two scroll strips.

private let primary = CGRect(x: 0, y: 0, width: 1512, height: 982)
/// A second display to the right, 1080 high, its bottom 100 pt above the primary's bottom.
private let secondCocoa = CGRect(x: 1512, y: 100, width: 1920, height: 1080)
private let second = CGRect(x: 1512, y: -198, width: 1920, height: 1080)
private let win = CGRect(x: 100, y: 50, width: 800, height: 600)

private func item(_ id: Int, _ kind: ItemKind, _ frame: CGRect, label: String = "x", role: String = "AXButton",
                  subrole: String = "", enabled: Bool = true, actions: [String] = ["AXPress"]) -> AXItem {
    AXItem(id: id, role: role, subrole: subrole, label: label, frame: frame, enabled: enabled, actions: actions, kind: kind)
}

@Test func cocoaFramesBecomeTopLeft() {
    #expect(ScreenScope.toTopLeft(cocoa: primary, primaryHeight: 982) == primary)
    #expect(ScreenScope.toTopLeft(cocoa: secondCocoa, primaryHeight: 982) == second)
    // the primary's visible frame without a 33 pt menu bar and an 80 pt Dock
    #expect(ScreenScope.toTopLeft(cocoa: CGRect(x: 0, y: 80, width: 1512, height: 869), primaryHeight: 982)
            == CGRect(x: 0, y: 33, width: 1512, height: 869))
}

@Test func theScreenHoldingMostOfTheWindowWins() {
    let screens = [primary, second]
    #expect(ScreenScope.screenIndex(for: win, screens: screens) == 0)
    #expect(ScreenScope.screenIndex(for: CGRect(x: 1400, y: 100, width: 800, height: 400), screens: screens) == 1)
    #expect(ScreenScope.screenIndex(for: CGRect(x: 1000, y: 100, width: 800, height: 400), screens: screens) == 0)
    // nowhere on a screen: the first
    #expect(ScreenScope.screenIndex(for: CGRect(x: -5000, y: 0, width: 10, height: 10), screens: screens) == 0)
    #expect(ScreenScope.screenIndex(for: win, screens: []) == nil)
}

@Test func scrollStripsSitInsideTheAreaTopAndBottom() throws {
    let s = try #require(ScreenScope.scrollStrips(area: CGRect(x: 100, y: 100, width: 800, height: 600)))
    #expect(s.up == CGRect(x: 380, y: 104, width: 240, height: 32))
    #expect(s.down == CGRect(x: 380, y: 664, width: 240, height: 32))
    // a narrow, short area: half its width, a sixth of its height
    let n = try #require(ScreenScope.scrollStrips(area: CGRect(x: 0, y: 0, width: 100, height: 120)))
    #expect(n.up == CGRect(x: 25, y: 4, width: 50, height: 20) && n.down == CGRect(x: 25, y: 96, width: 50, height: 20))
    #expect(ScreenScope.scrollStrips(area: CGRect(x: 0, y: 0, width: 30, height: 600)) == nil)
    #expect(ScreenScope.scrollStrips(area: .null) == nil)
}

@Test func theDockCountsOnlyWhenShownOnThatScreen() {
    let list = CGRect(x: 400, y: 905, width: 700, height: 72)
    let shown = CGRect(x: 0, y: 33, width: 1512, height: 869)       // the Dock takes the bottom 80 pt
    let autoHidden = CGRect(x: 0, y: 33, width: 1512, height: 945)  // only 4 pt left for it
    #expect(ScreenScope.dockVisible(listFrame: list, screen: primary, visibleFrame: shown))
    #expect(!ScreenScope.dockVisible(listFrame: list, screen: primary, visibleFrame: autoHidden))
    #expect(!ScreenScope.dockVisible(listFrame: list, screen: second, visibleFrame: second))
    #expect(ScreenScope.onScreen(CGRect(x: 1500, y: 10, width: 20, height: 20), screen: primary)
            && !ScreenScope.onScreen(CGRect(x: 1505, y: 10, width: 20, height: 20), screen: primary))
}

@Test func assembleOrdersByKindAndRenumbers() {
    let entries: [(item: AXItem, payload: String)] = [
        (item(0, .dock, CGRect(x: 500, y: 920, width: 50, height: 50), label: "Notes", role: "AXDockItem"), "dock0"),
        (item(0, .window, CGRect(x: 120, y: 70, width: 40, height: 20), label: "Back"), "win0"),
        (item(1, .window, CGRect(x: 170, y: 70, width: 40, height: 20), label: "Forward"), "win1"),
        (item(0, .menuBar, CGRect(x: 40, y: 0, width: 40, height: 24), label: "File", role: "AXMenuBarItem"), "bar0"),
        (item(0, .scroll, CGRect(x: 380, y: 104, width: 240, height: 32), label: "Scroll up", role: "AXScrollArea",
              subrole: "BPScrollUp", actions: ["AXScrollUpByPage"]), "up"),
        (item(0, .menu, CGRect(x: 40, y: 30, width: 200, height: 22), label: "New Window", role: "AXMenuItem"), "menu0"),
    ]
    let (out, truncated) = ScreenScope.assemble(entries)
    #expect(!truncated)
    #expect(out.map(\.payload) == ["menu0", "win0", "win1", "up", "bar0", "dock0"])
    #expect(out.map(\.item.id) == [0, 1, 2, 3, 4, 5])
    #expect(out.map(\.item.kind) == [.menu, .window, .window, .scroll, .menuBar, .dock])
}

@Test func assembleCapsAt200WithTheWindowFirst() {
    func many(_ kind: ItemKind, _ n: Int) -> [(item: AXItem, payload: Int)] {
        (0..<n).map { (item: item($0, kind, CGRect(x: 0, y: 0, width: 10, height: 10)), payload: $0) }
    }
    // a busy window leaves room for the 40 others
    let a = ScreenScope.assemble(many(.window, 250) + many(.dock, 30) + many(.menuBar, 8) + many(.scroll, 2))
    #expect(a.entries.count == 200 && a.truncated)
    #expect(a.entries.filter { $0.item.kind == .window }.count == 160)
    #expect(a.entries.filter { $0.item.kind == .dock }.count == 30)
    #expect(a.entries.prefix(160).allSatisfy { $0.item.kind == .window })
    #expect(a.entries.filter { $0.item.kind == .window }.map(\.payload) == Array(0..<160))   // the first ones kept
    // too many others: the window keeps half, the Dock (last) is cut
    let b = ScreenScope.assemble(many(.window, 150) + many(.menu, 90) + many(.dock, 40))
    #expect(b.entries.count == 200 && b.truncated)
    #expect(b.entries.filter { $0.item.kind == .window }.count == 100)
    #expect(b.entries.filter { $0.item.kind == .menu }.count == 90)
    #expect(b.entries.filter { $0.item.kind == .dock }.count == 10)
    #expect(b.entries.map(\.item.id) == Array(0..<200))
    // a small window is never cut
    let c = ScreenScope.assemble(many(.window, 50) + many(.dock, 30))
    #expect(c.entries.count == 80 && !c.truncated)
}

private func screenSnapshot(screen: CGRect = primary, window: CGRect = win) -> WindowSnapshot {
    let wx = window.minX, wy = window.minY
    let items = [
        item(0, .window, CGRect(x: wx + 10, y: wy + 10, width: 40, height: 20), label: "Back"),
        item(1, .scroll, CGRect(x: wx + 280, y: wy + 54, width: 240, height: 32), label: "Scroll up",
             role: "AXScrollArea", subrole: "BPScrollUp", actions: ["AXScrollUpByPage"]),
        item(2, .scroll, CGRect(x: wx + 280, y: wy + 564, width: 240, height: 32), label: "Scroll down",
             role: "AXScrollArea", subrole: "BPScrollDown", enabled: false, actions: ["AXScrollDownByPage"]),
        item(3, .menuBar, CGRect(x: screen.minX + 60, y: screen.minY, width: 40, height: 24), label: "File",
             role: "AXMenuBarItem"),
        item(4, .dock, CGRect(x: screen.minX + 400, y: screen.maxY - 70, width: 60, height: 60), label: "Notes",
             role: "AXDockItem", subrole: "AXApplicationDockItem", actions: ["AXPress", "AXShowMenu"]),
        item(5, .dock, CGRect(x: screen.maxX + 400, y: 900, width: 60, height: 60), label: "Elsewhere",
             role: "AXDockItem"),   // on no part of this screen
    ]
    return WindowSnapshot(appName: "Safari", bundleID: "com.apple.Safari", pid: 9, windowTitle: "Start",
                          windowFrame: window, items: items, walkMs: 40, truncated: false, screenFrame: screen)
}

@Test func hintsAreRelativeToTheScreen() {
    let s = screenSnapshot()
    #expect(s.viewport == primary)
    #expect(s.countsByKind == [.window: 1, .scroll: 2, .menuBar: 1, .dock: 2])
    let h = Hints.make(s)
    #expect(h.map { $0["id"] as? Int } == [0, 1, 2, 3, 4])
    #expect(h[0]["x"] as? Int == 110 && h[0]["y"] as? Int == 60)          // window item: screen points
    #expect(h[1]["role"] as? String == "AXScrollArea" && h[1]["tag"] as? String == "BPScrollUp")
    #expect(h[1]["actions"] as? [String] == ["AXScrollUpByPage"] && h[1]["text"] as? String == "Scroll up")
    #expect(h[2]["enabled"] as? Bool == false)
    #expect(h[3]["x"] as? Int == 60 && h[3]["y"] as? Int == 0 && h[3]["role"] as? String == "AXMenuBarItem")
    #expect(h[4]["y"] as? Int == 912 && h[4]["role"] as? String == "AXDockItem" && h[4]["tag"] as? String == "AXApplicationDockItem")
}

@Test func hintsOnASecondScreenAreRelativeToIt() {
    let w = CGRect(x: 1700, y: -100, width: 900, height: 700)
    let h = Hints.make(screenSnapshot(screen: second, window: w))
    #expect(h[0]["x"] as? Int == 198 && h[0]["y"] as? Int == 108)
    #expect(h[3]["x"] as? Int == 60 && h[3]["y"] as? Int == 0)
    #expect(h.count == 5)
}

@Test func windowOnlySnapshotsStillUseTheWindow() {
    var s = screenSnapshot()
    s.screenFrame = .zero
    #expect(s.viewport == win)
    // the menu bar (above the window) and the Dock (below it) fall outside the window
    #expect(Hints.make(s).map { $0["id"] as? Int } == [0, 1, 2])
}
