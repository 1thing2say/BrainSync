// Simulator smoke runs (DEBUG builds only, and only with these launch arguments): drive the suite
// without taps so a script can check the whole loop against `brainsync.server --board fake`.
//
//   xcrun simctl launch booted org.calhacks.brainsync.partner \
//       -debugSuiteLink http://127.0.0.1:8765/ -debugSuiteAccept YES -debugSuiteGaze t1
//
// -debugSuiteLink pairs, -debugSuiteAccept accepts the flicker warning (as its button does),
// -debugSuiteApp opens an app (camera, reels, voice), -debugSuiteGaze points the fake board at a
// square once the template is trained (the server then picks it like a look).

#if DEBUG
import Foundation
import BrainSyncKit

extension SuiteModel {
    func applyDebugLaunchArguments() {
        let args = UserDefaults.standard   // launch arguments land in its argument domain
        if let link = args.string(forKey: "debugSuiteLink") { pair(text: link) }
        if let name = args.string(forKey: "debugSuiteApp"), let app = SuiteApp(rawValue: name) { open(app) }
        if args.bool(forKey: "debugSuiteAccept") { acceptWarning() }
        if let slot = args.string(forKey: "debugSuiteGaze") {
            Task { [weak self] in
                while let self, self.trained == nil || self.welcome?.fake != true {
                    try? await Task.sleep(nanoseconds: 200_000_000)
                }
                self?.tapSquare(slot)
            }
        }
    }
}
#endif
