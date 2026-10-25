import Foundation
import Testing
@testable import BrainPointCore

private struct Fixture: Decodable {
    struct Case: Decodable { let waveform: String; let freq: Double; let phase: Double; let depth: Double; let refresh: Double; let values: [Double] }
    let source: String
    let frames: Int
    let cases: [Case]
}

private func loadFixture() throws -> Fixture {
    let url = try #require(Bundle.module.url(forResource: "flicker_fixture", withExtension: "json", subdirectory: "Fixtures"))
    return try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
}

@Test func fixtureCoversEveryCase() throws {
    let f = try loadFixture()
    #expect(f.cases.count == 24)
    #expect(f.frames == 241)
    #expect(Set(f.cases.map { "\($0.waveform):\($0.freq):\($0.phase):\($0.depth)" }).count == 24)
    #expect(Set(f.cases.map(\.depth)) == [0.5, 1.0])
}

/// The same tolerance as extension/test/flicker.test.mjs (1e-9); in practice the port is
/// bit-exact or within an ulp or two of numpy's sin.
@Test func luminanceMatchesPythonFixture() throws {
    let f = try loadFixture()
    var worst = 0.0, exact = 0, total = 0
    for c in f.cases {
        #expect(c.values.count == f.frames)
        for (n, want) in c.values.enumerated() {
            let got = Luminance.value(freq: c.freq, phase: c.phase, frame: n, refresh: c.refresh,
                                      waveform: c.waveform, depth: c.depth)
            worst = max(worst, abs(got - want))
            if got == want { exact += 1 }
            total += 1
        }
    }
    #expect(worst < 1e-9, "max abs diff \(worst)")
    #expect(worst <= 4.5e-16, "max abs diff \(worst) is more than a couple of ulps")
    print("luminance fixture: \(exact)/\(total) values bit-exact, max abs diff \(worst)")
}

@Test func defaultsAndBadArguments() {
    #expect(Luminance.value(freq: 15, phase: 0, frame: 1, refresh: 60) == Luminance.value(freq: 15, phase: 0, frame: 1, refresh: 60, waveform: "sine"))
    #expect(Luminance.value(freq: 15, phase: 0, frame: 1, refresh: 60, waveform: "sine", depth: 1) == 1)
    #expect(Luminance.value(freq: 15, phase: 0, frame: 3, refresh: 60, waveform: "sine", depth: 1) == 0)
    // flicker.js throws on these; the Swift port draws still instead
    #expect(Luminance.value(freq: 15, phase: 0, frame: 1, refresh: 0) == 0.5)
    #expect(Luminance.value(freq: 15, phase: 0, frame: 1, refresh: 60, waveform: "triangle") == 0.5)
    for d in [0, -0.1, 1.2, Double.nan, .infinity] {
        #expect(Luminance.value(freq: 15, phase: 0, frame: 1, refresh: 60, depth: d) == 0.5)
        #expect(!Luminance.isValid(refresh: 60, depth: d))
    }
}

@Test func squareAndDepth() {
    let seq = (0..<8).map { Luminance.value(freq: 15, phase: 0, frame: $0, refresh: 60, waveform: "square") }
    #expect(seq == [1, 1, 0, 0, 1, 1, 0, 0])
    let half = (0..<4).map { Luminance.value(freq: 15, phase: 0, frame: $0, refresh: 60, waveform: "square", depth: 0.5) }
    #expect(half == [0.75, 0.75, 0.25, 0.25])
    var sum = 0.0
    for n in 0..<600 {
        let v = Luminance.value(freq: 17, phase: 0.3, frame: n, refresh: 60, waveform: "sine", depth: 0.5)
        #expect(v >= 0.25 && v <= 0.75)
        sum += v
    }
    #expect(abs(sum / 600 - 0.5) < 1e-12)
}

@Test func pyModAndPhotosensitive() {
    #expect(Luminance.pyMod(-0.25, 1) == 0.75)
    #expect(Luminance.pyMod(1.25, 1) == 0.25)
    #expect(Luminance.pyMod(2, 1) == 0)
    #expect([12.0, 15, 17, 25, 26].map(Luminance.photosensitive) == [false, true, true, true, false])
}

@Test func flickerMath() {
    #expect(FlickerMath.frameIndex(t: 10.0 + 4.0 / 60, t0: 10.0, refresh: 60) == 4)
    #expect(FlickerMath.isDropped(interval: 2.0 / 60, refresh: 60))
    #expect(!FlickerMath.isDropped(interval: 1.2 / 60, refresh: 60))
    #expect(FlickerMath.lumHz(measured: 59.94, nominal: 60, tolerance: 1) == 60)
    #expect(FlickerMath.lumHz(measured: 120.1, nominal: 60, tolerance: 1) == 120.1)
    #expect(FlickerMath.lumHz(measured: nil, nominal: 60, tolerance: 1) == 60)
    #expect(FlickerMath.canShow(freq: 20, refresh: 60))
    #expect(!FlickerMath.canShow(freq: 30, refresh: 60))
}

@Test func refreshMeterPort() {
    var m = RefreshMeter(size: 120, warmup: 20)
    for _ in 0..<20 { let took = m.push(1.0 / 60); #expect(!took) }
    #expect(m.hz() == nil)
    for bad in [0, Double.nan, -1] { let took = m.push(bad); #expect(!took) }
    for i in 0..<120 { m.push(i % 2 == 0 ? 0.0166 : 0.0167) }
    #expect(m.ready && m.due)
    let hz = m.hz()!
    #expect(abs(hz - 1 / 0.01665) < 1e-9)
    // an outlier (a dropped frame) is left out of the mean
    m.push(0.05)
    #expect(!m.due)
    #expect(abs(m.hz()! - hz) < 0.01)
    var ms = RefreshMeter(size: 4, warmup: 0)
    for v in [16.0, 17.0, 16.0, 17.0] { ms.push(v) }
    #expect(abs(ms.hz(unit: 1000)! - 1000 / 16.5) < 1e-9)
    #expect(RefreshMeter.median([3, 1, 2]) == 2)
    #expect(RefreshMeter.median([4, 1, 2, 3]) == 2.5)
}
