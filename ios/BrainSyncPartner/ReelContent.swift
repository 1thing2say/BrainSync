// The Reels app's content: 20 made-up reels, generated from a seed, so nothing is downloaded and
// no licensed media ships. Each one is a gradient, a big number, a caption, an @handle and one
// small moving element that makes it feel like video (the Chrome mock's sweeping bar,
// extension/test/reels_mock.html on cvep3/reels). Reel i always looks the same: the seed is i.

import SwiftUI

/// What moves on a reel. Each is a few shapes, cheap to redraw, so the motion never competes
/// with the 60 Hz display link that paints the c-VEP squares beside it.
enum ReelMotion: CaseIterable, Sendable {
    /// A translucent band sweeping top to bottom (the mock's `.bar`).
    case sweep
    /// A soft disc circling the centre.
    case orbit
    /// Five bars rising and falling, like an equaliser.
    case bars
    /// A ring that grows and fades, over and over.
    case pulse
}

/// One generated reel. Plain values only, so the feed can draw any reel from its index alone.
struct Reel: Identifiable, Sendable {
    /// Position in `ReelContent.all`, from 0.
    let id: Int
    /// The big number on the reel (id + 1, as in the mock).
    var number: Int { id + 1 }
    let caption: String
    let handle: String
    /// Top and bottom of the background gradient, and the moving element's colour.
    let top: Color
    let bottom: Color
    let accent: Color
    let motion: ReelMotion
    /// Seconds one pass of the motion takes (the mock's sweep is 2.4 s).
    let period: Double
    /// Seconds the "video" lasts, for the progress line at the bottom; it loops after that.
    let length: Double
    let likes: Int
}

enum ReelContent {
    /// How many reels the feed has.
    static let count = 20

    /// Every reel, generated once.
    static let all: [Reel] = (0..<count).map(make)

    /// Reel `index`, wrapping both ways, so any index the feed is given has a reel.
    static func reel(_ index: Int) -> Reel {
        all[((index % count) + count) % count]
    }

    /// Builds reel `index` from a generator seeded with the index alone (deterministic: the same
    /// reel on every launch and every device).
    static func make(_ index: Int) -> Reel {
        var rng = SplitMix64(seed: UInt64(index) &* 0x9E37_79B9 &+ 0x5EED)
        // Hues spread round the wheel like the mock's (i * 47) % 360, plus a little seeded jitter.
        let hue = (Double((index * 47) % 360) + rng.unit() * 20) / 360
        let shift = 0.08 + rng.unit() * 0.14
        let top = Color(hue: hue.truncatingRemainder(dividingBy: 1), saturation: 0.62,
                        brightness: 0.46)
        let bottom = Color(hue: (hue + shift).truncatingRemainder(dividingBy: 1),
                           saturation: 0.7, brightness: 0.18)
        let accent = Color(hue: (hue + 0.5).truncatingRemainder(dividingBy: 1),
                           saturation: 0.35, brightness: 1)
        let subject = subjects[rng.below(subjects.count)]
        let ending = endings[rng.below(endings.count)]
        let handle = "@" + handleStarts[rng.below(handleStarts.count)]
            + handleEnds[rng.below(handleEnds.count)]
        return Reel(id: index,
                    caption: "\(subject) \(ending)",
                    handle: handle,
                    top: top, bottom: bottom, accent: accent,
                    motion: ReelMotion.allCases[index % ReelMotion.allCases.count],
                    period: 1.8 + rng.unit() * 1.6,
                    length: Double(8 + rng.below(9)),
                    likes: 120 + rng.below(48_000))
    }

    private static let subjects = [
        "Morning light over the bay", "Tiny kitchen, big pancakes", "Rooftop sunset run",
        "Learning the drums, day 12", "My cat versus the vacuum", "Ferry ride in the fog",
        "One-pan pasta in 15 minutes", "Library at closing time", "First snow on the ridge",
        "Night market dumplings", "Skate park, golden hour", "Repotting every plant I own",
        "Rain on the tram window", "Hackathon, hour 30", "Painting the hallway green",
    ]
    private static let endings = [
        "#nofilter", "(wait for it)", "part 2", "#ocean", "#coffee", "#weekend", "sound on",
        "#dailyvlog", "#slowliving", "#citylife",
    ]
    private static let handleStarts = [
        "sunny", "pixel", "quiet", "late", "loud", "tiny", "north", "paper", "neon", "slow",
    ]
    private static let handleEnds = [
        ".days", "_studio", "fox", ".films", "_kitchen", "walks", ".notes", "_and_co", "lab",
        ".loop",
    ]
}

