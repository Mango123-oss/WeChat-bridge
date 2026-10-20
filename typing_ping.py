#!/usr/bin/env python3
"""刷新微信的"正在输入"指示器。用法: typing_ping.py <user_id> start|stop"""
import json, os, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import ilink


def load(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return d


def save(p, o):
    with open(p, "w") as f:
        json.dump(o, f)


def main():
    uid, action = sys.argv[1], sys.argv[2]
    start = action == "start"
    creds = load(f"{BASE}/state/credentials.json", {})
    if not creds:
        return
    tickets = load(f"{BASE}/state/typing_tickets.json", {})
    ticket = tickets.get(uid)
    if not ticket:
        ct = load(f"{BASE}/state/context.json", {}).get(uid, "")
        if not ct:
            return
        for _ in range(3):
            try:
                cfg = ilink.get_config(creds["base_url"], creds["token"], uid, ct)
                ticket = cfg.get("typing_ticket") or ""
                break
            except Exception:
                time.sleep(2)
        tickets[uid] = ticket
        save(f"{BASE}/state/typing_tickets.json", tickets)
    if ticket:
        try:
            ilink.send_typing(creds["base_url"], creds["token"], uid, ticket, start)
        except Exception as e:
            print(f"typing {action} failed: {e}")


main()
