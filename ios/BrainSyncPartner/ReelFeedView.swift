// The Reels app's feed: one full-height reel at a time that snaps up or down like Instagram
// Reels, ported from the Chrome extension's mock (extension/test/reels_mock.html on
// cvep3/reels). It only draws: the current index, the liked set and the paused flag come in as
// plain values from ReelsCore, and the two c-VEP squares (Like-pause / Next, stacked on the
// right, never more than two) go in the right rail the caller passes in. Nothing here
// flickers; the motion is a few shapes redrawn at most 30 times a second, and only on the reel
// showing, so it never costs the squares' 60 Hz display-link frames.

import SwiftUI

/// Which end of the feed a bump shows.
enum ReelFeedEdge: Sendable {
    case top, end
}

struct ReelFeedView<RightRail: View>: View {
    /// The reel showing. Any integer: content wraps every `ReelContent.count` reels.
    let index: Int
    /// Indices of liked reels. A reel joining the set while it shows gets a heart burst.
    let liked: Set<Int>
    /// Freezes the reel's motion and shows a pause glyph.
    let paused: Bool
    /// Darkens the reel's motion while the user is choosing: moving content next to the c-VEP
    /// squares weakens the response to them.
    let dimmed: Bool
    /// Bump the feed down with "Top of feed" each time this grows (Up on the first reel).
    let topBumps: Int
    /// Bump the feed up with "End of feed" each time this grows (Down on the last reel).
    let endBumps: Int
    /// Width of the right rail, which stays black and holds `rightRail`.
    let railWidth: CGFloat
    let rightRail: RightRail

    init(index: Int, liked: Set<Int>, paused: Bool, dimmed: Bool,
         topBumps: Int = 0, endBumps: Int = 0, railWidth: CGFloat = 112,
         @ViewBuilder rightRail: () -> RightRail) {
        self.index = index
        self.liked = liked
        self.paused = paused
        self.dimmed = dimmed
        self.topBumps = topBumps
        self.endBumps = endBumps
        self.railWidth = railWidth
        self.rightRail = rightRail()
    }

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Seconds the current reel played before `resumedAt` (its frozen time while paused).
    @State private var playedBefore: Double = 0
    /// When the current reel last started playing, or nil while paused.
    @State private var resumedAt: Date?
    /// Counts heart bursts and bumps, so each change replays its animation once.
    @State private var hearts = 0
    @State private var bumps = 0
    @State private var bumpEdge: ReelFeedEdge = .top

    var body: some View {
        HStack(spacing: 0) {
            column
            rightRail
                .frame(width: railWidth)
                .frame(maxHeight: .infinity)
                .background(Color.black)
        }
        .background(Color.black)
        .onAppear { resumedAt = paused ? nil : Date() }
        .onChange(of: index) { _, _ in
            // A new reel starts from its beginning.
            playedBefore = 0
            resumedAt = paused ? nil : Date()
        }
        .onChange(of: paused) { _, isPaused in
            if isPaused {
                playedBefore = playTime(at: Date())
                resumedAt = nil
            } else {
                resumedAt = Date()
            }
        }
        .onChange(of: liked) { old, new in
            if new.contains(index) && !old.contains(index) { hearts += 1 }
        }
        .onChange(of: topBumps) { _, _ in bump(.top) }
        .onChange(of: endBumps) { _, _ in bump(.end) }
    }

    // MARK: - The reel column

