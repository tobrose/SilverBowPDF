"""Certificates in Windows' own store: list them, sign with them, trust them.
Windows only, ctypes only, no Qt.

Signing goes through Windows (CNG: NCryptSignHash) with the key where it
lives -- a smart card, the TPM, or the user's software key store. The key
is never exported; a smart card's PIN prompt is Windows' own. pyHanko does
the PDF and CMS work around it (WindowsSigner).

Trust, for checking signatures, is Windows' too: the Trusted Root store,
which on a managed machine is the company's -- so a certificate the
company issued checks out as trusted, as it does in Outlook or Edge.
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

from .digitalid import WINDOWS, DigitalID, DigitalIDError, common_name

_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True) if sys.platform == "win32" else None
_ncrypt = ctypes.WinDLL("ncrypt") if sys.platform == "win32" else None

_ENCODING = 0x00000001 | 0x00010000          # X509_ASN_ENCODING | PKCS_7_ASN_ENCODING
_CERT_KEY_PROV_INFO_PROP_ID = 2
_CERT_KEY_CONTEXT_PROP_ID = 5
_CERT_NCRYPT_KEY_HANDLE_PROP_ID = 78
_CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF
_CRYPT_USER_KEYSET = 0x00001000
_CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG = 0x00040000
_CRYPT_ACQUIRE_WINDOW_HANDLE_FLAG = 0x00000080
_CRYPT_ACQUIRE_COMPARE_KEY_FLAG = 0x00000004
_BCRYPT_PAD_PKCS1 = 0x00000002
_PKCS12_NO_PERSIST_KEY = 0x00008000
_PKCS12_ALWAYS_CNG_KSP = 0x00000200
_CERT_STORE_ADD_ALWAYS = 4
_HASH_IDS = {"sha256": "SHA256", "sha384": "SHA384", "sha512": "SHA512"}


class _CERT_CONTEXT(ctypes.Structure):
    _fields_ = [("dwCertEncodingType", wintypes.DWORD),
                ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbCertEncoded", wintypes.DWORD),
                ("pCertInfo", ctypes.c_void_p),
                ("hCertStore", ctypes.c_void_p)]


_PCCERT = ctypes.POINTER(_CERT_CONTEXT)


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _PKCS1_PADDING(ctypes.Structure):
    _fields_ = [("pszAlgId", wintypes.LPCWSTR)]


class _USAGE(ctypes.Structure):
    _fields_ = [("cUsageIdentifier", wintypes.DWORD), ("rgpszUsageIdentifier", ctypes.c_void_p)]


class _KEY_CONTEXT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hKey", ctypes.c_size_t),
                ("dwKeySpec", wintypes.DWORD)]


class _CERT_USAGE_MATCH(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("Usage", _USAGE)]


class _CHAIN_PARA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("RequestedUsage", _CERT_USAGE_MATCH)]


class _TRUST_STATUS(ctypes.Structure):
    _fields_ = [("dwErrorStatus", wintypes.DWORD), ("dwInfoStatus", wintypes.DWORD)]


class _CHAIN_ELEMENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("pCertContext", _PCCERT),
                ("TrustStatus", _TRUST_STATUS)]  # (further fields unused)


class _SIMPLE_CHAIN(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("TrustStatus", _TRUST_STATUS),
                ("cElement", wintypes.DWORD),
                ("rgpElement", ctypes.POINTER(ctypes.POINTER(_CHAIN_ELEMENT)))]


class _CHAIN_CONTEXT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("TrustStatus", _TRUST_STATUS),
                ("cChain", wintypes.DWORD),
                ("rgpChain", ctypes.POINTER(ctypes.POINTER(_SIMPLE_CHAIN)))]


if _crypt32 is not None:
    _crypt32.CertOpenSystemStoreW.restype = ctypes.c_void_p
    _crypt32.CertOpenSystemStoreW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _crypt32.CertEnumCertificatesInStore.restype = _PCCERT
    _crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, _PCCERT]
    _crypt32.CertDuplicateCertificateContext.restype = _PCCERT
    _crypt32.CertDuplicateCertificateContext.argtypes = [_PCCERT]
    _crypt32.CertFreeCertificateContext.argtypes = [_PCCERT]
    _crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _crypt32.CertGetCertificateContextProperty.argtypes = [
        _PCCERT, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    _crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
        _PCCERT, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL)]
    _crypt32.PFXImportCertStore.restype = ctypes.c_void_p
    _crypt32.PFXImportCertStore.argtypes = [ctypes.POINTER(_BLOB), wintypes.LPCWSTR, wintypes.DWORD]
    _crypt32.CertGetCertificateChain.argtypes = [
        ctypes.c_void_p, _PCCERT, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_CHAIN_PARA),
        wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_CHAIN_CONTEXT))]
    _crypt32.CertFreeCertificateChain.argtypes = [ctypes.POINTER(_CHAIN_CONTEXT)]
    _ncrypt.NCryptSignHash.restype = ctypes.c_long
    _ncrypt.NCryptSignHash.argtypes = [
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_char_p, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    _ncrypt.NCryptFreeObject.argtypes = [ctypes.c_size_t]
    _ncrypt.NCryptDeleteKey.restype = ctypes.c_long
    _ncrypt.NCryptDeleteKey.argtypes = [ctypes.c_size_t, wintypes.DWORD]


def _der(ctx: _PCCERT) -> bytes:
    c = ctx.contents
    return ctypes.string_at(c.pbCertEncoded, c.cbCertEncoded)


def _has_private_key(ctx: _PCCERT) -> bool:
    size = wintypes.DWORD(0)
    for prop in (_CERT_KEY_PROV_INFO_PROP_ID, _CERT_NCRYPT_KEY_HANDLE_PROP_ID):
        if _crypt32.CertGetCertificateContextProperty(ctx, prop, None, ctypes.byref(size)):
            return True
    return False


def _store_certs(store) -> Iterator[_PCCERT]:
    ctx = _crypt32.CertEnumCertificatesInStore(store, None)
    while ctx:
        yield ctx
        ctx = _crypt32.CertEnumCertificatesInStore(store, ctx)  # frees the previous one


def _open(name: str):
    store = _crypt32.CertOpenSystemStoreW(None, name)
    if not store:
        raise DigitalIDError(f"Windows' {name} certificate store can't be opened "
                             f"(error {ctypes.get_last_error()}).")
    return store


def _can_sign(cert) -> bool:
    """Not expired, and its key usage (if it says) includes signing."""
    import datetime as dt
    validity = cert["tbs_certificate"]["validity"]
    now = dt.datetime.now(dt.timezone.utc)
    if not validity["not_before"].native <= now <= validity["not_after"].native:
        return False
    usage = cert.key_usage_value
    return usage is None or bool({"digital_signature", "non_repudiation"} & usage.native)


def list_ids() -> List[DigitalID]:
    """The user's Windows certificates that can sign: with a private key,
    in date, allowed to sign. Smart-card certificates are here when the
    card is (Windows lists them once the card has been seen)."""
    if _crypt32 is None:
        return []
    from asn1crypto import x509
    found = []
    store = _open("MY")
    try:
        for ctx in _store_certs(store):
            if not _has_private_key(ctx):
                continue
            der = _der(ctx)
            try:
                cert = x509.Certificate.load(der)
                if not _can_sign(cert):
                    continue
            except Exception:
                continue  # a certificate asn1crypto can't read isn't one to sign with
            found.append(DigitalID(
                WINDOWS, common_name(cert), common_name_or(cert.issuer),
                cert["tbs_certificate"]["validity"]["not_after"].native,
                thumbprint=hashlib.sha1(der).hexdigest(),
                self_signed=cert.self_signed in ("yes", "maybe")))
    finally:
        _crypt32.CertCloseStore(store, 0)
    return sorted(found, key=lambda d: (d.name.lower(), d.expires or 0))


def common_name_or(name) -> str:
    native = name.native
    return native.get("common_name") or native.get("organization_name") or name.human_friendly


def trust_certificates() -> Tuple[list, list]:
    """(trust roots, other certificates) from Windows: Trusted Root
    Certification Authorities, and Intermediate CAs plus Trusted People.
    A certificate asn1crypto can't read is skipped, not fatal."""
    if _crypt32 is None:
        return [], []
    from asn1crypto import x509

    def load(name):
        out = []
        store = _open(name)
        try:
            for ctx in _store_certs(store):
                try:
                    out.append(x509.Certificate.load(_der(ctx)))
                except Exception:
                    pass
        finally:
            _crypt32.CertCloseStore(store, 0)
        return out
    # Trusted People: certificates a user (or IT) chose to trust directly --
    # how a self-made ID becomes "verified" on the machines that trust it.
    return load("ROOT") + load("TrustedPeople"), load("CA")


