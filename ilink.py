#!/usr/bin/env python3
"""Minimal iLink Bot API client.

Pure stdlib (urllib honors *_proxy env vars, which this VM needs for egress).
Protocol reverse-engineered from wechatbot-sdk 0.3.0 (MIT) — see vendor/.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import struct
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_URL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
APP_CLIENT_VERSION = "196608"  # 0x00030000
BOT_AGENT = "MuseBridge/1.0"

# message types / states / item types (from SDK types.py)
USER, BOT = 1, 2
FINISH = 2
TEXT = 1
IMAGE, VOICE, FILE, VIDEO = 2, 3, 4, 5

CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
# getuploadurl media_type numbering differs from item types
UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE = 1, 2, 3


class ApiError(Exception):
    def __init__(self, message, *, http_status=0, errcode=0, payload=None):
        super().__init__(message)
        self.http_status = http_status
        self.errcode = errcode
        self.payload = payload

    @property
    def is_session_expired(self) -> bool:
        return self.errcode == -14


def _headers(token: str | None = None) -> dict:
    h = {
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": APP_CLIENT_VERSION,
        "User-Agent": BOT_AGENT,
    }
    if token:
        uin = struct.unpack(">I", os.urandom(4))[0]
        h.update({
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": base64.b64encode(str(uin).encode()).decode(),
        })
    return h


def _call(method: str, url: str, token: str | None = None,
          body: dict | None = None, timeout: int = 50) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            payload = json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ApiError(f"HTTP {e.code}: {detail}", http_status=e.code)
    except (TimeoutError, urllib.error.URLError) as e:
        raise ApiError(f"network error: {e}")
    except (ConnectionError, http.client.HTTPException, OSError) as e:
        # The gateway routinely drops idle/queued connections with a bare
        # RemoteDisconnected that urllib does not wrap — normalise it so
        # callers' ApiError retry logic applies.
        raise ApiError(f"network error: {e}")
    ret, errcode = payload.get("ret"), payload.get("errcode")
    if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode != 0):
        code = errcode if isinstance(errcode, int) and errcode != 0 else (ret or 0)
        raise ApiError(payload.get("errmsg") or f"iLink error ret={ret} errcode={errcode}",
                       errcode=code, payload=payload)
    return payload


def _base_info() -> dict:
    return {"channel_version": "1.0", "bot_agent": BOT_AGENT}


def base_info() -> dict:
    return _base_info()


# ---- login ----

def get_qr_code(base_url: str = BASE_URL, local_tokens: list | None = None) -> dict:
    return _call("POST", f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3",
                 body={"local_token_list": local_tokens or []})


def poll_qr_status(base_url: str, qrcode: str, verify_code: str | None = None) -> dict:
    url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode, safe='')}"
    if verify_code:
        url += f"&verify_code={urllib.parse.quote(verify_code, safe='')}"
    return _call("GET", url)


# ---- messaging ----

def get_updates(base_url: str, token: str, cursor: str, timeout: int = 70) -> dict:
    return _call("POST", f"{base_url}/ilink/bot/getupdates", token,
                 {"get_updates_buf": cursor, "base_info": _base_info()}, timeout=timeout)


def build_text_message(user_id: str, context_token: str, text: str) -> dict:
    return {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": uuid.uuid4().hex,
        "message_type": BOT,
        "message_state": FINISH,
        "context_token": context_token,
        "item_list": [{"type": TEXT, "text_item": {"text": text}}],
    }


def send_message(base_url: str, token: str, msg: dict) -> dict:
    return _call("POST", f"{base_url}/ilink/bot/sendmessage", token,
                 {"msg": msg, "base_info": _base_info()})


def get_upload_url(base_url: str, token: str, body: dict) -> dict:
    return _call("POST", f"{base_url}/ilink/bot/getuploadurl", token, body, timeout=30)


def build_media_message(user_id: str, context_token: str, item_type: int,
                        media_ref: dict, cipher_size: int,
                        file_name: str = "", raw_size: int = 0) -> dict:
    if item_type == IMAGE:
        item = {"type": IMAGE,
                "image_item": {"media": media_ref, "mid_size": cipher_size}}
    elif item_type == VIDEO:
        item = {"type": VIDEO,
                "video_item": {"media": media_ref, "video_size": cipher_size}}
    else:
        item = {"type": FILE,
                "file_item": {"media": media_ref, "file_name": file_name,
                              "len": str(raw_size)}}
    return {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": uuid.uuid4().hex,
        "message_type": BOT,
        "message_state": FINISH,
        "context_token": context_token,
        "item_list": [item],
    }


def get_config(base_url: str, token: str, user_id: str, context_token: str) -> dict:
    return _call("POST", f"{base_url}/ilink/bot/getconfig", token, {
        "ilink_user_id": user_id,
        "context_token": context_token,
        "base_info": _base_info(),
    }, timeout=20)


def send_typing(base_url: str, token: str, user_id: str, ticket: str, start: bool) -> dict:
    return _call("POST", f"{base_url}/ilink/bot/sendtyping", token, {
        "ilink_user_id": user_id,
        "typing_ticket": ticket,
        "status": 1 if start else 2,
        "base_info": _base_info(),
    }, timeout=20)


def notify(base_url: str, token: str, online: bool) -> None:
    ep = "notifystart" if online else "notifystop"
    try:
        _call("POST", f"{base_url}/ilink/bot/msg/{ep}", token,
              {"base_info": _base_info()}, timeout=20)
    except ApiError:
        pass  # non-fatal


def extract_text(raw: dict) -> tuple[str, str]:
    """(kind, text) for an incoming USER message. v1: text + placeholders."""
    parts, kind = [], "text"
    for item in raw.get("item_list", []):
        t = item.get("type")
        if t == TEXT and item.get("text_item", {}).get("text"):
            parts.append(item["text_item"]["text"])
        elif t == 2:
            kind = "media"; parts.append("[图片]")
        elif t == 3:
            vt = (item.get("voice_item") or {}).get("text") or ""
            kind = "voice" if vt else "media"
            parts.append(f"[语音转文字] {vt}" if vt else "[语音]")
        elif t == 4:
            fn = (item.get("file_item") or {}).get("file_name") or "文件"
            kind = "media"; parts.append(f"[文件：{fn}]")
        elif t == 5:
            kind = "media"; parts.append("[视频]")
    text = "\n".join(p for p in parts if p).strip()
    return (kind, text) if text else ("media", "[不支持的消息类型]")


def extract_media(raw: dict) -> list[dict]:
    """Media items of an incoming USER message (image / file / video).

    Each entry: {"item_type", "kind", "media", "aeskey_hex", "file_name"}.
    `media` is the CDN ref dict (encrypt_query_param / full_url / aes_key);
    `aeskey_hex` is the item-level hex key override, possibly "".
    Voice is skipped: its transcript already flows through extract_text.
    """
    out = []
    for item in raw.get("item_list", []):
        t = item.get("type")
        if t not in (IMAGE, FILE, VIDEO):
            continue
        sub = (item.get("image_item") or item.get("video_item")
               or item.get("file_item") or {})
        media = sub.get("media") or {}
        if not media:
            continue
        out.append({
            "item_type": t,
            "kind": {IMAGE: "image", VIDEO: "video", FILE: "file"}[t],
            "media": media,
            "aeskey_hex": sub.get("aeskey") or "",
            "file_name": sub.get("file_name") or "",
        })
    return out
