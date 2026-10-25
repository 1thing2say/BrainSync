import XCTest
@testable import BrainSyncKit

final class UIConnectionTests: XCTestCase {
    func testCloseCodes() {
        XCTAssertEqual(UIConnection.stopReason(closeCode: 4403, httpStatus: nil), .badKey)
        XCTAssertEqual(UIConnection.stopReason(closeCode: 0, httpStatus: 403), .badKey)
        XCTAssertEqual(UIConnection.stopReason(closeCode: 4000, httpStatus: nil), .replaced)
        XCTAssertEqual(UIConnection.stopReason(closeCode: 4000, httpStatus: 101), .replaced)
        // Everything else reconnects: server restart, network drop, tunnel hiccup.
        XCTAssertNil(UIConnection.stopReason(closeCode: 1000, httpStatus: nil))
        XCTAssertNil(UIConnection.stopReason(closeCode: 1001, httpStatus: nil))
        XCTAssertNil(UIConnection.stopReason(closeCode: 1006, httpStatus: nil))
        XCTAssertNil(UIConnection.stopReason(closeCode: 0, httpStatus: nil))
        XCTAssertNil(UIConnection.stopReason(closeCode: 0, httpStatus: 502))
    }
}