/// SplitMix64: a tiny, well mixed generator, so a seed gives the same reel everywhere (Swift's
/// own random generators are not seedable).
struct SplitMix64 {
    private var state: UInt64

    init(seed: UInt64) { state = seed }

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }

    /// A number in [0, 1).
    mutating func unit() -> Double { Double(next() >> 11) / Double(1 << 53) }

    /// An integer in [0, n).
    mutating func below(_ n: Int) -> Int { Int(next() % UInt64(n)) }
}

// MARK: - Drawing one reel

/// One reel at a given play time: the gradient, the moving element, the big number. Captions,
/// the heart and the pause glyph are the feed's (`ReelFeedView`), drawn above this.
struct ReelCanvas: View {
    let reel: Reel
    /// Seconds the reel has played; the motion is a pure function of it, so a frozen time is a
    /// frozen frame (pause) and time 0 is the still poster the neighbours show.
    let time: Double
    /// Darkens the moving element while the user is choosing (see `ReelFeedView.dimmed`).
    var dimmed = false

    var body: some View {
        ZStack {
            LinearGradient(colors: [reel.top, reel.bottom], startPoint: .top, endPoint: .bottom)
            ReelMotionLayer(reel: reel, time: time)
                .opacity(dimmed ? 0.3 : 1)
            Text("\(reel.number)")
                .font(.system(size: 150, weight: .heavy, design: .rounded))
                .minimumScaleFactor(0.3)
                .lineLimit(1)
                .foregroundStyle(.white.opacity(0.92))
                .shadow(color: .black.opacity(0.45), radius: 18, y: 4)
                .padding(.horizontal, 12)
        }
        .clipped()
    }
}

/// The moving element alone: a few shapes placed from `time`, nothing else, so a redraw per
/// tick touches as little as possible.
struct ReelMotionLayer: View {
    let reel: Reel
    let time: Double

    var body: some View {
        GeometryReader { geo in
            let size = geo.size
            let phase = (time / reel.period).truncatingRemainder(dividingBy: 1)
            switch reel.motion {
            case .sweep:
                // The mock's bar: 6 % of the height, from just above the top to the bottom.
                let band = size.height * 0.06
                Rectangle()
                    .fill(.white.opacity(0.18))
                    .frame(width: size.width, height: band)
                    .offset(y: -band + phase * (size.height + band))
            case .orbit:
                let r = min(size.width, size.height) * 0.28
                let angle = phase * 2 * .pi
                Circle()
                    .fill(reel.accent.opacity(0.35))
                    .frame(width: r * 0.7, height: r * 0.7)
                    .blur(radius: 6)
                    .position(x: size.width / 2 + cos(angle) * r,
                              y: size.height / 2 + sin(angle) * r)
            case .bars:
                HStack(alignment: .bottom, spacing: size.width * 0.03) {
                    ForEach(0..<5, id: \.self) { i in
                        let wave = 0.5 + 0.5 * sin(2 * .pi * (phase + Double(i) * 0.17))
                        RoundedRectangle(cornerRadius: 4)
                            .fill(reel.accent.opacity(0.28))
                            .frame(height: size.height * (0.08 + 0.22 * wave))
                    }
                }
                .padding(.horizontal, size.width * 0.12)
                .frame(width: size.width, height: size.height * 0.92, alignment: .bottom)
            case .pulse:
                let d = min(size.width, size.height) * (0.3 + 0.9 * phase)
                Circle()
                    .strokeBorder(reel.accent.opacity(0.5 * (1 - phase)), lineWidth: 6)
                    .frame(width: d, height: d)
                    .position(x: size.width / 2, y: size.height / 2)
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

#Preview("Reels 1-4") {
    HStack(spacing: 2) {
        ForEach(0..<4, id: \.self) { i in
            ReelCanvas(reel: ReelContent.reel(i), time: 0.7)
        }
    }
    .frame(height: 360)
    .background(.black)
}