@dataclass
class _Held:
    """A certificate context kept open while signing (and the store it came
    from, for an imported .pfx)."""
    ctx: _PCCERT
    store: Optional[int] = None

    def close(self) -> None:
        if self.ctx:
            _crypt32.CertFreeCertificateContext(self.ctx)
            self.ctx = None
        if self.store:
            _crypt32.CertCloseStore(self.store, 0)
            self.store = None


def _find(thumbprint: str) -> _Held:
    store = _open("MY")
    try:
        for ctx in _store_certs(store):
            if hashlib.sha1(_der(ctx)).hexdigest() == thumbprint:
                held = _Held(_crypt32.CertDuplicateCertificateContext(ctx))
                _crypt32.CertFreeCertificateContext(ctx)  # leaving the loop early: ours to free
                return held
    finally:
        _crypt32.CertCloseStore(store, 0)
    raise DigitalIDError("That certificate is no longer in Windows' certificate store "
                         "(was a smart card removed?).")


def _chain(ctx: _PCCERT) -> List[bytes]:
    """The issuing CAs above a certificate, as Windows builds the chain --
    embedded in the signature so a recipient's checker can find them."""
    para = _CHAIN_PARA(ctypes.sizeof(_CHAIN_PARA))
    chain = ctypes.POINTER(_CHAIN_CONTEXT)()
    if not _crypt32.CertGetCertificateChain(None, ctx, None, None, ctypes.byref(para), 0, None,
                                            ctypes.byref(chain)):
        return []
    try:
        out = []
        if chain.contents.cChain:
            simple = chain.contents.rgpChain[0].contents
            for i in range(1, simple.cElement):  # [0] is the signer's own
                out.append(_der(simple.rgpElement[i].contents.pCertContext))
        return out
    finally:
        _crypt32.CertFreeCertificateChain(chain)