    private var column: some View {
        GeometryReader { geo in
            let height = geo.size.height
            ZStack {
                ForEach(Array(index - 1...index + 1), id: \.self) { i in
                    page(i, size: geo.size)
                        .offset(y: CGFloat(i - index) * height)
                }
            }
            .animation(reduceMotion ? .easeInOut(duration: 0.2) : .snappy(duration: 0.32),
                       value: index)
            .keyframeAnimator(initialValue: 0.0, trigger: bumps) { content, shift in
                content.offset(y: bumpEdge == .top ? shift : -shift)
            } keyframes: { _ in
                SpringKeyframe(reduceMotion ? 0 : 44, duration: 0.14)
                SpringKeyframe(0, duration: 0.4, spring: .bouncy)
            }
        }
        .clipped()
        .overlay { heartBurst }
        .overlay { pauseGlyph }
        .overlay(alignment: bumpEdge == .top ? .top : .bottom) { edgePill }
        .overlay(alignment: .topLeading) {
            Text("Reels")
                .font(.title3.weight(.bold))
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.5), radius: 6)
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .accessibilityHidden(true)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    /// One reel with its caption. Only the reel showing plays; its neighbours are still posters,
    /// so at most one timeline ticks.
    @ViewBuilder
    private func page(_ i: Int, size: CGSize) -> some View {
        let reel = ReelContent.reel(i)
        let isCurrent = i == index
        ZStack(alignment: .bottomLeading) {
            if isCurrent {
                TimelineView(.animation(minimumInterval: 1.0 / 30,
                                        paused: paused || reduceMotion)) { context in
                    let t = reduceMotion ? 0 : playTime(at: context.date)
                    let looped = t.truncatingRemainder(dividingBy: reel.length)
                    ReelCanvas(reel: reel, time: looped, dimmed: dimmed)
                        .overlay(alignment: .bottom) {
                            ReelProgress(fraction: looped / reel.length)
                        }
                }
            } else {
                ReelCanvas(reel: reel, time: 0, dimmed: dimmed)
            }
            Color.black.opacity(dimmed ? 0.3 : 0)
                .allowsHitTesting(false)
            ReelCaption(reel: reel, liked: liked.contains(i))
        }
        .frame(width: size.width, height: size.height)
        .animation(.easeInOut(duration: 0.25), value: dimmed)
    }

    private func playTime(at now: Date) -> Double {
        playedBefore + (resumedAt.map { max(0, now.timeIntervalSince($0)) } ?? 0)
    }

    private func bump(_ edge: ReelFeedEdge) {
        bumpEdge = edge
        bumps += 1
    }

    // MARK: - Overlays

    private enum Burst: CaseIterable { case hidden, pop, hold }

