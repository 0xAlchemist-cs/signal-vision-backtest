# -*- coding: utf-8 -*-
"""Voice-channel event-driven backtest — single-symbol template (gold/forex CFD style).

For channels where one trader posts entry alerts AND follow-up voice actions
("take half profit", "move stop to break-even", "close everything"), plus new
signals that invalidate the previous position.

Input contract (JSON files):
  events : list of {"ts_ms": int, "kind": "signal"|"manage"|"noise", ...}
      signal -> {"side": "BUY"|"SELL", "entry": float|None, "sl": float, "tp": float}
                entry=None means market-style alert (only SL/TP given):
                fill at the close of the first 1m bar after the message.
      manage -> {"ev": "secure_profit"|"be_set"|"close_all"}
  klines : list of [ts_ms, open, high, low, close] 1m bars, single symbol.

Knobs (see Dataclass Config):
  r_action        what happens when price reaches +1R:
                  "flat" (close all) | "half_be" (close half + stop to entry)
                  | "be_only" (stop to entry) | "none"
  sizing          "fixed" lots_fixed | "risk" risk_per_trade / stop distance,
                  clamped to [min_lots, max_lots]
  on_new_signal   "close_and_open" (any new signal flattens the old pos)
                  | "ignore_same_dir" (only opposite side flattens)
  entry_timeout_min  pending limit orders older than this are dropped
  price_guard_pct    reject signals whose entry/sl/tp deviates more than this
                     from the bar close at signal time (parse-error guard)

Match rules (deliberately conservative):
  * within one 1m bar, SL is checked BEFORE the +1R trigger and TP;
  * limit fill requires low <= entry <= high (no "touched with open");
  * legs are recorded per partial close, so one trade can emit 2 legs.

Output: legs JSON (one record per close) + summary (win rate, PnL, by reason).
"""
import argparse
import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    initial_capital: float = 800.0
    oz_per_lot: float = 100.0          # XAUUSD: 1 lot = 100 oz
    lots_fixed: float = 0.01           # used when sizing == "fixed"
    sizing: str = "fixed"              # "fixed" | "risk"
    risk_per_trade: float = 50.0       # used when sizing == "risk"
    min_lots: float = 0.01
    max_lots: float = 0.02
    r_action: str = "half_be"          # "flat" | "half_be" | "be_only" | "none"
    on_new_signal: str = "close_and_open"  # "close_and_open" | "ignore_same_dir"
    entry_timeout_min: int = 1440      # 24h
    price_guard_pct: float = 0.35
    secure_profit_half: bool = True    # trader's "secure profit" -> close half + BE


def load(events_path: Path, klines_path: Path):
    kl = [(r[0], r[1], r[2], r[3], r[4]) for r in json.loads(klines_path.read_text(encoding="utf-8"))]
    evs = json.loads(events_path.read_text(encoding="utf-8"))
    evs.sort(key=lambda e: e["ts_ms"])
    return kl, evs