def _attached_key(ctx: _PCCERT) -> int:
    """A CNG key handle already attached to the certificate context (an
    in-memory import's key, never on disk), or 0. CryptAcquire-
    CertificatePrivateKey doesn't look there -- measured: it fails with
    CRYPT_E_NO_KEY_PROPERTY even though the handle is right here."""
    kc = _KEY_CONTEXT()
    size = wintypes.DWORD(ctypes.sizeof(kc))
    if _crypt32.CertGetCertificateContextProperty(ctx, _CERT_KEY_CONTEXT_PROP_ID,
                                                  ctypes.byref(kc), ctypes.byref(size)) \
            and kc.dwKeySpec == _CERT_NCRYPT_KEY_SPEC and kc.hKey:
        return kc.hKey
    return 0


def _acquire_key(ctx: _PCCERT, hwnd: int = 0) -> Tuple[int, bool]:
    """(CNG key handle, whether it's ours to free) -- the normal route for a
    certificate in the store, a smart card included."""
    flags = _CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG | _CRYPT_ACQUIRE_COMPARE_KEY_FLAG
    param = None
    if hwnd:
        # Parent Windows' own prompts (a smart card PIN) on our window.
        window = ctypes.c_void_p(hwnd)
        flags |= _CRYPT_ACQUIRE_WINDOW_HANDLE_FLAG
        param = ctypes.byref(window)
    key = ctypes.c_size_t(0)
    spec, free = wintypes.DWORD(0), wintypes.BOOL(False)
    if not _crypt32.CryptAcquireCertificatePrivateKey(ctx, flags, param, ctypes.byref(key),
                                                      ctypes.byref(spec), ctypes.byref(free)):
        err = ctypes.get_last_error() & 0xFFFFFFFF
        raise DigitalIDError(
            f"Windows wouldn't let this certificate's key sign (error {err:#010x}). If it's "
            "on a smart card, check the card is in; if the PIN prompt was cancelled, try again.")
    return key.value, bool(free.value)


def _sign_digest(ctx: _PCCERT, digest: bytes, algorithm: str, is_rsa: bool,
                 hwnd: int = 0) -> bytes:
    attached = _attached_key(ctx)
    key, free = (attached, False) if attached else _acquire_key(ctx, hwnd)
    try:
        padding = _PKCS1_PADDING(_HASH_IDS[algorithm]) if is_rsa else None
        pad_ref = ctypes.byref(padding) if padding is not None else None
        pad_flag = _BCRYPT_PAD_PKCS1 if is_rsa else 0
        size = wintypes.DWORD(0)
        status = _ncrypt.NCryptSignHash(key, pad_ref, digest, len(digest), None, 0,
                                        ctypes.byref(size), pad_flag)
        if status:
            raise DigitalIDError(f"Windows couldn't sign (NCryptSignHash {status & 0xFFFFFFFF:#010x}).")
        out = ctypes.create_string_buffer(size.value)
        status = _ncrypt.NCryptSignHash(key, pad_ref, digest, len(digest), out, size,
                                        ctypes.byref(size), pad_flag)
        if status:
            raise DigitalIDError(f"Windows couldn't sign (NCryptSignHash {status & 0xFFFFFFFF:#010x}).")
        return out.raw[:size.value]
    finally:
        if free:
            _ncrypt.NCryptFreeObject(key)