    /// A big heart that pops in the middle of the reel when it is liked, then fades.
    private var heartBurst: some View {
        Image(systemName: "heart.fill")
            .font(.system(size: 110))
            .foregroundStyle(.white)
            .shadow(color: .black.opacity(0.35), radius: 14)
            .phaseAnimator(Burst.allCases, trigger: hearts) { content, phase in
                content
                    .scaleEffect(phase == .hidden ? 0.4 : (phase == .pop ? 1.15 : 1))
                    .opacity(phase == .hidden ? 0 : 0.95)
            } animation: { phase in
                switch phase {
                case .pop: return .spring(duration: 0.28, bounce: 0.5)
                case .hold: return .easeOut(duration: 0.5)
                case .hidden: return .easeIn(duration: 0.3)
                }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }

    /// While paused: a still pause glyph in the middle of the reel (no animation loop).
    @ViewBuilder
    private var pauseGlyph: some View {
        if paused {
            Image(systemName: "pause.fill")
                .font(.system(size: 40, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 92, height: 92)
                .background(.black.opacity(0.45), in: Circle())
                .transition(.opacity.combined(with: .scale(scale: 0.8)))
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    /// "Top of feed" / "End of feed" for a moment after a bump.
    private var edgePill: some View {
        Text(bumpEdge == .top ? "Top of feed" : "End of feed")
            .font(.footnote.weight(.semibold))
            .foregroundStyle(Palette.text)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(.black.opacity(0.7), in: Capsule())
            .overlay(Capsule().strokeBorder(Palette.lineStrong, lineWidth: 1))
            .padding(bumpEdge == .top ? .top : .bottom, 52)
            .phaseAnimator(Burst.allCases, trigger: bumps) { content, phase in
                content.opacity(phase == .hidden ? 0 : 1)
            } animation: { phase in
                switch phase {
                case .pop: return .easeOut(duration: 0.15)
                case .hold: return .linear(duration: 1.1)
                case .hidden: return .easeIn(duration: 0.3)
                }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }

    private var accessibilityText: String {
        let reel = ReelContent.reel(index)
        var parts = ["Reel \(reel.number) of \(ReelContent.count)", reel.caption, "by \(reel.handle)"]
        if liked.contains(index) { parts.append("liked") }
        if paused { parts.append("paused") }
        return parts.joined(separator: ", ")
    }
}

/// The handle, caption and like count at the bottom left of a reel.
private struct ReelCaption: View {
    let reel: Reel
    let liked: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(reel.handle)
                .font(.subheadline.weight(.semibold))
            Text(reel.caption)
                .font(.subheadline)
                .lineLimit(2)
            HStack(spacing: 5) {
                Image(systemName: liked ? "heart.fill" : "heart")
                    .foregroundStyle(liked ? Palette.danger : .white)
                Text(Self.count(reel.likes + (liked ? 1 : 0)))
                    .monospacedDigit()
            }
            .font(.footnote.weight(.medium))
            .padding(.top, 2)
        }
        .foregroundStyle(.white)
        .shadow(color: .black.opacity(0.6), radius: 4)
        .padding(.horizontal, 16)
        .padding(.bottom, 22)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(alignment: .bottom) {
            LinearGradient(colors: [.clear, .black.opacity(0.55)], startPoint: .top,
                           endPoint: .bottom)
                .frame(height: 150)
        }
    }

    /// 1234 -> "1,234", 12345 -> "12.3K", like Instagram's counts.
    static func count(_ n: Int) -> String {
        if n < 10_000 { return n.formatted() }
        return (Double(n) / 1000).formatted(.number.precision(.fractionLength(1))) + "K"
    }
}

/// The thin playback line along the bottom of the reel.
private struct ReelProgress: View {
    let fraction: Double

    var body: some View {
        GeometryReader { geo in
            Rectangle()
                .fill(.white.opacity(0.85))
                .frame(width: geo.size.width * min(max(fraction, 0), 1), height: 2)
        }
        .frame(height: 2)
        .background(.white.opacity(0.2))
        .accessibilityHidden(true)
    }
}

// MARK: - Previews

/// Stand-in for the integrator's two stacked squares (still, never flickering).
private struct PreviewRail: View {
    var body: some View {
        VStack(spacing: 28) {
            ForEach(["Like", "Next"], id: \.self) { label in
                VStack(spacing: 6) {
                    Rectangle().fill(.white).frame(width: 72, height: 72)
                    MonoLabel(label)
                }
            }
        }
    }
}

/// Buttons drive the feed the way ReelsCore would, so the snap, heart, pause and bumps can be
/// tried in the canvas.
private struct ReelFeedPreview: View {
    @State private var index = 0
    @State private var liked: Set<Int> = []
    @State private var paused = false
    @State private var dimmed = true
    @State private var topBumps = 0
    @State private var endBumps = 0

    var body: some View {
        VStack(spacing: 0) {
            ReelFeedView(index: index, liked: liked, paused: paused, dimmed: dimmed,
                         topBumps: topBumps, endBumps: endBumps) {
                PreviewRail()
            }
            HStack {
                Button("Up") {
                    if index == 0 { topBumps += 1 } else { index -= 1 }
                }
                Button("Like") { liked.insert(index) }
                Button(paused ? "Play" : "Pause") { paused.toggle() }
                Button("Down") {
                    if index == ReelContent.count - 1 { endBumps += 1 } else { index += 1 }
                }
                Toggle("Dim", isOn: $dimmed).fixedSize()
            }
            .buttonStyle(FilledButtonStyle())
            .padding(8)
        }
        .background(Palette.bg)
        .preferredColorScheme(.dark)
    }
}

#Preview("Feed, interactive") {
    ReelFeedPreview()
}

#Preview("Liked and paused") {
    ReelFeedView(index: 4, liked: [4], paused: true, dimmed: false) {
        PreviewRail()
    }
}