def run(kl, evs, cfg: Config):
    starts = [k[0] for k in kl]
    equity = cfg.initial_capital
    legs = []
    pos = None

    def close_part(px, oz, ts, reason):
        nonlocal equity, pos
        d = 1 if pos["side"] == "BUY" else -1
        pnl = (px - pos["entry"]) * d * oz
        equity += pnl
        pos["qty"] -= oz
        legs.append({"ts": ts, "side": pos["side"], "entry": pos["entry"],
                     "exit": px, "qty": oz, "reason": reason, "pnl": round(pnl, 2)})
        if pos["qty"] <= 1e-9:
            pos = None

    for e in evs:
        ts = e["ts_ms"]
        # --- advance bars up to this event: SL / TP / +1R triggers ---
        if pos:
            d = 1 if pos["side"] == "BUY" else -1
            i0 = bisect_left(starts, pos["lw"])
            i1 = bisect_left(starts, ts)
            for n in range(i0, min(i1, len(kl))):
                b = kl[n]
                if (b[3] <= pos["sl"]) if d == 1 else (b[2] >= pos["sl"]):
                    close_part(pos["sl"], pos["qty"], b[0],
                               "BE" if pos.get("be") else "SL")
                    break
                if pos["tp"] and ((b[2] >= pos["tp"]) if d == 1 else (b[3] <= pos["tp"])):
                    close_part(pos["tp"], pos["qty"], b[0], "TP")
                    break
                if not pos.get("r1_done") and cfg.r_action != "none":
                    r1 = pos["entry"] + d * pos["unit"]
                    if (b[2] >= r1) if d == 1 else (b[3] <= r1):
                        if cfg.r_action == "flat":
                            close_part(r1, pos["qty"], b[0], "1R flat")
                            break
                        if cfg.r_action == "half_be":
                            close_part(r1, round(pos["qty"] / 2, 2), b[0], "1R half")
                            if pos:
                                pos["sl"] = pos["entry"]
                                pos["be"] = True
                                pos["r1_done"] = True
                        else:  # be_only
                            pos["sl"] = pos["entry"]
                            pos["be"] = True
                            pos["r1_done"] = True
            if pos:
                pos["lw"] = ts
        # --- trader voice actions ---
        kind = e.get("kind")
        if kind == "manage" and pos:
            ev, i = e.get("ev"), bisect_left(starts, ts)
            if i >= len(kl):
                continue
            px = kl[i][4]
            if ev == "close_all":
                close_part(px, pos["qty"], ts, "close_all_call")
            elif ev == "secure_profit" and cfg.secure_profit_half:
                close_part(px, round(pos["qty"] / 2, 2), ts, "secure_profit")
                if pos and not pos.get("be"):
                    pos["sl"] = pos["entry"]
                    pos["be"] = True
                if pos:
                    pos["r1_done"] = True
            elif ev == "be_set" and not pos.get("be"):
                pos["sl"] = pos["entry"]
                pos["be"] = True
        # --- new signals ---
        elif kind == "signal":
            side = e.get("side")
            if side not in ("BUY", "SELL"):
                continue
            d = 1 if side == "BUY" else -1
            entry, sl, tp = e.get("entry"), e.get("sl"), e.get("tp")
            if sl is None or tp is None:
                continue
            i = bisect_left(starts, ts)
            if i >= len(kl):
                continue
            ref = kl[i][4]
            if entry is not None and (entry <= 0 or abs(entry - ref) / ref > cfg.price_guard_pct):
                continue
            # fill: limit touch within window, else first-bar close for market-style alerts
            fill = None
            if entry is not None:
                for n in range(i, min(i + cfg.entry_timeout_min, len(kl))):
                    b = kl[n]
                    if b[3] <= entry <= b[2]:
                        fill = entry
                        break
            else:
                fill = ref
            if fill is None:
                continue
            if any(v <= 0 or abs(v - ref) / ref > cfg.price_guard_pct for v in (fill, sl, tp)):
                continue
            unit = abs(fill - sl)
            if unit <= 0 or (sl - fill) * d >= 0 or (fill - tp) * d >= 0:
                continue
            # new signal vs open position
            if pos:
                if cfg.on_new_signal == "ignore_same_dir" and pos["side"] == side:
                    continue
                close_part(kl[i][4], pos["qty"], ts, "signal_flip")
            if cfg.sizing == "risk":
                lots = min(cfg.max_lots, max(cfg.min_lots,
                                                math.floor(cfg.risk_per_trade / unit) / 100.0))
            else:
                lots = cfg.lots_fixed
            pos = {"side": side, "entry": fill, "qty": round(lots * cfg.oz_per_lot, 2),
                   "sl": sl, "tp": tp, "unit": unit, "open_ts": ts, "lw": ts,
                   "be": False, "r1_done": False}

    if pos:
        close_part(kl[-1][4], pos["qty"], kl[-1][0], "end_of_data")

    wins = sum(1 for t in legs if t["pnl"] > 0)
    total = round(sum(t["pnl"] for t in legs), 2)
    by_reason = {}
    for t in legs:
        r = by_reason.setdefault(t["reason"], {"legs": 0, "pnl": 0.0})
        r["legs"] += 1
        r["pnl"] = round(r["pnl"] + t["pnl"], 2)
    peak, mdd = cfg.initial_capital, 0.0
    eq = cfg.initial_capital
    for t in legs:
        eq += t["pnl"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"legs": legs, "summary": {
        "legs": len(legs), "win_rate": round(wins / len(legs) * 100, 1) if legs else 0,
        "total_pnl": total, "final_equity": round(equity, 2),
        "max_drawdown": round(mdd, 2), "by_reason": by_reason}}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--events", required=True)
    ap.add_argument("--klines", required=True)
    ap.add_argument("--out", default="voice_event_result.json")
    ap.add_argument("--capital", type=float, default=800.0)
    ap.add_argument("--oz-per-lot", type=float, default=100.0)
    ap.add_argument("--sizing", choices=["fixed", "risk"], default="fixed")
    ap.add_argument("--lots", type=float, default=0.02)
    ap.add_argument("--risk", type=float, default=50.0)
    ap.add_argument("--min-lots", type=float, default=0.01)
    ap.add_argument("--max-lots", type=float, default=0.02)
    ap.add_argument("--r-action", choices=["flat", "half_be", "be_only", "none"],
                    default="half_be")
    ap.add_argument("--on-new-signal", choices=["close_and_open", "ignore_same_dir"],
                    default="close_and_open")
    ap.add_argument("--entry-timeout-min", type=int, default=1440)
    args = ap.parse_args()

    cfg = Config(initial_capital=args.capital, oz_per_lot=args.oz_per_lot,
                 lots_fixed=args.lots, sizing=args.sizing, risk_per_trade=args.risk,
                 min_lots=args.min_lots, max_lots=args.max_lots, r_action=args.r_action,
                 on_new_signal=args.on_new_signal, entry_timeout_min=args.entry_timeout_min)
    kl, evs = load(Path(args.events), Path(args.klines))
    res = run(kl, evs, cfg)
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    s = res["summary"]
    det = " | ".join(f"{k}:{v['legs']} {v['pnl']:+.0f}"
                     for k, v in sorted(s["by_reason"].items(), key=lambda x: -x[1]["legs"]))
    print(f"legs {s['legs']} | wr {s['win_rate']}% | pnl {s['total_pnl']:+.1f} | "
          f"equity {s['final_equity']:.1f} | mdd {s['max_drawdown']:+.1f}")
    print(det)


if __name__ == "__main__":
    main()
