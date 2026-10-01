# -*- coding: utf-8 -*-
"""Standalone Discord trading-signal parser: text + chart-image via an
OpenAI-compatible vision LLM (GLM / DeepSeek / GPT-4o / Qwen-VL, etc).

Extracts a structured JSON instruction from a trader message:
  symbol / side / order_type / entry_prices / sl / tp_levels /
  stop_trigger_type (close_based!) / stop_timeframe / conditional /
  intent_family (OPEN / RESULT / ANALYSIS / NOISE)

Design notes (what makes this work on real trader channels):
- Text numbers are trusted first; the chart is only used to FILL GAPS or
  RECONCILE impossible values (decimal typos, wrong-side stops).
- Close-based stops ("4H close below X") are tagged so the execution layer
  can ignore intrabar wicks -- this alone changed backtest outcomes by tens of R
  on scalpers who trade wick-heavy entries.
- Conditional entries ("if price sweeps X then short") are tagged `conditional`
  and treated as resting orders with a TTL, not market fills.
- Announcements / result-brags / education posts are filtered (RESULT /
  ANALYSIS / NOISE families) -- naive regex pipelines mis-trade these badly.

Usage:
    export SIGNAL_API_KEY=...            # required
    export SIGNAL_API_BASE=https://api.<provider>.com/v1
    export SIGNAL_MODEL=glm-4.5v         # any OpenAI-compatible vision model

    python -m signal_parser --text "long BTC cmp, tp 70k, sl 4h close 66.5k"
    python -m signal_parser --text "..." --image chart.png

Credits: prompt design distilled from battle-tested private copy-trading
systems (ROBAT-style text gates + chart reconciliation); tick-level backtest
companion in ./backtest follows VeloTradeX's conservative replay design (MIT).
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path

import requests

DEFAULT_BASE = os.environ.get("SIGNAL_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
DEFAULT_MODEL = os.environ.get("SIGNAL_MODEL", "glm-4.5v")
PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "signal_vision_prompt.md"


def _api_key() -> str:
    key = os.environ.get("SIGNAL_API_KEY", "")
    if not key:
        raise SystemExit("SIGNAL_API_KEY not set")
    return key


def load_prompt() -> str:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    # Use everything after the SYSTEM PROMPT marker if present
    marker = "## SYSTEM PROMPT"
    return text.split(marker, 1)[1].split("---", 1)[0].strip() if marker in text else text


def image_to_data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    data = Path(path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# ---------------------------------------------------------------------------
# cheap local pre-gate (saves API calls on pure chatter)
# ---------------------------------------------------------------------------

ACTION_RE = re.compile(
    r"(?i)(\blong\b|\bshort\b|\blonging\b|\bshorting\b|\bbuy\b|\bsell\b|entry|zone|dca|"
    r"\btp\b|\bsl\b|stop.?loss|做多|做空|接多|接空|挂单|进场|入场|短多|短空)")
PRICE_RE = re.compile(r"(\d+\.\d+|\$\d|\d{4,})")
RESULT_RE = re.compile(
    r"(?i)(got stopped|stopped out|booked|secured|tp\s*(hit|smash)|\+\d+\.?\d*\s*r\b|"
    r"\+\d+(\.\d+)?%|战报|止盈了|止损了|保本)")


def looks_like_signal(text: str) -> bool:
    """Cheap gate: action word + a number, and not obviously a result brag."""
    if not text:
        return False
    if RESULT_RE.search(text) and not ACTION_RE.search(text):
        return False
    return bool(ACTION_RE.search(text) and PRICE_RE.search(text))


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _post(payload: dict) -> dict:
    base = os.environ.get("SIGNAL_API_BASE", DEFAULT_BASE).rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("SIGNAL_API_BASE must be https (api key travels in headers)")
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"},
        json=payload, timeout=120)
    r.raise_for_status()
    return r.json()


def _extract_json(content: str) -> dict:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def parse_signal(text: str, image_path: str | None = None,
                 model: str | None = None) -> dict:
    """Parse one trader message (optionally with chart image) into structured JSON."""
    system = load_prompt()
    model = model or os.environ.get("SIGNAL_MODEL", DEFAULT_MODEL)

    user_content: list | str = text[:6000]
    if image_path:
        data_url = image_to_data_url(image_path)
        user_content = [
            {"type": "text", "text": text[:4000]},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    }
    data = _post(payload)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    out = _extract_json(content)
    out.setdefault("should_trade", False)
    # normalize a few fields
    if out.get("side"):
        out["side"] = str(out["side"]).upper()
    if out.get("order_type"):
        out["order_type"] = str(out["order_type"]).upper()
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Parse a trading signal message")
    ap.add_argument("--text", required=True, help="message text")
    ap.add_argument("--image", help="optional chart image path")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    if not looks_like_signal(args.text):
        print(json.dumps({"should_trade": False,
                          "intent_family": "skipped_by_pregate"}, ensure_ascii=False))
    else:
        print(json.dumps(parse_signal(args.text, args.image, args.model),
                         ensure_ascii=False, indent=2))
