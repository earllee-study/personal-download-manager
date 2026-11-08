"""Add/check/remove a test certificate in the *current user* Root store.

Local HTTP/2 benchmarks need clients that use the Windows root store to trust
the test certificate without ``--insecure``.  ``certutil -addstore`` may block
on an interactive Windows confirmation, so this helper talks to CryptoAPI directly:
no UI, no admin rights, current-user store only.

Usage:
    python bench/certstore.py install tests/certs/localhost.crt
    python bench/certstore.py check   <SHA1-thumbprint>
    python bench/certstore.py remove  <SHA1-thumbprint>

Only the current user's Root store is touched.  Remove the certificate as soon
as the comparison is finished (see docs/testing.md).
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
from ctypes import wintypes
from pathlib import Path

CERT_STORE_PROV_SYSTEM_W = 10
CERT_SYSTEM_STORE_CURRENT_USER = 0x00010000
X509_ASN_ENCODING = 0x00000001
PKCS_7_ASN_ENCODING = 0x00010000
CERT_STORE_ADD_REPLACE_EXISTING = 3
CERT_FIND_SHA1_HASH = 0x00010000
CERT_CLOSE_STORE_FORCE_FLAG = 0x0001

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

crypt32.CertOpenStore.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
]
crypt32.CertOpenStore.restype = ctypes.c_void_p

crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
crypt32.CertCloseStore.restype = wintypes.BOOL

crypt32.CertAddEncodedCertificateToStore.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
]
crypt32.CertAddEncodedCertificateToStore.restype = wintypes.BOOL

crypt32.CertFindCertificateInStore.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
crypt32.CertFindCertificateInStore.restype = ctypes.c_void_p

crypt32.CertDeleteCertificateFromStore.argtypes = [ctypes.c_void_p]
crypt32.CertDeleteCertificateFromStore.restype = wintypes.BOOL

crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
crypt32.CertFreeCertificateContext.restype = wintypes.BOOL


class CRYPT_HASH_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def pem_to_der(path: Path) -> bytes:
    text = path.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in text:
        return text  # already DER
    import ssl

    return ssl.PEM_cert_to_DER_cert(text.decode("ascii"))


def open_root_store() -> int:
    handle = crypt32.CertOpenStore(
        ctypes.c_void_p(CERT_STORE_PROV_SYSTEM_W),
        0,
        None,
        CERT_SYSTEM_STORE_CURRENT_USER,
        ctypes.c_wchar_p("Root"),
    )
    if not handle:
        raise OSError(ctypes.get_last_error(), "CertOpenStore(CurrentUser\\Root)")
    return handle


def find(store: int, sha1: bytes):
    blob = (ctypes.c_ubyte * len(sha1)).from_buffer_copy(sha1)
    hashed = CRYPT_HASH_BLOB(len(sha1), ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)))
    return crypt32.CertFindCertificateInStore(
        store,
        X509_ASN_ENCODING | PKCS_7_ASN_ENCODING,
        0,
        CERT_FIND_SHA1_HASH,
        ctypes.byref(hashed),
        None,
    )


def thumbprint(der: bytes) -> str:
    return hashlib.sha1(der).hexdigest().upper()


def install(path: Path) -> str:
    der = pem_to_der(path)
    store = open_root_store()
    try:
        buf = (ctypes.c_ubyte * len(der)).from_buffer_copy(der)
        context = ctypes.c_void_p()
        ok = crypt32.CertAddEncodedCertificateToStore(
            store,
            X509_ASN_ENCODING | PKCS_7_ASN_ENCODING,
            buf,
            len(der),
            CERT_STORE_ADD_REPLACE_EXISTING,
            ctypes.byref(context),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CertAddEncodedCertificateToStore")
        if context:
            crypt32.CertFreeCertificateContext(context)
    finally:
        crypt32.CertCloseStore(store, CERT_CLOSE_STORE_FORCE_FLAG)
    return thumbprint(der)


def check(sha1_hex: str) -> bool:
    sha1 = bytes.fromhex(sha1_hex)
    store = open_root_store()
    try:
        context = find(store, sha1)
        if not context:
            return False
        crypt32.CertFreeCertificateContext(context)
        return True
    finally:
        crypt32.CertCloseStore(store, CERT_CLOSE_STORE_FORCE_FLAG)


def remove(sha1_hex: str) -> bool:
    sha1 = bytes.fromhex(sha1_hex)
    store = open_root_store()
    try:
        context = find(store, sha1)
        if not context:
            return False
        try:
            ok = crypt32.CertDeleteCertificateFromStore(context)
        finally:
            crypt32.CertFreeCertificateContext(context)
        if not ok:
            raise OSError(ctypes.get_last_error(), "CertDeleteCertificateFromStore")
        return True
    finally:
        crypt32.CertCloseStore(store, CERT_CLOSE_STORE_FORCE_FLAG)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("install")
    add.add_argument("path", type=Path)
    for name in ("check", "remove"):
        item = sub.add_parser(name)
        item.add_argument("thumbprint")
    args = parser.parse_args(argv)

    if args.command == "install":
        print(install(args.path))
        return 0
    if args.command == "check":
        present = check(args.thumbprint)
        print("present" if present else "absent")
        return 0 if present else 1
    if args.command == "remove":
        removed = remove(args.thumbprint)
        print("removed" if removed else "not found")
        return 0 if removed else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
