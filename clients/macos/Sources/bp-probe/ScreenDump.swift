import AppKit
import BrainPointCore
import BrainPointPlatform

/// The screen-scope walk for `bp-probe axdump`: what a BrainPoint scan of this app would send
/// (window, open menus, scroll strips, menu bar, Dock on the window's screen). Walks twice
/// (the first warms the caches) and reports the second; prints one RESULT line and returns
/// the JSON for the dump file. Reads only: nothing is pressed, focused or activated.
@MainActor
func screenScopeDump(pid: Int32) -> [String: Any] {
    _ = ScreenWalker.walk(pid: pid)
    guard let w = ScreenWalker.walk(pid: pid) else {
        print("RESULT screen scope: no usable window")
        return ["error": "no usable window"]
    }
    let s = w.snapshot
    let hints = Hints.make(s)
    let counts = ItemKind.allCases.map { k in "\(k.rawValue) \(s.countsByKind[k] ?? 0)" }.joined(separator: ", ")
    let ms = ["window", "menus", "scroll", "dock"].compactMap { k in s.kindMs[k].map { "\(k) \($0)" } }
        .joined(separator: ", ")
    print("RESULT screen scope: items \(s.items.count) (\(counts)), hints \(hints.count), nodes \(w.nodes), " +
          "walk \(r1(s.walkMs)) ms (\(ms)), truncated \(s.truncated), screen \(rectJSON(s.screenFrame)) @\(w.screenScale)x")
    for k in [ItemKind.menuBar, .menu, .scroll, .dock] {
        let labels = s.items.filter { $0.kind == k }.map { $0.enabled ? $0.label : "(\($0.label))" }
        if !labels.isEmpty { print("  \(k.rawValue): " + labels.prefix(40).joined(separator: " | ")) }
    }
    for i in s.items where i.kind == .scroll {
        print("  scroll strip \(i.subrole): \(rectJSON(i.frame)), actions \(i.actions), enabled \(i.enabled)")
    }
    return [
        "screen": rectJSON(s.screenFrame), "scale": w.screenScale,
        "walk_ms": r1(s.walkMs), "kind_ms": s.kindMs, "truncated": s.truncated, "n_nodes": w.nodes,
        "counts": Dictionary(uniqueKeysWithValues: ItemKind.allCases.map { ($0.rawValue, s.countsByKind[$0] ?? 0) }),
        "items": s.items.map { i in
            ["id": i.id, "kind": i.kind.rawValue, "role": i.role, "subrole": i.subrole, "label": i.label,
             "frame": rectJSON(i.frame), "enabled": i.enabled, "actions": i.actions] as [String: Any]
        },
        "hints": hints,
    ]
}
