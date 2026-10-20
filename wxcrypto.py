#!/usr/bin/env python3
"""WeChat CDN media crypto: AES-128-ECB + PKCS7.

Ported from Liu-zhongxian/wechat-bridge (MIT License) crypto.py, adapted to
this bridge: pycryptodome is vendored under vendor_pkgs/ so the daemon finds
it regardless of the systemd unit's HOME / site-packages state.

The CDN aes_key comes in 4 shapes: 16 raw bytes / 32 hex chars /
base64(16 bytes) / base64(32 hex chars).
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent / "vendor_pkgs"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    from Crypto.Cipher import AES
    pad = 16 - len(data) % 16
    data = data + bytes([pad]) * pad
    return AES.new(key, AES.MODE_ECB).encrypt(data)


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    from Crypto.Cipher import AES
    out = AES.new(key, AES.MODE_ECB).decrypt(data)
    pad = out[-1]
    if pad < 1 or pad > 16:
        raise ValueError("AES decrypt: bad padding")
    if out[-pad:] != bytes([pad]) * pad:
        raise ValueError("AES decrypt: padding check failed")
    return out[:-pad]


def _is_hex(b: bytes) -> bool:
    return all((48 <= c <= 57) or (97 <= c <= 102) or (65 <= c <= 70) for c in b)


def parse_aes_key(s) -> bytes:
    """Parse a WeChat CDN aes_key. Strict base64 (standard + URL-safe)."""
    if isinstance(s, str):
        s = s.encode("utf-8")
    if len(s) == 16:
        return bytes(s)
    if len(s) == 32 and _is_hex(s):
        return bytes.fromhex(s.decode("ascii"))

    def _b64_try(data, altchars=None):
        data = data.strip()
        pad = (-len(data)) % 4
        try:
            return base64.b64decode(data + b"=" * pad, altchars=altchars, validate=True)
        except Exception:
            return None

    for alt in (None, b"-_"):
        decoded = _b64_try(s, altchars=alt)
        if decoded is None:
            continue
        if len(decoded) == 32 and _is_hex(decoded):
            return bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) == 16:
            return decoded
    raise ValueError("aes_key format not recognised (len=%d)" % len(s))


def try_decrypt_variants(encrypted: bytes, raw_key_str, key_override: bytes | None = None):
    """Brute-force key-derivation x mode combos; return (plaintext, strategy).

    Fallback for media (notably video) whose key format is unclear: whichever
    combo yields a recognisable file header (JPEG / MP4 ftyp) wins.
    """
    from Crypto.Cipher import AES
    candidates = []
    raw = raw_key_str.encode("utf-8") if isinstance(raw_key_str, str) else (raw_key_str or b"")
    if key_override:
        candidates.append((key_override, "ECB", None, "override-ECB"))
    derivations = []
    if len(raw) == 16:
        derivations.append((raw, "raw16"))
    if len(raw) == 32 and _is_hex(raw):
        derivations.append((bytes.fromhex(raw.decode("ascii")), "hex32"))
    for alt in (None, b"-_"):
        d = raw.strip()
        pad = (-len(d)) % 4
        try:
            dec = base64.b64decode(d + b"=" * pad, altchars=alt, validate=True)
        except Exception:
            continue
        tag = "b64" if alt is None else "b64url"
        if len(dec) == 16:
            derivations.append((dec, tag + "-16"))
        if len(dec) == 32:
            if _is_hex(dec):
                derivations.append((bytes.fromhex(dec.decode("ascii")), tag + "-hex32"))
            derivations.append((dec, tag + "-32(AES256)"))
            derivations.append((dec[:16], tag + "-first16"))
            derivations.append((hashlib.md5(dec).digest(), tag + "-md5"))
    for kb, dname in derivations:
        if len(kb) == 16:
            candidates.append((kb, "ECB", None, dname + "-ECB"))
            candidates.append((kb, "CBC", b"\x00" * 16, dname + "-CBC0"))
        elif len(kb) == 32:
            candidates.append((kb, "ECB", None, dname + "-ECB"))
    for kb, mode, iv, name in candidates:
        try:
            if mode == "ECB":
                out = AES.new(kb, AES.MODE_ECB).decrypt(encrypted)
            else:
                out = AES.new(kb, AES.MODE_CBC, iv=iv).decrypt(encrypted)
        except Exception:
            continue
        if not out:
            continue
        pad = out[-1]
        if pad < 1 or pad > 16:
            continue
        if out[-pad:] != bytes([pad]) * pad:
            continue
        plain = out[:-pad]
        if looks_like_file(plain):
            return plain, name
    return None, None


def looks_like_file(data: bytes) -> bool:
    """Magic-byte sniff for the file types WeChat media actually carries."""
    return (
        data[:3] == b"\xff\xd8\xff"                       # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"               # PNG
        or data[:4] == b"GIF8"                            # GIF
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WEBP
        or b"ftyp" in data[:32]                           # MP4/MOV
        or data[:4] == b"PK\x03\x04"                      # ZIP/OOXML
        or data[:4] == b"%PDF"                            # PDF
        or data[:4] == b"\xff\xf1" or data[:4] == b"\xff\xf9"  # AAC
        or data[:3] == b"ID3"                             # MP3
    )
