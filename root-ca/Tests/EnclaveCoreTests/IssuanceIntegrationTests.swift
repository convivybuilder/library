import Foundation
import XCTest
@testable import EnclaveCore

/// End-to-end, root through leaf, entirely against
/// `ThrowawaySoftwareSigner` — never the real Secure Enclave, which this
/// machine does not have. Chain validity is confirmed by
/// `SecTrustHelper.chainValidates`, Security.framework's own X.509 engine —
/// a genuinely independent implementation from everything in `EnclaveCore`.
/// What these tests establish: the DER this tool assembles is a certificate
/// a real, independent X.509 implementation accepts, chains, and reads back
/// the shape platform/decisions/0066 requires. See the PR body for what
/// remains unverified without real hardware.
final class IssuanceIntegrationTests: XCTestCase {
    private func makeRoot() throws -> (signer: ThrowawaySoftwareSigner, der: Data, commonName: String) {
        let signer = try ThrowawaySoftwareSigner.generate()
        let notBefore = Date().addingTimeInterval(-60) // clears SecTrust's not-yet-valid check by a margin
        let der = try RootCertificate.generateSelfSigned(
            signer: signer, commonName: "Test Root",
            notBefore: notBefore, notAfter: notBefore.addingTimeInterval(3650 * 86400))
        return (signer, der, "Test Root")
    }

    func testRootCertificateIsSelfSignedCAWithCertAndCRLSignOnly() throws {
        let (_, rootDER, commonName) = try makeRoot()
        XCTAssertTrue(try SecTrustHelper.chainValidates(leafDER: rootDER, rootDER: rootDER),
                       "a root must validate against itself as its own trust anchor")
        XCTAssertEqual(try CertificateReader.subjectCommonName(certificateDER: rootDER), commonName)

        let outer = try DERReader.readTLV(rootDER, at: 0)
        let tbs = try DERReader.children(rootDER, in: outer.content)[0]
        let tbsFields = try DERReader.children(rootDER, in: tbs.content)
        let extensionsWrapper = tbsFields[7] // [3] EXPLICIT extensions
        let extensionsSeq = try DERReader.readTLV(rootDER, at: extensionsWrapper.content.lowerBound)
        let extensions = try DERReader.children(rootDER, in: extensionsSeq.content)
        var sawBasicConstraintsCA = false
        var sawKeyUsageCertAndCRLSignOnly = false
        for ext in extensions {
            let fields = try DERReader.children(rootDER, in: ext.content)
            let oid = Array(rootDER[fields[0].content])
            let extnValueField = fields.last!
            let value = Data(rootDER[extnValueField.content])
            if oid == OIDs.idCeBasicConstraints {
                XCTAssertEqual(value, DER.sequence([DER.boolean(true)]))
                sawBasicConstraintsCA = true
            } else if oid == OIDs.idCeKeyUsage {
                XCTAssertEqual(value, Data([0x03, 0x02, 0x01, 0x06])) // keyCertSign | cRLSign
                sawKeyUsageCertAndCRLSignOnly = true
            }
        }
        XCTAssertTrue(sawBasicConstraintsCA)
        XCTAssertTrue(sawKeyUsageCertAndCRLSignOnly)
    }

