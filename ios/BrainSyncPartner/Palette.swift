// The app's look, shared with the web page: Vercel dark (near-black, 1px lines, colour only for
// status) with Docker-docs callouts. Nothing here animates or blinks (PLAN rule 10).

import SwiftUI

/// The partner page's colours (ui/style.css :root): the same hex values, so both clients match.
enum Palette {
    static let bg = Color(hex: 0x000000)
    /// Cards and side panels.
    static let panel = Color(hex: 0x0a0a0a)
    /// Boxes, inputs, buttons.
    static let surface = Color(hex: 0x111111)
    /// A flickering target on the user's screen, hover.
    static let surfaceHi = Color(hex: 0x1a1a1a)
    /// 1px borders.
    static let line = Color(hex: 0x242424)
    /// Borders on focus or emphasis.
    static let lineStrong = Color(hex: 0x333333)
    static let text = Color(hex: 0xededed)
    static let muted = Color(hex: 0xa1a1a1)
    /// Replies, Say it, the mic listening, all fine.
    static let say = Color(hex: 0x62c073)
    /// More options, Back.
    static let next = Color(hex: 0x52a8ff)
    /// "Look now", the selection timer, the focus ring.
    static let look = Color(hex: 0x52a8ff)
    static let warn = Color(hex: 0xf5a524)
    /// Stop (white bold text on it).
    static let danger = Color(hex: 0xe5484d)
    /// The picked target.
    static let pick = Color(hex: 0xffd34d)

    /// Buttons and inputs.
    static let radius: CGFloat = 6
    /// Cards.
    static let cardRadius: CGFloat = 12
}

fileprivate extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xff) / 255,
                  green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255)
    }
}

// MARK: - Shared pieces

/// A small label in monospace capitals with a little letter spacing (the web's .label).
struct MonoLabel: View {
    let text: String
    var color: Color = Palette.muted

    init(_ text: String, color: Color = Palette.muted) {
        self.text = text
        self.color = color
    }

    var body: some View {
        Text(text.uppercased())
            .font(.caption2.monospaced().weight(.medium))
            .tracking(0.6)
            .foregroundStyle(color)
    }
}

/// A heading with the tight letter spacing of the web's headings.
struct Heading: View {
    let text: String
    var font: Font = .title.weight(.bold)

    init(_ text: String, font: Font = .title.weight(.bold)) {
        self.text = text
        self.font = font
    }

    var body: some View {
        Text(text)
            .font(font)
            .tracking(-0.5)
            .foregroundStyle(Palette.text)
            .accessibilityAddTraits(.isHeader)
    }
}

/// A Docker-docs style callout: a tinted box with a coloured left edge, an icon and a small title.
struct Callout<Content: View>: View {
    let title: String
    let icon: String
    let tint: Color
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.footnote)
                    .foregroundStyle(tint)
                MonoLabel(title, color: tint)
            }
            content
        }
        .padding(14)
        .padding(.leading, 3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.08))
        .overlay(alignment: .leading) {
            Rectangle().fill(tint).frame(width: 3)
        }
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(tint.opacity(0.3), lineWidth: 1))
    }
}

extension View {
    /// A card: the panel colour, a 1px line, rounded 12.
    func card(padding: CGFloat = 14) -> some View {
        self.padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: Palette.cardRadius))
            .overlay(RoundedRectangle(cornerRadius: Palette.cardRadius)
                .strokeBorder(Palette.line, lineWidth: 1))
    }
}

/// Buttons: primary = light fill with dark text, secondary = surface with a 1px line, danger =
/// Stop's red with white bold text. A press only dims the fill a little.
struct FilledButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, danger }

    let kind: Kind
    let large: Bool

    init(_ kind: Kind = .secondary, large: Bool = false) {
        self.kind = kind
        self.large = large
    }

    func makeBody(configuration: Configuration) -> some View {
        FilledButton(configuration: configuration, kind: kind, large: large)
    }
}

/// A view of its own, so it can read isEnabled from the environment.
private struct FilledButton: View {
    let configuration: ButtonStyleConfiguration
    let kind: FilledButtonStyle.Kind
    let large: Bool
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Palette.radius)
        configuration.label
            .font(font)
            .multilineTextAlignment(.center)
            .padding(.horizontal, large ? 18 : 14)
            .padding(.vertical, large ? 14 : 9)
            .frame(minHeight: large ? 54 : 40)
            .foregroundStyle(foreground)
            .background(fill.opacity(configuration.isPressed ? 0.8 : 1), in: shape)
            .overlay(shape.strokeBorder(border, lineWidth: 1))
            .contentShape(shape)
            .opacity(isEnabled ? 1 : 0.45)
    }

    private var font: Font {
        switch kind {
        case .danger: return large ? .title3.weight(.bold) : .headline.weight(.bold)
        case .primary, .secondary: return large ? .headline : .subheadline.weight(.medium)
        }
    }

    private var fill: Color {
        switch kind {
        case .primary: return Palette.text
        case .secondary: return Palette.surface
        case .danger: return Palette.danger
        }
    }

    private var foreground: Color {
        switch kind {
        case .primary: return Palette.panel  // #0a0a0a on the light fill
        case .secondary: return Palette.text
        case .danger: return .white
        }
    }

    private var border: Color {
        switch kind {
        case .primary: return Palette.text
        case .secondary: return Palette.line
        case .danger: return Palette.danger
        }
    }
}
