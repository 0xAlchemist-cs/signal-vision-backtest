# -*- coding: utf-8 -*-
"""Missed-signal detector: flags pure-text messages that LOOK like trade
entries (action word + price levels) but were NOT captured by the parser.

This is the practical fix for the "signal loss" problem observed with
Chinese-language traders whose stop/TP levels are embedded in prose.
"""
import json
import re
from pathlib import Path

# Chinese + English action patterns
ENTRY_LIKE = re.compile(
    r"(进场|入场|开多|开空|做多|做空|短多|短空|接多|接空|挂单|现价|已进|"
    r"\blong\b|\bshort\b|\bbuy\b|\bsell\b|\bentry\b)")
LEVEL_LIKE = re.compile(
    r"(止损|止盈|防守|\bsl\b|\btp\b|TP\d|SL|stop\s|target)")
NUM = re.compile(r"\d{3,}(?:\.\d+)?")


def scan_missed(jsonl_path: str, signals_path: str) -> dict:
    """Scan a message JSONL for entry-like pure-text messages that the
    parser did NOT capture as signals. Returns a summary dict."""
    sig_ids = set()
    if Path(signals_path).exists():
        for s in json.loads(Path(signals_path).read_text(encoding="utf-8")):
            sig_ids.add(s.get("id"))

    missed = []
    total_text = 0
    for line in Path(jsonl_path).open(encoding="utf-8"):
        if not line.strip().endswith("}"):
            continue
        m = json.loads(line)
        text = (m.get("content") or "").strip()
        if not text or m.get("attachments"):
            continue  # only pure-text (no image) messages
        total_text += 1
        if m["id"] in sig_ids:
            continue  # already parsed
        # entry-like: action word + price-like number
        if ENTRY_LIKE.search(text) and NUM.search(text):
            missed.append({"id": m["id"], "ts": m.get("ts"),
                           "text": text[:200]})

    return {"total_text_msgs": total_text,
            "missed_candidates": len(missed),
            "missed": missed}


if __name__ == "__main__":
    import sys
    jsonl = sys.argv[1] if len(sys.argv) > 1 else "data/channel.jsonl"
    sigs = sys.argv[2] if len(sys.argv) > 2 else "work/signals.json"
    result = scan_missed(jsonl, sigs)
    print(json.dumps({k: v for k, v in result.items() if k != "missed"},
                     ensure_ascii=False, indent=2))
    if result["missed"]:
        out = Path(jsonl).parent / "missed_signals.json"
        out.write_text(json.dumps(result["missed"], ensure_ascii=False),
                       encoding="utf-8")
        print(f"→ {len(result['missed'])} 条疑似漏检信号已存 {out}")