    func testClientLeafChainsAndCarriesTheRightShape() throws {
        let (rootSigner, rootDER, rootCN) = try makeRoot()
        let generated = try TestCSRBuilder.makeP256CSR(commonName: "macbook")
        let csr = try CertificateRequest.parse(pem: generated.pem)

        let notBefore = Date().addingTimeInterval(-60)
        let result = try LeafCertificate.issue(
            csr: csr, machine: "macbook", kind: .client, issuerCommonName: rootCN,
            notBefore: notBefore, notAfter: notBefore.addingTimeInterval(90 * 86400),
            signer: rootSigner)

        XCTAssertTrue(try SecTrustHelper.chainValidates(leafDER: result.certificateDER, rootDER: rootDER),
                       "Security.framework's own trust engine must accept the issued leaf against the root")

        XCTAssertEqual(try CertificateReader.subjectCommonName(certificateDER: result.certificateDER), "macbook")
        XCTAssertEqual(Fingerprint.certificate(der: result.certificateDER), result.certificateFingerprint)
        XCTAssertEqual(try PEM.decode(result.certificatePEM, label: "CERTIFICATE"), result.certificateDER)

        let shape = try decodeLeafExtensions(result.certificateDER)
        XCTAssertEqual(shape.uriNames, ["convivy://machine/macbook"], "exactly one URI SAN")
        XCTAssertTrue(shape.dnsNames.isEmpty)
        XCTAssertTrue(shape.ipNames.isEmpty)
        XCTAssertEqual(shape.ekuOIDs, [OIDs.idKpClientAuth])
        XCTAssertEqual(shape.basicConstraintsIsCA, false)

        // The public-key fingerprint is reproducible from the CSR's own
        // generated key — what `generate_csr.py` would have printed on the
        // requesting machine for the same key.
        let independentSPKI = X509.spki(rawECPoint: generated.rawECPoint)
        XCTAssertEqual(csr.spkiDER, independentSPKI, "the SPKI reused from the CSR must be byte-identical to one built fresh from the same raw point")
        XCTAssertEqual(result.publicKeyFingerprint, Fingerprint.publicKey(spkiDER: independentSPKI))
    }

    func testServerLeafCarriesLoopbackNamesAndServerAuthOnly() throws {
        let (rootSigner, _, rootCN) = try makeRoot()
        let generated = try TestCSRBuilder.makeP256CSR(commonName: "library-mini")
        let csr = try CertificateRequest.parse(pem: generated.pem)

        let notBefore = Date().addingTimeInterval(-60)
        let result = try LeafCertificate.issue(
            csr: csr, machine: "library-mini", kind: .server, issuerCommonName: rootCN,
            notBefore: notBefore, notAfter: notBefore.addingTimeInterval(90 * 86400),
            signer: rootSigner)

        let shape = try decodeLeafExtensions(result.certificateDER)
        XCTAssertEqual(shape.uriNames, ["convivy://machine/library-mini"], "exactly one URI SAN")
        XCTAssertEqual(shape.dnsNames, ["localhost"])
        XCTAssertEqual(shape.ipNames, [Data([127, 0, 0, 1])])
        XCTAssertEqual(shape.ekuOIDs, [OIDs.idKpServerAuth])
    }

    func testInvalidMachineNamesAreRejected() throws {
        let (rootSigner, _, rootCN) = try makeRoot()
        let generated = try TestCSRBuilder.makeP256CSR(commonName: "bad")
        let csr = try CertificateRequest.parse(pem: generated.pem)
        let notBefore = Date()

        for badName in ["", "Uppercase", "has spaces", "-leading-hyphen", String(repeating: "a", count: 65)] {
            XCTAssertThrowsError(try LeafCertificate.issue(
                csr: csr, machine: badName, kind: .client, issuerCommonName: rootCN,
                notBefore: notBefore, notAfter: notBefore.addingTimeInterval(90 * 86400),
                signer: rootSigner)) { error in
                guard case MachineNameError.invalid = error else {
                    return XCTFail("expected .invalid for \(badName.debugDescription), got \(error)")
                }
            }
        }
    }

