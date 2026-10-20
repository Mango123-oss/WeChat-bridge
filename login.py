#!/usr/bin/env python3
"""WeChat iLink QR login (stdlib-only).

  python3 login.py

Polls the QR status until the user scans + confirms in WeChat, then stores
credentials at state/credentials.json (mode 600).

Outputs:
  qr/latest.png              — the QR code (refreshed on expiry), send this to the user
  state/login_status.json    — {"status": waiting_scan|scanned|done|expired|error, ...}
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import ilink
from ilink import ApiError

STATE = BASE / "state"
FLAGS = STATE / "flags"
QR_DIR = BASE / "qr"
CRED_PATH = STATE / "credentials.json"
STATUS_PATH = STATE / "login_status.json"
TIMEOUT = 600
QR_POLL = 2.0


def set_status(**kw) -> None:
    STATUS_PATH.write_text(json.dumps({"ts": int(time.time()), **kw}, ensure_ascii=False), "utf-8")
    print(f"[login] {kw}", flush=True)


def save_qr(content: str) -> bool:
    """qrcode_img_content is a lite-app URL; render it as a QR image for WeChat scan."""
    QR_DIR.mkdir(parents=True, exist_ok=True)
    target = QR_DIR / "latest.png"
    try:
        import qrcode
        from PIL import Image, ImageDraw

        img = qrcode.make(content, box_size=10, border=2).convert("RGB")
        w, h = img.size
        canvas = Image.new("RGB", (w, h + 60), "white")
        canvas.paste(img, (0, 0))
        ImageDraw.Draw(canvas).text((20, h + 18), "WeChat: scan to log in Muse bridge", fill="black")
        canvas.save(target)
        print(f"[login] QR rendered -> {target} ({target.stat().st_size} bytes)", flush=True)
        return True
    except Exception as e:
        (QR_DIR / "latest.txt").write_text(content, "utf-8")
        print(f"[login] QR render failed ({e}); raw URL saved to latest.txt", flush=True)
        return False


def save_credentials(data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": data["bot_token"],
        "base_url": data.get("baseurl") or ilink.BASE_URL,
        "account_id": data.get("ilink_bot_id"),
        "user_id": data.get("ilink_user_id"),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    CRED_PATH.write_text(json.dumps(payload, indent=2), "utf-8")
    os.chmod(CRED_PATH, 0o600)
    print(f"[login] credentials saved (account {payload['account_id']})", flush=True)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    FLAGS.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + TIMEOUT
    refresh = 0

    set_status(status="waiting_scan", detail="正在生成二维码")

    while time.time() < deadline:
        refresh += 1
        if refresh > 5:
            set_status(status="error", detail="二维码过期太多次，登录中止")
            return 1
        try:
            qr = ilink.get_qr_code()
        except ApiError as e:
            set_status(status="error", detail=f"取二维码失败：{e}")
            return 1

        qrcode, content = qr.get("qrcode", ""), qr.get("qrcode_img_content", "")
        if not qrcode or not content:
            set_status(status="error", detail=f"二维码接口返回异常：{str(qr)[:200]}")
            return 1
        save_qr(content)
        set_status(status="waiting_scan", detail="请用微信扫码（可从相册选择）", refresh=refresh)

        base = ilink.BASE_URL
        last = ""
        while time.time() < deadline:
            try:
                st = ilink.poll_qr_status(base, qrcode)
            except ApiError as e:
                print(f"[login] poll error: {e}", flush=True)
                time.sleep(QR_POLL)
                continue
            cur = st.get("status", "")
            if cur != last:
                last = cur
                print(f"[login] qr status -> {cur}", flush=True)
                if cur == "scaned":
                    set_status(status="scanned", detail="已扫码，请在微信里确认登录")
                elif cur == "scaned_but_redirect" and st.get("redirect_host"):
                    base = f"https://{st['redirect_host']}"
            if cur == "confirmed":
                if not st.get("bot_token"):
                    set_status(status="error", detail="已确认但缺少 token")
                    return 1
                save_credentials(st)
                (FLAGS / "session_expired").unlink(missing_ok=True)
                set_status(status="done", detail="登录成功")
                print("[login] DONE", flush=True)
                return 0
            if cur == "expired":
                print("[login] QR expired — refreshing", flush=True)
                break  # outer loop gets a new QR
            if cur == "need_verifycode":
                set_status(status="waiting_scan", detail="微信要求输入配对码，请告诉我微信上显示的数字")
                print("[login] need_verifycode — waiting for user to provide the code", flush=True)
                time.sleep(10)
                continue
            if cur in ("verify_code_blocked", "binded_redirect"):
                print(f"[login] status {cur} — refreshing QR", flush=True)
                break
            time.sleep(QR_POLL)

    set_status(status="expired", detail="登录超时，请重新发起")
    return 1


if __name__ == "__main__":
    sys.exit(main())
