#!/usr/bin/env python3
"""WeChat bridge fast lane v3: single front door + pluggable fast brain.

Claims EVERY inbox message within ~0.3s (atomic rename inbox -> processing),
so no other processor can ever race it. Then:
- simple chat / general-knowledge questions -> answered directly by the
  configured fast model, file to done/;
- everything else (tasks, commands, routing, pinned chats, media, anything
  needing tools / personal memory / realtime data) -> moved to escalated/
  for the Muse triage worker; complex text tasks get one instant ack first.

Brain configuration (fastlane_config.json in this directory, chmod 600):
    {
      "provider": "openai",
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "api_key": "sk-...",
      "model": "glm-4.7-flash",
      "fallback_model": ""
    }
Any OpenAI-compatible endpoint works (Zhipu, SiliconFlow, Bailian, Ark,
DeepSeek, ...). Without a config file the fast lane stays inert: every
message is escalated to the main agent until a fast model is configured.

BRIDGE_DIR: env override, else the directory containing this script
(never Path.home(): under systemd HOME is /root).
"""
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

BRIDGE = Path(os.environ.get("BRIDGE_DIR", Path(__file__).resolve().parent))
INBOX = BRIDGE / "inbox"
PROCESSING = BRIDGE / "processing"
DONE = BRIDGE / "done"
OUTBOX = BRIDGE / "outbox"
ESCALATED = BRIDGE / "escalated"
STATE = BRIDGE / "state"
CONFIG_PATH = BRIDGE / "fastlane_config.json"
ACKED_PATH = STATE / "fastlane_acked.json"
HISTORY_PATH = STATE / "fastlane_history.json"
LOG_PATH = BRIDGE / "logs" / "fastlane.log"

API_TIMEOUT = 25
ESCALATE_TOKEN = "ESCALATE"

SYSTEM_PROMPT = """角色与任务
你是 Muse，芒果的个人 AI 助手（Muse Spark，Meta 出品），正通过微信和芒果本人聊天。你的任务是快速、自然地回答他的日常问题。

工作原则与规则
- 只用你确知的通用知识回答；凡是需要查询实时信息（天气、股价、新闻、赛程）、调用工具、读取他的文件/记忆/日程、执行操作（生成文件、发消息、做视频、设置提醒等）的问题，一律只输出 ESCALATE 这一个词，不要尝试回答。
- 涉及他本人情况而你不确定的（他的计划、账号、数据），一律只输出 ESCALATE。
- 不编造数字、日期和事实；拿不准就输出 ESCALATE。
- 纯文本输出，不用任何 markdown 符号（不用 **、#、- 列表符号），中文，简短自然，像微信聊天，一般不超过 200 字。
- 不要说自己是 Google 的模型；问你是谁就说你是 Muse，芒果的个人 AI 助手。

输出要求
- 能答：直接给答案，一段话说清。
- 不能答：只输出 ESCALATE（不要附带任何解释）。

示例
问：水的化学式是什么？
答：H2O，两个氢原子加一个氧原子。
问：今天乌鲁木齐天气怎么样？
答：ESCALATE"""

TASK_KEYWORDS = [
    "帮我", "帮忙", "查一下", "查查", "搜索", "搜一下", "下载", "生成", "制作",
    "做一个", "做个", "写一", "画一", "提醒我", "定时", "发给", "转发", "打开",
    "订", "买", "报告", "总结一下", "整理一下", "翻译这", "天气", "股价", "股票",
    "新闻", "汇率", "你记得", "上次", "刚才", "我的账号", "我的文件", "怎么样了",
    "进度", "视频", "图片", "照片", "录音", "文件发", "多少钱", "价格",
]
ROUTE_RE = re.compile(r"^.{1,20}[:：]")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + f" [fastlane] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), "utf-8")


# --- configuration ---------------------------------------------------------

_cfg_cache = {"mtime": "unset", "cfg": None}


