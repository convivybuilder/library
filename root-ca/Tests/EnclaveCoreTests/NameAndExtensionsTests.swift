import Foundation
import XCTest
@testable import EnclaveCore

final class NameTests: XCTestCase {
    func testCommonNameRoundTrips() {
        let der = X509Name.commonNameOnly("macbook")
        XCTAssertEqual(X509Name.readCommonName(der), "macbook")
    }

    func testReadCommonNameOnUnrelatedDataReturnsNil() {
        XCTAssertNil(X509Name.readCommonName(Data([0x01, 0x02])))
    }
}

final class ExtensionsTests: XCTestCase {
    /// Manually walks a built `subjectAltName` extension back apart —
    /// exercising the same GeneralName tags `machine_identity.uri_names`
    /// reads on the Python side, from the other direction.
    func testSubjectAltNameCarriesExactlyOneURI() throws {
        let ext = X509Extension.subjectAltName(machineURI: "convivy://machine/macbook", includeLoopback: false)
        let (oid, value) = try decodeExtension(ext)
        XCTAssertEqual(oid, OIDs.idCeSubjectAltName)

        let generalNames = try DERReader.readTLV(value, at: 0)
        let names = try DERReader.children(value, in: generalNames.content)
        XCTAssertEqual(names.count, 1)
        XCTAssertEqual(names[0].tag, DER.contextPrimitive(6)) // URI
        XCTAssertEqual(String(data: value[names[0].content], encoding: .utf8), "convivy://machine/macbook")
    }

    func testSubjectAltNameWithLoopbackAddsExactlyDNSAndIP() throws {
        let ext = X509Extension.subjectAltName(machineURI: "convivy://machine/library-mini", includeLoopback: true)
        let (_, value) = try decodeExtension(ext)
        let generalNames = try DERReader.readTLV(value, at: 0)
        let names = try DERReader.children(value, in: generalNames.content)
        XCTAssertEqual(names.count, 3)
        XCTAssertEqual(names[0].tag, DER.contextPrimitive(6)) // URI
        XCTAssertEqual(names[1].tag, DER.contextPrimitive(2)) // DNS
        XCTAssertEqual(String(data: value[names[1].content], encoding: .utf8), "localhost")
        XCTAssertEqual(names[2].tag, DER.contextPrimitive(7)) // IP
        XCTAssertEqual(Array(value[names[2].content]), [127, 0, 0, 1])
    }

    func testExtendedKeyUsageClientVsServerAreMutuallyExclusive() throws {
        let client = try decodeExtension(X509Extension.extendedKeyUsage(clientAuth: true))
        XCTAssertEqual(client.oid, OIDs.idCeExtKeyUsage)
        let clientPurposes = try DERReader.readTLV(client.value, at: 0)
        let clientOIDs = try DERReader.children(client.value, in: clientPurposes.content)
        XCTAssertEqual(clientOIDs.count, 1)
        XCTAssertEqual(Array(client.value[clientOIDs[0].content]), OIDs.idKpClientAuth)
        XCTAssertNotEqual(Array(client.value[clientOIDs[0].content]), OIDs.idKpServerAuth)

        let server = try decodeExtension(X509Extension.extendedKeyUsage(clientAuth: false))
        let serverPurposes = try DERReader.readTLV(server.value, at: 0)
        let serverOIDs = try DERReader.children(server.value, in: serverPurposes.content)
        XCTAssertEqual(Array(server.value[serverOIDs[0].content]), OIDs.idKpServerAuth)
    }

    func testBasicConstraintsCAIsCriticalLeafIsNot() throws {
        let ca = X509Extension.basicConstraints(isCA: true)
        let caOuter = try DERReader.readTLV(ca, at: 0)
        let caFields = try DERReader.children(ca, in: caOuter.content)
        XCTAssertEqual(caFields.count, 3, "expected extnID, critical=TRUE, extnValue")
        XCTAssertEqual(caFields[1].tag, DER.tagBoolean)

        let leaf = X509Extension.basicConstraints(isCA: false)
        let leafOuter = try DERReader.readTLV(leaf, at: 0)
        let leafFields = try DERReader.children(leaf, in: leafOuter.content)
        XCTAssertEqual(leafFields.count, 2, "critical omitted (DEFAULT FALSE)")
    }