    func testSigningLeafChainsAndCarriesTheRightShape() throws {
        let (rootSigner, rootDER, rootCN) = try makeRoot()
        let generated = try TestCSRBuilder.makeP256CSR(commonName: "ignored, never trusted")
        let csr = try CertificateRequest.parse(pem: generated.pem)

        let notBefore = Date().addingTimeInterval(-60)
        let result = try LeafCertificate.issue(
            csr: csr, machine: "macbook", kind: .signing, signingEmail: "Operator@Example.com",
            issuerCommonName: rootCN, notBefore: notBefore,
            notAfter: notBefore.addingTimeInterval(90 * 86400), signer: rootSigner)

        XCTAssertTrue(try SecTrustHelper.chainValidates(leafDER: result.certificateDER, rootDER: rootDER),
                       "Security.framework's own trust engine must accept the issued signing leaf against the root")
        XCTAssertEqual(try CertificateReader.subjectCommonName(certificateDER: result.certificateDER), "operator@example.com",
                        "the email is stored lower-cased")

        let shape = try decodeLeafExtensions(result.certificateDER)
        XCTAssertEqual(shape.rfc822Names, ["operator@example.com"], "exactly one rfc822Name")
        XCTAssertEqual(shape.uriNames, ["convivy://signer/macbook"], "exactly one signer URI, never convivy://machine/")
        XCTAssertTrue(shape.dnsNames.isEmpty)
        XCTAssertTrue(shape.ipNames.isEmpty)
        XCTAssertEqual(shape.ekuOIDs, [OIDs.idKpSigning], "no clientAuth or serverAuth")
        XCTAssertEqual(shape.ekuCritical, true, "the signing EKU is critical, unlike the machine/server EKU")
        XCTAssertEqual(shape.basicConstraintsIsCA, false)
    }

    func testInvalidSigningEmailsAreRejected() throws {
        let (rootSigner, _, rootCN) = try makeRoot()
        let generated = try TestCSRBuilder.makeP256CSR(commonName: "macbook")
        let csr = try CertificateRequest.parse(pem: generated.pem)
        let notBefore = Date()

        for badEmail in ["", "no-at-sign", "two@at@example.com", "has space@example.com",
                          "tab\tin@example.com", "control\u{0007}char@example.com"] {
            XCTAssertThrowsError(try LeafCertificate.issue(
                csr: csr, machine: "macbook", kind: .signing, signingEmail: badEmail,
                issuerCommonName: rootCN, notBefore: notBefore,
                notAfter: notBefore.addingTimeInterval(90 * 86400), signer: rootSigner)) { error in
                guard case SigningEmailError.invalid = error else {
                    return XCTFail("expected .invalid for \(badEmail.debugDescription), got \(error)")
                }
            }
        }

        XCTAssertThrowsError(try LeafCertificate.issue(
            csr: csr, machine: "macbook", kind: .signing, issuerCommonName: rootCN,
            notBefore: notBefore, notAfter: notBefore.addingTimeInterval(90 * 86400),
            signer: rootSigner)) { error in
            guard case SigningEmailError.invalid = error else {
                return XCTFail("expected .invalid when signingEmail is nil, got \(error)")
            }
        }
    }

    /// `rfc822Name` encodes as `IA5String` (RFC 5280 §4.2.1.6), ASCII only,
    /// so `validateSigningEmail` must refuse every scalar outside printable
    /// ASCII, not only the whitespace-and-C0 set a looser filter might catch:
    /// a C1 control and a bidi override both sit above 0x7F and would
    /// otherwise reach the SAN unchanged.
    func testValidateSigningEmailRejectsNonPrintableASCII() throws {
        let badEmails = [
            "c1 control (U+009B)": "person\u{009B}@example.com",
            "bidi override (U+202E)": "person\u{202E}@example.com",
            "zero-width space (U+200B)": "person\u{200B}@example.com",
            "non-ASCII letter": "jösé@example.com",
            "space": "person name@example.com",
            "tab": "person\tname@example.com",
            "embedded NUL": "person\u{0000}name@example.com",
        ]
        for (label, email) in badEmails {
            XCTAssertThrowsError(try LeafCertificate.validateSigningEmail(email), label) { error in
                guard case SigningEmailError.invalid = error else {
                    return XCTFail("expected .invalid for \(label) (\(email.debugDescription)), got \(error)")
                }
            }
        }
    }

