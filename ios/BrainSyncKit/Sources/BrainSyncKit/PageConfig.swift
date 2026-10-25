import Foundation

/// What `GET /api/config` returns (app/server.py `page_config`). Every key has a default, so an
/// older or newer server still decodes.
public struct PageConfig: Decodable, Equatable {
    public var ttsRate: Double
    public var keyboardMode: Bool
    public var sttMode: String
    public var listenWindowS: Double
    public var sttGapS: Double
    public var sttMaxLineS: Double
    public var sttEchoS: Double
    public var cueS: Double
    public var maxTrialS: Double
    public var userName: String
    public var phoneUrl: String
    public var appLink: String
    /// "code" (c-VEP) or "freq" (f-VEP).
    public var paradigm: String
    /// How the user's screen is laid out: "cvep" (squares in a row, in slot order), "diamond"
    /// (f-VEP page mode) or "row" (list mode).
    public var layout: String
    /// Every slot of the user's screen, in order.
    public var slots: [String]

    enum CodingKeys: String, CodingKey {
        case ttsRate = "tts_rate"
        case keyboardMode = "keyboard_mode"
        case sttMode = "stt_mode"
        case listenWindowS = "listen_window_s"
        case sttGapS = "stt_gap_s"
        case sttMaxLineS = "stt_max_line_s"
        case sttEchoS = "stt_echo_s"
        case cueS = "cue_s"
        case maxTrialS = "max_trial_s"
        case userName = "user_name"
        case phoneUrl = "phone_url"
        case appLink = "app_link"
        case paradigm, layout, slots
    }

    public init(ttsRate: Double = 1.0, keyboardMode: Bool = false, sttMode: String = "always",
                listenWindowS: Double = 10.0, sttGapS: Double = 1.2, sttMaxLineS: Double = 15.0,
                sttEchoS: Double = 4.0, cueS: Double = 1.0, maxTrialS: Double = 10.0,
                userName: String = "", phoneUrl: String = "", appLink: String = "",
                paradigm: String = "freq", layout: String = "diamond",
                slots: [String] = ["s1", "s2", "s3", "s4"]) {
        self.ttsRate = ttsRate
        self.keyboardMode = keyboardMode
        self.sttMode = sttMode
        self.listenWindowS = listenWindowS
        self.sttGapS = sttGapS
        self.sttMaxLineS = sttMaxLineS
        self.sttEchoS = sttEchoS
        self.cueS = cueS
        self.maxTrialS = maxTrialS
        self.userName = userName
        self.phoneUrl = phoneUrl
        self.appLink = appLink
        self.paradigm = paradigm
        self.layout = layout
        self.slots = slots
    }

    public static let defaults = PageConfig()

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = PageConfig.defaults
        ttsRate = (try? c.decodeIfPresent(Double.self, forKey: .ttsRate)) ?? d.ttsRate
        keyboardMode = (try? c.decodeIfPresent(Bool.self, forKey: .keyboardMode)) ?? d.keyboardMode
        sttMode = (try? c.decodeIfPresent(String.self, forKey: .sttMode)) ?? d.sttMode
        listenWindowS = (try? c.decodeIfPresent(Double.self, forKey: .listenWindowS)) ?? d.listenWindowS
        sttGapS = (try? c.decodeIfPresent(Double.self, forKey: .sttGapS)) ?? d.sttGapS
        sttMaxLineS = (try? c.decodeIfPresent(Double.self, forKey: .sttMaxLineS)) ?? d.sttMaxLineS
        sttEchoS = (try? c.decodeIfPresent(Double.self, forKey: .sttEchoS)) ?? d.sttEchoS
        cueS = (try? c.decodeIfPresent(Double.self, forKey: .cueS)) ?? d.cueS
        maxTrialS = (try? c.decodeIfPresent(Double.self, forKey: .maxTrialS)) ?? d.maxTrialS
        userName = (try? c.decodeIfPresent(String.self, forKey: .userName)) ?? d.userName
        phoneUrl = (try? c.decodeIfPresent(String.self, forKey: .phoneUrl)) ?? d.phoneUrl
        appLink = (try? c.decodeIfPresent(String.self, forKey: .appLink)) ?? d.appLink
        paradigm = (try? c.decodeIfPresent(String.self, forKey: .paradigm)) ?? d.paradigm
        layout = (try? c.decodeIfPresent(String.self, forKey: .layout)) ?? d.layout
        slots = (try? c.decodeIfPresent([String].self, forKey: .slots)) ?? d.slots
    }

    public static func decode(_ data: Data) throws -> PageConfig {
        try JSONDecoder().decode(PageConfig.self, from: data)
    }
}