def windows_signer(identity: DigitalID, hwnd: int = 0):
    """A pyHanko signer for a certificate in the user's store."""
    return _signer_for(_find(identity.thumbprint), hwnd)


def _signer_for(held: _Held, hwnd: int = 0):
    from asn1crypto import algos, x509
    from pyhanko.sign.signers.pdf_cms import Signer
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    cert = x509.Certificate.load(_der(held.ctx))
    chain = [x509.Certificate.load(der) for der in _chain(held.ctx)]
    is_rsa = cert.public_key.algorithm == "rsa"
    # Room for the signature before it exists (pyHanko's dry run): RSA is
    # exactly the modulus size; ECDSA's DER is at most 2 * (n + 1) + 6.
    key_bytes = (cert.public_key.bit_size + 7) // 8
    placeholder = key_bytes if is_rsa else 2 * (key_bytes + 1) + 6

    class WindowsSigner(Signer):
        def __init__(self):
            super().__init__(signing_cert=cert,
                             cert_registry=SimpleCertificateStore.from_certs([cert] + chain))
            self.held = held

        async def async_sign_raw(self, data: bytes, digest_algorithm: str,
                                 dry_run: bool = False) -> bytes:
            if dry_run:
                return bytes(placeholder)
            algorithm = digest_algorithm.lower()
            if algorithm not in _HASH_IDS:
                raise DigitalIDError(f"Unsupported digest {digest_algorithm} for Windows signing.")
            digest = hashlib.new(algorithm, data).digest()
            raw = _sign_digest(self.held.ctx, digest, algorithm, is_rsa, hwnd)
            # CNG gives ECDSA as r || s; CMS wants the DER sequence.
            return raw if is_rsa else algos.DSASignature.from_p1363(raw).dump()

        def close(self) -> None:
            self.held.close()

    return WindowsSigner()


def imported_pfx_signer(pfx: bytes, password: str, persist_key: bool = False):
    """A Windows (CNG) signer for a .pfx imported into a memory store, for
    the tests and the self-test -- the certificate never enters the user's
    store.

    persist_key=False: the key stays in memory (PKCS12_NO_PERSIST_KEY) and
    signs through the attached handle. persist_key=True: the key goes into
    the user's CNG key store for the duration, so signing takes the same
    CryptAcquireCertificatePrivateKey route a real certificate does; the
    signer's close() deletes the key again (NCryptDeleteKey)."""
    data = (ctypes.c_ubyte * len(pfx)).from_buffer_copy(pfx)
    blob = _BLOB(len(pfx), data)
    flags = _PKCS12_ALWAYS_CNG_KSP | (_CRYPT_USER_KEYSET if persist_key else _PKCS12_NO_PERSIST_KEY)
    store = _crypt32.PFXImportCertStore(ctypes.byref(blob), password, flags)
    if not store:
        raise DigitalIDError(f"Windows couldn't import the test ID (error {ctypes.get_last_error()}).")
    for ctx in _store_certs(store):
        if _has_private_key(ctx) or _attached_key(ctx):
            held = _Held(_crypt32.CertDuplicateCertificateContext(ctx), store)
            _crypt32.CertFreeCertificateContext(ctx)  # leaving the loop early: ours to free
            signer = _signer_for(held)
            if persist_key:
                plain_close = signer.close

                def close_and_delete_key():
                    try:
                        if held.ctx:
                            key, _ = _acquire_key(held.ctx)
                            status = _ncrypt.NCryptDeleteKey(key, 0)  # also frees the handle
                            if status:
                                raise DigitalIDError(
                                    f"The temporary test key wasn't deleted from the key "
                                    f"store (NCryptDeleteKey {status & 0xFFFFFFFF:#010x}).")
                    finally:
                        plain_close()
                signer.close = close_and_delete_key
            return signer
    _crypt32.CertCloseStore(store, 0)
    raise DigitalIDError("The test ID has no private key.")
