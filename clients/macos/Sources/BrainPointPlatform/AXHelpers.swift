import AppKit
import ApplicationServices

// Small typed wrappers over the C Accessibility API. Every read goes through these so the rules
// about what we never read (the value of any text input) live in one place: see `AXRoles`.

enum AXRoles {
    /// Roles whose AXValue is user-typed text. Their value is never read, by any code path.
    static let textInput: Set<String> = [
        "AXTextField", "AXTextArea", "AXSearchField", "AXSecureTextField", "AXComboBox",
    ]
    /// Text inputs we offer as focusable items (secure fields are left out entirely: focusing
    /// one turns on secure input, which pauses BrainPoint anyway).
    static let focusableText: Set<String> = ["AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"]
    /// Controls that are pressable by their role.
    static let control: Set<String> = [
        "AXButton", "AXLink", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton",
        "AXMenuItem", "AXDisclosureTriangle", "AXIncrementor", "AXTab",
    ]
    /// Pressable only when they offer AXPress.
    static let pressOnly: Set<String> = ["AXCell", "AXRow"]
    /// Actions ElementRegistry knows how to use, in order of preference.
    static let usableActions = ["AXPress", "AXPick", "AXConfirm", "AXShowMenu", "AXOpen"]

    static func isSecure(role: String, subrole: String) -> Bool {
        role == "AXSecureTextField" || subrole == "AXSecureTextField"
    }
}

enum AX {
    static func attr(_ e: AXUIElement, _ name: String) -> CFTypeRef? {
        var v: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(e, name as CFString, &v)
        return err == .success ? v : nil
    }

    static func string(_ e: AXUIElement, _ name: String) -> String? {
        attr(e, name) as? String
    }

    static func element(_ e: AXUIElement, _ name: String) -> AXUIElement? {
        guard let v = attr(e, name), CFGetTypeID(v) == AXUIElementGetTypeID() else { return nil }
        return (v as! AXUIElement)
    }

    static func elements(_ e: AXUIElement, _ name: String) -> [AXUIElement] {
        guard let v = attr(e, name), let arr = v as? [AnyObject] else { return [] }
        return arr.compactMap { o in
            CFGetTypeID(o) == AXUIElementGetTypeID() ? (o as! AXUIElement) : nil
        }
    }

    static func bool(_ v: CFTypeRef?) -> Bool? {
        guard let v else { return nil }
        if CFGetTypeID(v) == CFBooleanGetTypeID() { return CFBooleanGetValue((v as! CFBoolean)) }
        if let n = v as? NSNumber { return n.boolValue }
        return nil
    }

    static func point(_ v: CFTypeRef?) -> CGPoint? {
        guard let v, CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
        let av = v as! AXValue
        var p = CGPoint.zero
        return AXValueGetType(av) == .cgPoint && AXValueGetValue(av, .cgPoint, &p) ? p : nil
    }

    static func size(_ v: CFTypeRef?) -> CGSize? {
        guard let v, CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
        let av = v as! AXValue
        var s = CGSize.zero
        return AXValueGetType(av) == .cgSize && AXValueGetValue(av, .cgSize, &s) ? s : nil
    }

    /// Global frame (top-left origin), or nil when position or size is missing.
    static func frame(_ e: AXUIElement) -> CGRect? {
        guard let p = point(attr(e, kAXPositionAttribute)), let s = size(attr(e, kAXSizeAttribute))
        else { return nil }
        return CGRect(origin: p, size: s)
    }

    static func actions(_ e: AXUIElement) -> [String] {
        var names: CFArray?
        guard AXUIElementCopyActionNames(e, &names) == .success, let arr = names as? [String] else {
            return []
        }
        return arr
    }

    static func pid(_ e: AXUIElement) -> Int32? {
        var p: pid_t = 0
        return AXUIElementGetPid(e, &p) == .success ? p : nil
    }

    /// Several attributes in one IPC round trip; missing ones come back nil.
    static func many(_ e: AXUIElement, _ names: [String]) -> [CFTypeRef?]? {
        var out: CFArray?
        let err = AXUIElementCopyMultipleAttributeValues(e, names as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &out)
        guard err == .success, let arr = out as? [AnyObject], arr.count == names.count else { return nil }
        return arr.map { o in
            // Missing attributes come back as an AXValue of type .axError.
            if CFGetTypeID(o) == AXValueGetTypeID(), AXValueGetType(o as! AXValue) == .axError { return nil }
            return o as CFTypeRef
        }
    }
}

/// The label rule shared by the walk and the stale check in ElementRegistry.press: title,
/// description, a fixed name for window buttons (close, minimize, zoom, full screen), help, then (non-text roles only) a string AXValue, then the text of a few
/// direct static-text / image children. Never reads the value of a text input or secure field.
enum AXLabel {
    static let maxChars = 120

    static func clean(_ s: String) -> String {
        let collapsed = s.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
        return collapsed.count > maxChars ? String(collapsed.prefix(maxChars)) : collapsed
    }

    static func make(_ e: AXUIElement, role: String, subrole: String,
                     title: String?, description: String?, help: String?) -> String {
        for s in [title, description] { if let s, !clean(s).isEmpty { return clean(s) } }
        let textual = AXRoles.textInput.contains(role) || AXRoles.isSecure(role: role, subrole: subrole)
        if textual {
            // Text inputs: what labels the field, never what is typed in it.
            if let t = AX.element(e, kAXTitleUIElementAttribute) {
                let tRole = AX.string(t, kAXRoleAttribute) ?? ""
                if !AXRoles.textInput.contains(tRole) {
                    for s in [AX.string(t, kAXTitleAttribute), AX.string(t, kAXValueAttribute)] {
                        if let s, !clean(s).isEmpty { return clean(s) }
                    }
                }
            }
            if let s = AX.string(e, kAXPlaceholderValueAttribute), !clean(s).isEmpty { return clean(s) }
            if let help, !clean(help).isEmpty { return clean(help) }
            return ""
        }
        if let name = subroleNames[subrole] { return name }
        if let help, !clean(help).isEmpty { return clean(help) }
        if let s = AX.string(e, kAXValueAttribute), !clean(s).isEmpty { return clean(s) }
        // Unlabelled container controls (WebKit links, table cells): the first child text.
        for c in AX.elements(e, kAXChildrenAttribute).prefix(4) {
            let r = AX.string(c, kAXRoleAttribute) ?? ""
            let s: String?
            switch r {
            case "AXStaticText": s = AX.string(c, kAXValueAttribute) ?? AX.string(c, kAXTitleAttribute)
            case "AXImage": s = AX.string(c, kAXDescriptionAttribute) ?? AX.string(c, kAXTitleAttribute)
            default: s = nil
            }
            if let s, !clean(s).isEmpty { return clean(s) }
        }
        return ""
    }

    /// Window buttons that often carry no title or description.
    static let subroleNames = [
        "AXCloseButton": "Close window", "AXMinimizeButton": "Minimize window",
        "AXZoomButton": "Zoom window", "AXFullScreenButton": "Full screen",
    ]
}
