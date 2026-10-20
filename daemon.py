#!/usr/bin/env python3
"""WeChat iLink bridge daemon (stdlib-only).

Long-polls the iLink gateway and bridges it to local file queues:

  iLink --getupdates--> inbox/*.json --(cron agent turn)--> outbox/*.json --sendmessage--> iLink

Only the transport lives here. Message understanding / replies happen in the
`wechat-bridge-triage` cron's agent turn, which drains inbox/ and writes
outbox/. This keeps the daemon dumb and restart-safe.

v2: text + media. Incoming images/files/videos are downloaded from the
WeChat CDN (AES decrypt) into media/ and referenced from the inbox record
as `media_paths`; outgoing jobs may carry `media_path` to send an
image/file/video, with `text` as an optional caption. Voice still flows
as its transcript text.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import ilink
from ilink import ApiError

INBOX = BASE / "inbox"
OUTBOX = BASE / "outbox"
MEDIA = BASE / "media"
STATE = BASE / "state"
FLAGS = STATE / "flags"
LOGS = BASE / "logs"

CRED_PATH = STATE / "credentials.json"
CURSOR_PATH = STATE / "cursor.txt"
CONTEXT_PATH = STATE / "context.json"      # {user_id: context_token}
CONFIG_PATH = BASE / "config.json"
LAST_USER_PATH = STATE / "last_user.txt"
CHUNK = 2000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bridge] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOGS / "daemon.log"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bridge")

try:
    import wxmedia
except Exception as e:  # media is an add-on; the text bridge must survive it
    wxmedia = None
    log.error("wxmedia unavailable, media disabled: %r", e)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", "utf-8")


def chunk_text(text: str, size: int = CHUNK) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n"):
        if cur and len(cur) + len(para) + 1 > size:
            chunks.append(cur); cur = ""
        while len(para) > size:
            chunks.append(para[:size]); para = para[size:]
        cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


class Bridge:
    def __init__(self) -> None:
        self.cfg = load_json(CONFIG_PATH, {"allowed_users": []})
        creds = load_json(CRED_PATH, None)
        if not creds or not creds.get("token"):
            log.error("no credentials at %s — run login.py first", CRED_PATH)
            sys.exit(2)
        self.base_url = creds.get("base_url") or ilink.BASE_URL
        self.token = creds["token"]
        self.cursor = CURSOR_PATH.read_text("utf-8").strip() if CURSOR_PATH.exists() else ""
        self.ctx: dict = load_json(CONTEXT_PATH, {})
        self.tickets: dict = {}  # user_id -> typing_ticket (refreshed per session)
        self.typing_on: set[str] = set()
        self.recent: dict[tuple[str, str], float] = {}
        self.lock = threading.Lock()

    # -- helpers --

    def typing(self, user_id: str, start: bool) -> None:
        # iLink 网关偶发断开连接，加 3 次重试，避免指示器悄悄丢失。
        for attempt in range(3):
            try:
                ct = self.ctx.get(user_id, "")
                if not ct:
                    return
                ticket = self.tickets.get(user_id)
                if ticket is None:
                    cfg = ilink.get_config(self.base_url, self.token, user_id, ct)
                    ticket = cfg.get("typing_ticket") or ""
                    self.tickets[user_id] = ticket
                if ticket:
                    ilink.send_typing(self.base_url, self.token, user_id, ticket, start)
                return
            except Exception as e:
                log.debug("typing failed (attempt %d): %s", attempt + 1, e)
                time.sleep(2)

    def allowed(self, user_id: str) -> bool:
        allowed = self.cfg.get("allowed_users") or []
        if not allowed:
            allowed.append(user_id)
            self.cfg["allowed_users"] = allowed
            save_json(CONFIG_PATH, self.cfg)
            log.warning("PAIRED with first sender %s (saved to config.json)", user_id)
            return True
        return user_id in allowed

    def file_inbox(self, user_id: str, kind: str, text: str,
                   media_paths: list[str] | None = None) -> None:
        now = time.time()
        # Media messages share placeholder text ("[图片]"): without the path
        # in the key, two images in a row would dedup the second one away.
        key = (user_id, f"{text}|{media_paths[0]}" if media_paths else text)
        with self.lock:
            if now - self.recent.get(key, 0) < 60:
                log.info("dedup: skip repeat from %s", user_id); return
            self.recent[key] = now
            for k, ts in list(self.recent.items()):
                if now - ts > 600:
                    del self.recent[k]
        msg = {"id": uuid.uuid4().hex[:12], "from_user_id": user_id,
               "kind": kind, "text": text, "ts": int(now)}
        if media_paths:
            msg["media_paths"] = media_paths
        (INBOX / f"{msg['ts']}-{msg['id']}.json").write_text(
            json.dumps(msg, ensure_ascii=False, indent=2), "utf-8")
        log.info("inbox <- %s (%s, %d chars%s)", user_id, kind, len(text),
                 f", {len(media_paths)} media" if media_paths else "")

    def save_media(self, raw: dict) -> list[str]:
        """Download + decrypt the media items of an incoming message.

        Best-effort: a failed download keeps the placeholder text from
        extract_text and never loses the message itself.
        """
        if wxmedia is None:
            return []
        try:
            items = ilink.extract_media(raw)
        except Exception as e:
            log.warning("extract_media failed: %r", e)
            return []
        paths: list[str] = []
        day = time.strftime("%Y%m%d")
        mid = str(raw.get("message_id") or uuid.uuid4().hex[:12])
        for i, it in enumerate(items):
            if it["kind"] == "file":
                name = "".join(c for c in it["file_name"] if c not in "/\\").strip()
                fname = f"{mid}_{name}" if name else f"{mid}_file.bin"
            else:
                ext = {"image": "jpg", "video": "mp4"}[it["kind"]]
                fname = f"{mid}_{it['kind']}_{i}.{ext}"
            dest = MEDIA / day / fname
            try:
                wxmedia.download_media(it["media"], it["aeskey_hex"], dest)
                paths.append(str(dest))
                log.info("media saved <- %s", dest)
            except Exception as e:
                log.error("media download failed (%s): %s", it["kind"], e)
        if items and not paths:
            # Keep the raw message for offline forensics (media keys are
            # message-scoped, not account credentials).
            dbg = MEDIA / "debug"
            dbg.mkdir(parents=True, exist_ok=True)
            raw_path = dbg / f"{mid}.json"
            raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
            os.chmod(raw_path, 0o600)
            log.error("media forensics saved -> %s", raw_path)
        return paths

    def send_media(self, user_id: str, ct: str, media_file: Path, caption: str) -> None:
        """Upload-then-send one media file with an optional caption.

        Upload happens first (invisible to the user if it fails), then the
        caption chunks, then the media message itself.
        """
        data = media_file.read_bytes()
        if len(data) > wxmedia.MAX_MEDIA_BYTES:
            raise ValueError(f"media file over 100MB cap: {media_file}")
        item_type = wxmedia.media_type_from_path(str(media_file))
        param, key_b64, cipher_size = wxmedia.upload_media(
            self.base_url, self.token, user_id, data, item_type)
        ref = {"encrypt_query_param": param, "aes_key": key_b64, "encrypt_type": 1}
        for chunk in chunk_text(caption) if caption else []:
            ilink.send_message(self.base_url, self.token,
                               ilink.build_text_message(user_id, ct, chunk))
        ilink.send_message(self.base_url, self.token,
                           ilink.build_media_message(user_id, ct, item_type, ref,
                                                     cipher_size, media_file.name,
                                                     len(data)))

    def session_dead(self, where: str) -> None:
        log.error("SESSION EXPIRED at %s — flagging and exiting clean", where)
        save_json(FLAGS / "session_expired", {"ts": int(time.time())})
        for u in list(self.typing_on):
            self.typing(u, False)
        ilink.notify(self.base_url, self.token, False)
        sys.exit(0)  # clean exit: systemd won't restart; cron notifies the user

    # -- loops --

    def poll_loop(self) -> None:
        retry = 2.0
        while True:
            try:
                updates = ilink.get_updates(self.base_url, self.token, self.cursor, timeout=70)
                buf = updates.get("get_updates_buf")
                if buf:
                    self.cursor = buf
                    CURSOR_PATH.write_text(buf, "utf-8")
                retry = 2.0
                for raw in updates.get("msgs", []):
                    if raw.get("message_type") != ilink.USER:
                        continue
                    user_id = raw.get("from_user_id", "")
                    ct = raw.get("context_token", "")
                    if ct and self.ctx.get(user_id) != ct:
                        self.ctx[user_id] = ct
                        save_json(CONTEXT_PATH, self.ctx)
                    if not self.allowed(user_id):
                        log.warning("ignore message from non-allowlisted %s", user_id)
                        continue
                    LAST_USER_PATH.write_text(user_id, "utf-8")
                    kind, text = ilink.extract_text(raw)
                    media_paths = self.save_media(raw)
                    self.file_inbox(user_id, kind, text, media_paths)
                    if user_id not in self.typing_on:
                        self.typing_on.add(user_id)
                        self.typing(user_id, True)
            except ApiError as e:
                if e.is_session_expired:
                    self.session_dead("poll")
                log.error("poll ApiError: %s (retry %.0fs)", e, retry)
                time.sleep(retry); retry = min(retry * 2, 30.0)
            except Exception as e:
                log.error("poll error: %r (retry %.0fs)", e, retry)
                time.sleep(retry); retry = min(retry * 2, 30.0)

    def outbox_loop(self) -> None:
        while True:
            try:
                files = sorted(OUTBOX.glob("*.json"), key=lambda p: p.stat().st_mtime)
                for path in files:
                    try:
                        job = json.loads(path.read_text("utf-8"))
                    except (json.JSONDecodeError, OSError):
                        path.unlink(missing_ok=True); continue
                    user_id, text = job.get("to_user_id", ""), job.get("text", "")
                    media_path = job.get("media_path", "")
                    if not user_id or (not text and not media_path):
                        path.unlink(missing_ok=True); continue
                    media_file = None
                    if media_path:
                        if wxmedia is None:
                            log.error("media support unavailable — drop job %s", path.name)
                            path.unlink(missing_ok=True); continue
                        media_file = Path(media_path)
                        if not media_file.is_file():
                            log.error("outbox media file missing %s — drop job", media_path)
                            path.unlink(missing_ok=True); continue
                    ct = self.ctx.get(user_id, "")
                    if not ct:
                        log.error("no context_token for %s — keep queued", user_id)
                        continue
                    try:
                        if media_file is not None:
                            self.send_media(user_id, ct, media_file, text)
                        else:
                            for chunk in chunk_text(text):
                                ilink.send_message(self.base_url, self.token,
                                                   ilink.build_text_message(user_id, ct, chunk))
                        path.unlink()
                        log.info("outbox -> %s (%d chars%s) sent", user_id, len(text),
                                 " + media" if media_file is not None else "")
                    except ValueError as e:
                        log.error("outbox job invalid, drop: %s", e)
                        path.unlink(missing_ok=True)
                    except ApiError as e:
                        if e.is_session_expired:
                            self.session_dead("send")
                        log.error("send failed, keep queued: %s", e)
                        time.sleep(5)
                    finally:
                        if user_id in self.typing_on:
                            self.typing_on.discard(user_id)
                            self.typing(user_id, False)
            except Exception as e:
                log.error("outbox loop error: %r", e)
            time.sleep(2)

    def run(self) -> None:
        for d in (INBOX, OUTBOX, FLAGS, LOGS, MEDIA):
            d.mkdir(parents=True, exist_ok=True)
        log.info("bridge starting")
        try:
            ilink.notify(self.base_url, self.token, True)
        except Exception as e:
            # 启动时的在线状态上报失败不应拖垮整个 daemon（网关偶发 RemoteDisconnected），
            # poll_loop 内已有重试，systemd 也会兜底；这里只记日志继续跑。
            log.error("startup notify failed (non-fatal): %r", e)
        threads = [threading.Thread(target=self.poll_loop, daemon=True, name="poll"),
                   threading.Thread(target=self.outbox_loop, daemon=True, name="outbox")]
        for t in threads:
            t.start()
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                ilink.notify(self.base_url, self.token, False)
            except Exception as e:
                log.error("shutdown notify failed (non-fatal): %r", e)


if __name__ == "__main__":
    Bridge().run()