    func testValidateSigningEmailAcceptsPlainAddressAndLowerCasesMixedCase() throws {
        XCTAssertEqual(try LeafCertificate.validateSigningEmail("operator@example.com"), "operator@example.com")
        XCTAssertEqual(try LeafCertificate.validateSigningEmail("Operator@Example.COM"), "operator@example.com")
    }

    func testInvalidMachineNameIsRejectedOnTheSigningKindToo() throws {
        let (rootSigner, _, rootCN) = try makeRoot()
        let generated = try TestCSRBuilder.makeP256CSR(commonName: "macbook")
        let csr = try CertificateRequest.parse(pem: generated.pem)
        let notBefore = Date()

        for badName in ["", "Uppercase", "has spaces"] {
            XCTAssertThrowsError(try LeafCertificate.issue(
                csr: csr, machine: badName, kind: .signing, signingEmail: "person@example.com",
                issuerCommonName: rootCN, notBefore: notBefore,
                notAfter: notBefore.addingTimeInterval(90 * 86400), signer: rootSigner)) { error in
                guard case MachineNameError.invalid = error else {
                    return XCTFail("expected .invalid for \(badName.debugDescription), got \(error)")
                }
            }
        }
    }

    /// A regression guard on `LeafCertificate.issue` itself: adding the
    /// `.signing` branch must leave the `.client`/`.server` branch producing
    /// byte-identical extensions to the extension-construction functions
    /// called directly — the same functions `ExtensionsTests` exercises in
    /// isolation, here compared against this file's actual issued output.
    /// `basicConstraints(isCA: false)`'s own bytes, hardcoded rather than
    /// called live: `X509Extension.subjectAltName`, `.extendedKeyUsage`, and
    /// `.keyUsage` are asserted against calling the function directly (this
    /// test's job is only to confirm `LeafCertificate.issue` writes exactly
    /// what the function produces), but `.basicConstraints` changed under
    /// this PR, and a live call on both sides of the comparison would make
    /// the regression it was meant to catch invisible. `30 09` (SEQUENCE, 9
    /// content bytes) wraps `06 03 55 1D 13` (OID 2.5.29.19) and
    /// `04 02 30 00` (OCTET STRING containing the empty SEQUENCE `cA` is
    /// omitted from).
    private static let expectedLeafBasicConstraints = Data([
        0x30, 0x09, 0x06, 0x03, 0x55, 0x1D, 0x13, 0x04, 0x02, 0x30, 0x00,
    ])

    func testMachineAndServerExtensionBytesAreUnchanged() throws {
        let (rootSigner, _, rootCN) = try makeRoot()
        let notBefore = Date().addingTimeInterval(-60)
        let notAfter = notBefore.addingTimeInterval(90 * 86400)

        let clientGenerated = try TestCSRBuilder.makeP256CSR(commonName: "macbook")
        let clientCSR = try CertificateRequest.parse(pem: clientGenerated.pem)
        let clientResult = try LeafCertificate.issue(
            csr: clientCSR, machine: "macbook", kind: .client, issuerCommonName: rootCN,
            notBefore: notBefore, notAfter: notAfter, signer: rootSigner)
        XCTAssertEqual(try rawExtensions(clientResult.certificateDER), [
            X509Extension.subjectAltName(machineURI: "convivy://machine/macbook", includeLoopback: false),
            X509Extension.extendedKeyUsage(clientAuth: true),
            Self.expectedLeafBasicConstraints,
            X509Extension.keyUsage(isCA: false),
        ])

        let serverGenerated = try TestCSRBuilder.makeP256CSR(commonName: "library-mini")
        let serverCSR = try CertificateRequest.parse(pem: serverGenerated.pem)
        let serverResult = try LeafCertificate.issue(
            csr: serverCSR, machine: "library-mini", kind: .server, issuerCommonName: rootCN,
            notBefore: notBefore, notAfter: notAfter, signer: rootSigner)
        XCTAssertEqual(try rawExtensions(serverResult.certificateDER), [
            X509Extension.subjectAltName(machineURI: "convivy://machine/library-mini", includeLoopback: true),
            X509Extension.extendedKeyUsage(clientAuth: false),
            Self.expectedLeafBasicConstraints,
            X509Extension.keyUsage(isCA: false),
        ])
    }

