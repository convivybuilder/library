import Foundation
import XCTest
@testable import EnclaveCore

final class DERTests: XCTestCase {
    func testShortFormLength() {
        XCTAssertEqual(DER.length(0), Data([0x00]))
        XCTAssertEqual(DER.length(127), Data([0x7F]))
    }

    func testLongFormLength() {
        XCTAssertEqual(DER.length(128), Data([0x81, 0x80]))
        XCTAssertEqual(DER.length(256), Data([0x82, 0x01, 0x00]))
    }

    func testIntegerPadsWhenHighBitSet() {
        // 0xFF alone would read back as -1 in two's complement; DER requires
        // a leading 0x00 to keep it positive.
        let encoded = DER.integer(unsignedMagnitude: Data([0xFF]))
        XCTAssertEqual(encoded, Data([0x02, 0x02, 0x00, 0xFF]))
    }

    func testIntegerStripsRedundantLeadingZero() {
        let encoded = DER.integer(unsignedMagnitude: Data([0x00, 0x7F]))
        XCTAssertEqual(encoded, Data([0x02, 0x01, 0x7F]))
    }

    func testSmallIntegerRoundTrips() throws {
        let encoded = DER.integer(2)
        let node = try DERReader.readTLV(encoded, at: 0)
        XCTAssertEqual(node.tag, DER.tagInteger)
        XCTAssertEqual(Array(encoded[node.content]), [0x02])
    }

    func testNamedBitStringTrimsTrailingZeroBits() {
        // bits 5 (keyCertSign) and 6 (cRLSign): byte 0000 0110 = 0x06, one
        // unused trailing bit.
        let bs = DER.namedBitString([5, 6])
        XCTAssertEqual(bs, Data([0x03, 0x02, 0x01, 0x06]))
    }

    func testNamedBitStringSingleLeadingBit() {
        // bit 0 (digitalSignature): byte 1000 0000 = 0x80, seven unused bits.
        let bs = DER.namedBitString([0])
        XCTAssertEqual(bs, Data([0x03, 0x02, 0x07, 0x80]))
    }

    func testSequenceRoundTripsThroughReader() throws {
        let seq = DER.sequence([DER.integer(1), DER.integer(2)])
        let outer = try DERReader.readTLV(seq, at: 0)
        XCTAssertEqual(outer.tag, DER.tagSequence)
        let children = try DERReader.children(seq, in: outer.content)
        XCTAssertEqual(children.count, 2)
        XCTAssertEqual(children[0].tag, DER.tagInteger)
        XCTAssertEqual(children[1].tag, DER.tagInteger)
    }
}

final class DERReaderTests: XCTestCase {
    func testTruncatedInputThrows() {
        XCTAssertThrowsError(try DERReader.readTLV(Data([0x30]), at: 0)) { error in
            guard case DERError.truncated = error else {
                return XCTFail("expected .truncated, got \(error)")
            }
        }
    }

    func testLongFormLengthParses() throws {
        var data = Data([0x04, 0x81, 0x02])
        data.append(Data([0xAA, 0xBB]))
        let node = try DERReader.readTLV(data, at: 0)
        XCTAssertEqual(node.tag, DER.tagOctetString)
        XCTAssertEqual(Array(data[node.content]), [0xAA, 0xBB])
        XCTAssertEqual(node.next, data.count)
    }

    func testChildrenThatOverrunParentThrows() throws {
        // A SEQUENCE claiming 3 content bytes but whose first child claims 5.
        // readTLV itself succeeds on the outer TLV (it trusts the declared
        // length); walking the too-short content as children is what fails.
        let malformed = Data([0x30, 0x03, 0x04, 0x05, 0xAA])
        let outer = try DERReader.readTLV(malformed, at: 0)
        XCTAssertThrowsError(try DERReader.children(malformed, in: outer.content))
    }

    func testExpectMismatchedTagThrows() throws {
        let node = try DERReader.readTLV(DER.integer(1), at: 0)
        XCTAssertThrowsError(try DERReader.expect(node, tag: DER.tagSequence)) { error in
            guard case DERError.unexpectedTag = error else {
                return XCTFail("expected .unexpectedTag, got \(error)")
            }
        }
    }
}

