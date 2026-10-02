"""Digital IDs: what certificate signing signs with. No Qt.

Two kinds (user's choice: both):
- A certificate in Windows' own store -- issued by the company (AD
  Certificate Services) or on a smart card. Listed and used through
  Windows (wincert.py); the private key never leaves Windows.
- A self-made ID, like Acrobat's "New digital ID": a self-signed
  certificate and its key in a password-protected .pfx file, made here.
  Anyone can make one, so recipients see "identity not verified" until
  they choose to trust it -- that's the honest answer, not a defect.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PFX = "pfx"
WINDOWS = "windows"

# Extended key usages that say "this certificate may sign documents".
_EKU_DOCUMENT_SIGNING = "1.3.6.1.4.1.311.10.3.12"   # Microsoft document signing
_EKU_ADOBE_AUTHENTIC = "1.2.840.113583.1.1.5"        # Adobe Authentic Documents Trust
ID_YEARS = 5


class DigitalIDError(Exception):
    pass


class WrongPassword(DigitalIDError):
    pass


@dataclass(frozen=True)
class DigitalID:
    kind: str                      # PFX or WINDOWS
    name: str                      # the signer, as the certificate names them
    issuer: str = ""
    expires: Optional[dt.datetime] = None
    path: Optional[Path] = None    # PFX
    thumbprint: str = ""           # WINDOWS: SHA-1 of the certificate, hex
    self_signed: bool = False

    def label(self) -> str:
        if self.kind == PFX:
            return f"{self.name} — {self.path.name}" if self.path else self.name
        by = "self-signed" if self.self_signed else f"issued by {self.issuer}"
        until = f", until {self.expires:%Y-%m-%d}" if self.expires else ""
        return f"{self.name} — Windows certificate, {by}{until}"


def common_name(cert) -> str:
    """The person's name from an asn1crypto certificate."""
    subject = cert.subject.native
    return (subject.get("common_name") or subject.get("email_address")
            or cert.subject.human_friendly)


def create_pfx_id(path: Path, name: str, password: str, email: str = "",
                  organization: str = "") -> DigitalID:
    """A new self-signed digital ID: RSA 3072, SHA-256, usable for document
    signing for ID_YEARS years, in a .pfx protected with `password`."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

    if not name.strip():
        raise DigitalIDError("A digital ID needs a name.")
    if not password:
        raise DigitalIDError("A digital ID needs a password.")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name.strip())]
    if organization.strip():
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization.strip()))
    if email.strip():
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email.strip()))
    subject = x509.Name(attrs)
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(subject).issuer_name(subject)
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(minutes=5))
               .not_valid_after(now + dt.timedelta(days=365 * ID_YEARS))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True,
                                            key_encipherment=False, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=False,
                                            crl_sign=False, encipher_only=False,
                                            decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([
                   ExtendedKeyUsageOID.EMAIL_PROTECTION,
                   ObjectIdentifier(_EKU_DOCUMENT_SIGNING),
                   ObjectIdentifier(_EKU_ADOBE_AUTHENTIC)]), critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                              critical=False))
    if email.strip():
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(email.strip())]), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    data = pkcs12.serialize_key_and_certificates(
        name.strip().encode("utf-8"), key, cert, None,
        serialization.BestAvailableEncryption(password.encode("utf-8")))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return DigitalID(PFX, name.strip(), name.strip(), cert.not_valid_after_utc, path,
                     self_signed=True)


def pfx_signer(path: Path, password: str):
    """A pyHanko signer for a .pfx file. Raises WrongPassword, or
    DigitalIDError for a file that isn't a usable ID."""
    from cryptography.hazmat.primitives.serialization import pkcs12

    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise DigitalIDError(f"Can't read {Path(path).name}: {exc}") from exc
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode("utf-8"))
    except ValueError as exc:
        # cryptography says "Invalid password or PKCS12 data" for both.
        raise WrongPassword(f"That password doesn't open {Path(path).name}, or it "
                            "isn't a digital ID file.") from exc
    if key is None or cert is None:
        raise DigitalIDError(f"{Path(path).name} has no private key, so it can't sign.")
    return _simple_signer(key, cert, extra or [])


def _simple_signer(key, cert, extra):
    from asn1crypto import keys as asn1_keys, x509 as asn1_x509
    from cryptography.hazmat.primitives import serialization
    from pyhanko.sign.signers import SimpleSigner
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    der = lambda c: c.public_bytes(serialization.Encoding.DER)  # noqa: E731
    key_info = asn1_keys.PrivateKeyInfo.load(key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    signing_cert = asn1_x509.Certificate.load(der(cert))
    registry = SimpleCertificateStore.from_certs(
        [signing_cert] + [asn1_x509.Certificate.load(der(c)) for c in extra])
    return SimpleSigner(signing_cert=signing_cert, signing_key=key_info, cert_registry=registry)


def describe_pfx(path: Path, password: str) -> DigitalID:
    """The DigitalID a .pfx holds (opening it needs its password)."""
    signer = pfx_signer(path, password)
    cert = signer.signing_cert
    return DigitalID(PFX, common_name(cert), cert.issuer.human_friendly,
                     cert["tbs_certificate"]["validity"]["not_after"].native, Path(path),
                     self_signed=cert.self_signed in ("yes", "maybe"))