    // MARK: - helpers

    /// The full raw TLV bytes of each `TBSCertificate.extensions` entry, in
    /// order — for byte-exact comparison against `X509Extension`'s own
    /// output, as opposed to `decodeLeafExtensions`'s field-by-field read.
    private func rawExtensions(_ certDER: Data) throws -> [Data] {
        let outer = try DERReader.readTLV(certDER, at: 0)
        let tbs = try DERReader.children(certDER, in: outer.content)[0]
        let tbsFields = try DERReader.children(certDER, in: tbs.content)
        let extensionsWrapper = tbsFields[7]
        let extensionsSeq = try DERReader.readTLV(certDER, at: extensionsWrapper.content.lowerBound)
        let extensions = try DERReader.children(certDER, in: extensionsSeq.content)
        return extensions.map { Data(certDER[$0.whole]) }
    }

    private struct LeafShape {
        var uriNames: [String] = []
        var rfc822Names: [String] = []
        var dnsNames: [String] = []
        var ipNames: [Data] = []
        var ekuOIDs: [[UInt8]] = []
        var ekuCritical: Bool?
        var basicConstraintsIsCA: Bool?
    }

    /// Walks the leaf's `TBSCertificate.extensions` directly — the same
    /// low-level reading `ExtensionsTests` already exercises in isolation,
    /// applied here to this test's actual issued output rather than to a
    /// hand-built extension in isolation.
    private func decodeLeafExtensions(_ certDER: Data) throws -> LeafShape {
        let outer = try DERReader.readTLV(certDER, at: 0)
        let tbs = try DERReader.children(certDER, in: outer.content)[0]
        let tbsFields = try DERReader.children(certDER, in: tbs.content)
        let extensionsWrapper = tbsFields[7]
        let extensionsSeq = try DERReader.readTLV(certDER, at: extensionsWrapper.content.lowerBound)
        let extensions = try DERReader.children(certDER, in: extensionsSeq.content)

        var shape = LeafShape()
        for ext in extensions {
            let fields = try DERReader.children(certDER, in: ext.content)
            let oid = Array(certDER[fields[0].content])
            let extnValueField = fields.last!
            let value = Data(certDER[extnValueField.content])

            if oid == OIDs.idCeSubjectAltName {
                let namesOuter = try DERReader.readTLV(value, at: 0)
                for name in try DERReader.children(value, in: namesOuter.content) {
                    switch name.tag {
                    case DER.contextPrimitive(1): shape.rfc822Names.append(String(data: value[name.content], encoding: .utf8)!)
                    case DER.contextPrimitive(6): shape.uriNames.append(String(data: value[name.content], encoding: .utf8)!)
                    case DER.contextPrimitive(2): shape.dnsNames.append(String(data: value[name.content], encoding: .utf8)!)
                    case DER.contextPrimitive(7): shape.ipNames.append(Data(value[name.content]))
                    default: XCTFail("unexpected GeneralName tag \(name.tag)")
                    }
                }
            } else if oid == OIDs.idCeExtKeyUsage {
                shape.ekuCritical = fields.count == 3 && certDER[fields[1].content].first == 0xFF
                let purposesOuter = try DERReader.readTLV(value, at: 0)
                for purpose in try DERReader.children(value, in: purposesOuter.content) {
                    shape.ekuOIDs.append(Array(value[purpose.content]))
                }
            } else if oid == OIDs.idCeBasicConstraints {
                let seq = try DERReader.readTLV(value, at: 0)
                let children = try DERReader.children(value, in: seq.content)
                shape.basicConstraintsIsCA = children.first.map { value[$0.content].first == 0xFF } ?? false
            }
        }
        return shape
    }
}