    /// X.690 (11.5): a DER encoder omits a field carrying its DEFAULT value,
    /// and `cA`'s default is `FALSE` — so a leaf's `BasicConstraints` is the
    /// empty `SEQUENCE`, `30 00`, never `SEQUENCE { cA: FALSE }`. Python's
    /// `cryptography` refuses to parse the explicit-`FALSE` encoding this used
    /// to write.
    func testLeafBasicConstraintsValueIsExactlyEmptySequence() throws {
        let ext = X509Extension.basicConstraints(isCA: false)
        let (oid, value) = try decodeExtension(ext)
        XCTAssertEqual(oid, OIDs.idCeBasicConstraints)
        XCTAssertEqual(value, Data([0x30, 0x00]))
    }

    func testKeyUsageRootHasCertSignAndCRLSignOnly() throws {
        let (_, value) = try decodeExtension(X509Extension.keyUsage(isCA: true))
        // 0000 0110 = keyCertSign (bit 5) | cRLSign (bit 6), one unused bit.
        XCTAssertEqual(value, Data([0x03, 0x02, 0x01, 0x06]))
    }

    func testKeyUsageLeafHasDigitalSignatureOnly() throws {
        let (_, value) = try decodeExtension(X509Extension.keyUsage(isCA: false))
        XCTAssertEqual(value, Data([0x03, 0x02, 0x07, 0x80]))
    }

    func testSigningOIDBytesMatchTheContractExactly() {
        // platform/contracts/library-protocol §2.2: DER content bytes for
        // 2.25.332679538808142797592760871621889796493 (UUID
        // fa47bfa2-1f8a-4d3d-8e88-d3cab3caa98d).
        XCTAssertEqual(OIDs.idKpSigning, [
            0x69, 0x83, 0xf4, 0xc7, 0xdf, 0xe8, 0xc3, 0xf8, 0xd2, 0xb4,
            0xfb, 0x8e, 0xc4, 0xb4, 0xf9, 0xab, 0x9e, 0xaa, 0xd3, 0x0d,
        ])
    }

    func testSignerSubjectAltNameCarriesExactlyRFC822AndURI() throws {
        let ext = X509Extension.signerSubjectAltName(
            email: "operator@example.com", signerURI: "convivy://signer/macbook")
        let (oid, value) = try decodeExtension(ext)
        XCTAssertEqual(oid, OIDs.idCeSubjectAltName)

        let generalNames = try DERReader.readTLV(value, at: 0)
        let names = try DERReader.children(value, in: generalNames.content)
        XCTAssertEqual(names.count, 2)
        XCTAssertEqual(names[0].tag, DER.contextPrimitive(1)) // rfc822Name
        XCTAssertEqual(String(data: value[names[0].content], encoding: .utf8), "operator@example.com")
        XCTAssertEqual(names[1].tag, DER.contextPrimitive(6)) // uniformResourceIdentifier
        XCTAssertEqual(String(data: value[names[1].content], encoding: .utf8), "convivy://signer/macbook")
    }

    func testExtendedKeyUsageSigningIsCriticalWithExactlyOnePurpose() throws {
        let ext = X509Extension.extendedKeyUsageSigning()
        let outer = try DERReader.readTLV(ext, at: 0)
        let fields = try DERReader.children(ext, in: outer.content)
        XCTAssertEqual(fields.count, 3, "expected extnID, critical=TRUE, extnValue")
        XCTAssertEqual(fields[1].tag, DER.tagBoolean)
        XCTAssertEqual(Array(ext[fields[1].content]), [0xFF])

        let value = Data(ext[fields.last!.content])
        let purposesOuter = try DERReader.readTLV(value, at: 0)
        let purposes = try DERReader.children(value, in: purposesOuter.content)
        XCTAssertEqual(purposes.count, 1)
        XCTAssertEqual(Array(value[purposes[0].content]), OIDs.idKpSigning)
        XCTAssertNotEqual(Array(value[purposes[0].content]), OIDs.idKpClientAuth)
        XCTAssertNotEqual(Array(value[purposes[0].content]), OIDs.idKpServerAuth)
    }

    private func decodeExtension(_ ext: Data) throws -> (oid: [UInt8], value: Data) {
        let outer = try DERReader.readTLV(ext, at: 0)
        let fields = try DERReader.children(ext, in: outer.content)
        let oidField = fields[0]
        let extnValueField = fields.last! // the OCTET STRING wrapping the extension's actual value
        return (Array(ext[oidField.content]), Data(ext[extnValueField.content]))
    }
}
