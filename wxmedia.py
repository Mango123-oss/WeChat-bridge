#!/usr/bin/env python3
"""WeChat CDN media transfer: encrypted download + upload.

Ported from Liu-zhongxian/wechat-bridge (MIT License) media.py, adapted to
this bridge's ilink client (stdlib urllib, ApiError model, proxy-aware).

Download: CDN ciphertext -> AES-128-ECB decrypt -> local file (0600).
Upload: random AES key -> encrypt -> getuploadurl -> POST ciphertext to CDN
        -> media ref for sendmessage.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import ilink
import wxcrypto

log = logging.getLogger("bridge.media")

MAX_MEDIA_BYTES = 100 << 20  # 100 MB
_UA = {"User-Agent": "Mozilla/5.0"}


def media_type_from_path(path: str) -> int:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ilink.IMAGE
    if ext in (".mp4", ".mov", ".avi", ".mkv"):
        return ilink.VIDEO
    return ilink.FILE


# ---- download ----

def _candidate_keys(media: dict, aeskey_item: str) -> list[tuple[str, bytes]]:
    """Ordered (source, key) candidates for an inbound media object.

    The item-level `aeskey` (image_item.aeskey) has priority per the SDK,
    then media.aes_key. Both go through the full format parser (hex /
    base64 variants) — the item field is NOT guaranteed to be plain hex.
    """
    out: list[tuple[str, bytes]] = []
    for src, raw in (("item.aeskey", aeskey_item), ("media.aes_key", media.get("aes_key") or "")):
        if not raw:
            continue
        try:
            k = wxcrypto.parse_aes_key(raw)
        except Exception as e:
            log.warning("%s unparseable (len=%d): %s", src, len(raw), e)
            continue
        if all(k != existing for _, existing in out):
            out.append((src, k))
    return out


def download_media(media: dict, aeskey_item: str, dest_path: Path) -> Path:
    """Download + decrypt one CDN media object to dest_path.

    media: the item's `media` dict (encrypt_query_param / full_url / aes_key).
    aeskey_item: item-level key string (image_item.aeskey, may be "").
    Note: video must use the encrypt_query_param URL; full_url data fails
    to decrypt for video.
    """
    candidates = _candidate_keys(media, aeskey_item)
    if not candidates:
        raise ValueError("no usable AES key in media/item")

    urls = []
    param = media.get("encrypt_query_param") or ""
    if param:
        urls.append(("param", ilink.CDN_BASE + "/download?encrypted_query_param=" +
                     urllib.parse.quote(param, safe="")))
    if media.get("full_url"):
        urls.append(("full_url", media["full_url"]))
    if not urls:
        raise ValueError("media has neither full_url nor encrypt_query_param")

    encrypted = None
    for name, url in urls:
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read(MAX_MEDIA_BYTES + 1)
            log.info("download source %s: %d bytes", name, len(data))
            encrypted = data
            break
        except Exception as e:
            log.warning("download source %s failed: %s", name, e)
    if encrypted is None:
        raise ValueError("all download sources failed")
    if len(encrypted) > MAX_MEDIA_BYTES:
        raise ValueError("media exceeds 100MB cap")

    plain = None
    if wxcrypto.looks_like_file(encrypted):
        log.info("download is already plaintext (magic bytes match)")
        plain = encrypted
    else:
        for src, key in candidates:
            try:
                plain = wxcrypto.aes_ecb_decrypt(encrypted, key)
                log.info("decrypt ok with %s", src)
                break
            except Exception as e:
                log.info("decrypt with %s failed: %s", src, e)
        if plain is None:
            for raw in (aeskey_item, media.get("aes_key") or ""):
                if not raw:
                    continue
                plain, strategy = wxcrypto.try_decrypt_variants(
                    encrypted, raw, candidates[0][1])
                if plain is not None:
                    log.info("decrypt ok via variant %s", strategy)
                    break
        if plain is None:
            dbg = dest_path.parent / "debug"
            dbg.mkdir(parents=True, exist_ok=True)
            dump = dbg / (dest_path.stem + ".bin")
            dump.write_bytes(encrypted)
            os.chmod(dump, 0o600)
            raise ValueError(f"all decrypt strategies failed; ciphertext kept at {dump}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
    tmp.write_bytes(plain)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest_path)
    return dest_path


# ---- upload ----

def upload_media(base_url: str, token: str, to_user_id: str,
                 data: bytes, item_type: int) -> tuple[str, str, int]:
    """Encrypt + upload to the WeChat CDN.

    Returns (encrypt_query_param, aes_key_b64, cipher_size) for the media ref.
    """
    aes_key = os.urandom(16)
    filekey = os.urandom(16).hex()
    encrypted = wxcrypto.aes_ecb_encrypt(data, aes_key)
    upload_type = {ilink.IMAGE: ilink.UPLOAD_IMAGE,
                   ilink.VIDEO: ilink.UPLOAD_VIDEO}.get(item_type, ilink.UPLOAD_FILE)
    body = {
        "filekey": filekey,
        "media_type": upload_type,
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data).hexdigest(),
        "filesize": len(encrypted),
        "no_need_thumb": True,
        "aeskey": aes_key.hex(),
        "base_info": ilink.base_info(),
    }
    resp = ilink.get_upload_url(base_url, token, body)  # raises ApiError on ret!=0
    upload_url = (resp.get("upload_full_url") or "").strip()
    if not upload_url:
        param = resp.get("upload_param") or ""
        if not param:
            raise ilink.ApiError("getuploadurl returned no upload address", payload=resp)
        upload_url = (ilink.CDN_BASE + "/upload?encrypted_query_param=" +
                      urllib.parse.quote(param, safe="") +
                      "&filekey=" + urllib.parse.quote(filekey, safe=""))

    download_param = ""
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            upload_url, data=encrypted,
            headers={"Content-Type": "application/octet-stream", **_UA},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                download_param = r.headers.get("x-encrypted-param") or ""
            if not download_param:
                raise ilink.ApiError("CDN upload response missing x-encrypted-param")
            break
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise ilink.ApiError(f"CDN upload client error HTTP {e.code}",
                                     http_status=e.code)
            last_err = e
        except Exception as e:
            last_err = e
        log.warning("CDN upload attempt %d failed: %s", attempt + 1, last_err)
        time.sleep(2)
    else:
        raise ilink.ApiError(f"CDN upload failed after retries: {last_err}")

    # Match the official plugin: aes_key is b64 of the 32-char hex string.
    aes_key_b64 = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
    return download_param, aes_key_b64, len(encrypted)
