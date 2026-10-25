import CoreGraphics
import Foundation
import Testing
@testable import BrainPointCore

@Test func scaledSizeKeepsAspectAndLongEdge() {
    // a 1512 x 857 pt viewport -> long edge 1280
    let s = CaptureMath.scaledSize(width: 1512, height: 857, maxEdge: 1280)
    #expect(s?.w == 1280 && s?.h == 726)   // 725.5 rounds up
    // portrait
    let p = CaptureMath.scaledSize(width: 900, height: 2000, maxEdge: 1280)
    #expect(p?.w == 576 && p?.h == 1280)
    // smaller than maxEdge: unchanged, never scaled up
    let small = CaptureMath.scaledSize(width: 640, height: 480, maxEdge: 1280)
    #expect(small?.w == 640 && small?.h == 480)
    // a sliver keeps at least 1 px
    let sliver = CaptureMath.scaledSize(width: 10000, height: 1, maxEdge: 1280)
    #expect(sliver?.w == 1280 && sliver?.h == 1)
}

@Test func scaledSizeRejectsBadInput() {
    #expect(CaptureMath.scaledSize(width: 0, height: 100, maxEdge: 1280) == nil)
    #expect(CaptureMath.scaledSize(width: -5, height: 100, maxEdge: 1280) == nil)
    #expect(CaptureMath.scaledSize(width: .nan, height: 100, maxEdge: 1280) == nil)
    #expect(CaptureMath.scaledSize(width: 100, height: .infinity, maxEdge: 1280) == nil)
    #expect(CaptureMath.scaledSize(width: 100, height: 100, maxEdge: 0) == nil)
}

@Test func sourceRectIsDisplayLocalAndCut() {
    let second = CGRect(x: 1512, y: -200, width: 1920, height: 1080)
    #expect(CaptureMath.sourceRect(CGRect(x: 1600, y: 0, width: 400, height: 300), onDisplay: second)
            == CGRect(x: 88, y: 200, width: 400, height: 300))
    // hanging off the left edge of the display: cut
    #expect(CaptureMath.sourceRect(CGRect(x: 1400, y: 0, width: 400, height: 300), onDisplay: second)
            == CGRect(x: 0, y: 200, width: 288, height: 300))
    #expect(CaptureMath.sourceRect(CGRect(x: 0, y: 0, width: 100, height: 100), onDisplay: second) == nil)
}

@Test func displayIndexPrefersCentreThenOverlap() {
    let main = CGRect(x: 0, y: 0, width: 1512, height: 982)
    let right = CGRect(x: 1512, y: 0, width: 1920, height: 1080)
    #expect(CaptureMath.displayIndex(for: CGRect(x: 100, y: 100, width: 200, height: 200), displays: [main, right]) == 0)
    #expect(CaptureMath.displayIndex(for: CGRect(x: 1400, y: 100, width: 600, height: 200), displays: [main, right]) == 1)
    // centre in a gap below both: largest overlap wins
    #expect(CaptureMath.displayIndex(for: CGRect(x: 1500, y: 900, width: 100, height: 400), displays: [main, right]) == 1)
    #expect(CaptureMath.displayIndex(for: CGRect(x: -900, y: -900, width: 10, height: 10), displays: [main, right]) == nil)
}

@Test func redactRectMapsIntoImagePixels() {
    let captured = CGRect(x: 100, y: 50, width: 1512, height: 857)   // -> 1280 x 725
    let r = CaptureMath.imageRect(of: CGRect(x: 100, y: 50, width: 151.2, height: 85.7),
                                  captured: captured, imageW: 1280, imageH: 725)
    #expect(r == CGRect(x: 0, y: 0, width: 128, height: 73))   // 72.5 rounded out
    // hanging over the bottom-right: cut at the image
    let edge = CaptureMath.imageRect(of: CGRect(x: 1500, y: 850, width: 500, height: 500),
                                     captured: captured, imageW: 1280, imageH: 725)
    #expect(edge?.maxX == 1280 && edge?.maxY == 725)
    // outside
    #expect(CaptureMath.imageRect(of: CGRect(x: 0, y: 0, width: 50, height: 40),
                                  captured: captured, imageW: 1280, imageH: 725) == nil)
}

@Test func flippedForCGContext() {
    #expect(CaptureMath.flipped(CGRect(x: 10, y: 0, width: 20, height: 30), imageH: 100)
            == CGRect(x: 10, y: 70, width: 20, height: 30))
}