def load_config():
    """Re-read fastlane_config.json when it changes (agents can rewrite it
    live; no restart needed). Without one, everything escalates."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _cfg_cache["mtime"] != mtime:
        cfg = load_json(CONFIG_PATH, None) if mtime is not None else None
        if not cfg or cfg.get("provider") != "openai":
            cfg = {"provider": "none", "model": ""}
        _cfg_cache.update(mtime=mtime, cfg=cfg)
        log(f"config: provider={cfg['provider']} model={cfg.get('model')}")
    return _cfg_cache["cfg"]


def brain_id(cfg):
    return f"{cfg.get('provider')}:{cfg.get('model')}"


# --- keep-alive HTTP -------------------------------------------------------
# One persistent connection per host. Through the egress proxy (CONNECT
# tunnel) when https_proxy is set and the host is not in no_proxy; direct
# otherwise. Any failure closes the connection; callers retry once.

_CONNS = {}


def _connect(scheme, host, port):
    key = (scheme, host, port)
    conn = _CONNS.get(key)
    if conn is not None:
        return conn
    no_proxy = (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "")
    proxied = (
        scheme == "https"
        and (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
        and host not in no_proxy
    )
    if proxied:
        p = urlparse(os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
        conn = http.client.HTTPSConnection(p.hostname, p.port or 3128, timeout=API_TIMEOUT)
        conn.set_tunnel(host, port)
    elif scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=API_TIMEOUT)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=API_TIMEOUT)
    _CONNS[key] = conn
    return conn


def _drop_conn(scheme, host, port):
    conn = _CONNS.pop((scheme, host, port), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _post_json(url, headers, body):
    """POST via the keep-alive pool; returns parsed JSON. Retries once on a
    fresh connection, then raises."""
    u = urlparse(url)
    scheme = u.scheme
    host = u.hostname
    port = u.port or (443 if scheme == "https" else 80)
    path = u.path + (("?" + u.query) if u.query else "")
    last_err = None
    for _ in range(2):
        conn = _connect(scheme, host, port)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {raw[:200]!r}")
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last_err = e
            _drop_conn(scheme, host, port)
    raise last_err


# --- OpenAI-compatible provider ---------------------------------------------

def _openai_body(cfg, history, text):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history[-6:]:
        messages.append({"role": "user", "content": turn["q"]})
        messages.append({"role": "assistant", "content": turn["a"]})
    messages.append({"role": "user", "content": text})
    payload = {"model": cfg["model"], "messages": messages,
               "max_tokens": 1024, "temperature": 0.7}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _call_openai(cfg, history, text, model_override=None):
    cfg = dict(cfg)
    if model_override:
        cfg["model"] = model_override
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg['api_key']}"}
    data = _post_json(url, headers, _openai_body(cfg, history, text))
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        return ESCALATE_TOKEN
    return (choice["message"].get("content") or "").strip()


# --- brain dispatch ------------------------------------------------------------

def _norm(q):
    return re.sub(r"[\s，。？！、：；,.?!:;]", "", q or "")


def ask_brain(cfg, history, text):
    """Returns (answer, cached). Raises if every model path failed."""
    nq = _norm(text)
    for turn in reversed(history):
        if _norm(turn.get("q")) == nq and turn.get("a") and turn.get("src") == brain_id(cfg):
            return turn["a"], True
    if cfg["provider"] == "openai":
        try:
            return _call_openai(cfg, history, text), False
        except Exception:
            fb = cfg.get("fallback_model")
            if fb and fb != cfg["model"]:
                return _call_openai(cfg, history, text, model_override=fb), False
            raise
    raise RuntimeError("fast model not configured: create fastlane_config.json")


# --- front door ------------------------------------------------------------------

def write_outbox(user_id, text):
    OUTBOX.mkdir(parents=True, exist_ok=True)
    name = f"fastlane-{uuid.uuid4().hex[:12]}.json"
    save_json(OUTBOX / name, {"to_user_id": user_id, "text": text})


def is_complex_heuristic(text):
    if len(text) > 120:
        return True
    return any(k in text for k in TASK_KEYWORDS)


def pinned():
    sess = load_json(STATE / "session.json", {})
    return sess.get("pinned")


def process_file(path, acked, history, cfg):
    try:
        msg = json.loads(path.read_text("utf-8"))
    except Exception:
        return "skipped"
    msg_id = msg.get("id") or path.stem
    user_id = msg.get("from_user_id", "")
    kind = msg.get("kind", "text")
    text = (msg.get("text") or "").strip()

    claimed = PROCESSING / path.name
    try:
        PROCESSING.mkdir(parents=True, exist_ok=True)
        path.rename(claimed)
    except OSError:
        return "skipped"

    def escalate(ack=False):
        if ack and msg_id not in acked:
            write_outbox(user_id, "收到，这件事我来办，稍等一下。")
            acked[msg_id] = int(time.time())
            save_json(ACKED_PATH, acked)
        try:
            ESCALATED.mkdir(parents=True, exist_ok=True)
            claimed.rename(ESCALATED / path.name)
        except OSError:
            pass
        log(f"{msg_id}: escalated (ack={ack})")
        return "escalated"

    if pinned():
        return escalate()
    if kind not in ("text", "voice"):
        return escalate()
    if not text or text.startswith("/") or ROUTE_RE.match(text):
        return escalate()
    if is_complex_heuristic(text):
        return escalate(ack=True)

    t0 = time.time()
    cached = False
    try:
        answer, cached = ask_brain(cfg, history, text)
    except Exception as e:
        log(f"brain call failed for {msg_id}: {e!r}; escalating")
        return escalate()
    dt = time.time() - t0
    if not answer or answer.startswith(ESCALATE_TOKEN):
        log(f"{msg_id}: brain escalated ({dt:.1f}s)")
        return escalate(ack=True)
    answer = answer.replace("**", "").replace("##", "").strip()
    write_outbox(user_id, answer)
    history.append({"q": text, "a": answer, "ts": int(time.time()), "src": brain_id(cfg)})
    save_json(HISTORY_PATH, history[-20:])
    try:
        DONE.mkdir(parents=True, exist_ok=True)
        claimed.rename(DONE / path.name)
    except OSError:
        pass
    log(f"{msg_id}: answered in {dt:.1f}s ({len(answer)} chars, cached={cached})")
    return "answered"


def scan_once():
    cfg = load_config()
    acked = load_json(ACKED_PATH, {})
    history = load_json(HISTORY_PATH, [])
    cutoff = int(time.time()) - 86400
    acked = {k: v for k, v in acked.items() if v >= cutoff}
    results = {}
    for path in sorted(INBOX.glob("*.json")):
        r = process_file(path, acked, history, cfg)
        results[r] = results.get(r, 0) + 1
    return results


def main():
    if "--once" in sys.argv:
        print(scan_once())
        return
    log(f"fastlane v3 starting (bridge={BRIDGE})")
    while True:
        try:
            scan_once()
        except Exception as e:
            log(f"scan error: {e!r}")
        time.sleep(0.3)


if __name__ == "__main__":
    main()
