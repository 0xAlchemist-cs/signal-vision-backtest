# -*- coding: utf-8 -*-
"""Voice-channel event-driven backtest — single-symbol template (gold/forex CFD style).

For channels where one trader posts entry alerts AND follow-up voice actions
("take half profit", "move stop to break-even", "close everything"), plus new
signals that invalidate the previous position.

Input contract (JSON files):
  events : list of {"ts_ms": int, "kind": "signal"|"manage"|"noise", ...}
      signal -> {"side": "BUY"|"SELL", "entry": float|None, "sl": float, "tp": float}
                entry=None means market-style alert: filled at the close of the
                first COMPLETED 1m bar after the message (fill time = bar end).
      manage -> {"ev": "secure_profit"|"be_set"|"close_all"}
  klines : list of [ts_ms, open, high, low, close] 1m bars, single symbol.

Order state machine (v0.2.2 — fixes look-ahead & premature fills):
  LIMIT orders live in a PENDING state from the message until the first bar
  whose range touches the entry; only then do they become FILLED positions.
  * manage events before the fill are ignored, EXCEPT close_all which cancels
    the pending order (no PnL — an order that never filled cannot close);
  * the position is managed only from its fill bar's close onward;
  * the reference price for guards/quotes is the close of the last COMPLETED
    bar before the message (never the current forming bar's close).

Knobs (see Dataclass Config):
  r_action        "flat" | "half_be" | "be_only" | "none"   (what happens at +1R)
  sizing          "fixed" lots_fixed | "risk" risk_per_trade / stop distance
  on_new_signal   "close_and_open" | "ignore_same_dir"
  entry_timeout_min  pending limit orders older than this are dropped
  price_guard_pct    reject signals whose entry/sl/tp deviates more than this
                     from the last known price at signal time

Match rules (deliberately conservative):
  * within one 1m bar, SL is checked BEFORE the +1R trigger and TP;
  * limit fill requires low <= entry <= high (no "touched with open");
  * legs are recorded per partial close, so one trade can emit 2 legs.

Output: legs JSON (one record per close) + summary (win rate, PnL, by reason).
"""
import argparse
import json
import math
import os
from bisect import bisect_left, bisect_right
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
    # ignore_same_dir 契约 (审查八): 只保护**已成交持仓** — 同向新信号被忽略;
    # 同向**未成交挂单**(含 WAITING_FILL)始终允许换新(撤旧挂新, 无盈亏)
    entry_timeout_min: int = 1440      # 24h
    price_guard_pct: float = 0.35
    secure_profit_half: bool = True    # trader's "secure profit" -> close half + BE


PATH_MODE = os.getenv("VE_PATH_MODE", "synthetic")   # synthetic | low_first | high_first
TICK_OFFSETS = (0, 20_000, 40_000, 59_999)


def candle_to_ticks(ts_ms, o, h, l, c):
    """1m OHLC → 4 合成 tick。low_first 先探低(对 LONG 悲观/SHORT 乐观),
    high_first 相反, synthetic 按阴阳定顺序 — 同 bar TP/SL 冲突的结果
    取决于该假设, 报告需配合冲突计数解读。"""
    if PATH_MODE == "low_first":
        first, second = l, h
    elif PATH_MODE == "high_first":
        first, second = h, l
    else:
        bull = c >= o
        first = l if bull else h
        second = h if bull else l
    return [(ts_ms + TICK_OFFSETS[0], o), (ts_ms + TICK_OFFSETS[1], first),
            (ts_ms + TICK_OFFSETS[2], second), (ts_ms + TICK_OFFSETS[3], c)]


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

    def known_price(ts: int) -> float:
        """最后一个**已完成** bar 的收盘: 消息所在分钟的前一根 bar。
        消息 10:00:15 → 10:00 bar 10:01 才收盘, 不可用; 09:59 收盘可知。
        消息早于全部 bar 时退化为首根 bar 的开盘价(开盘即已知)。"""
        minute_start = (ts // 60_000) * 60_000
        j = bisect_left(starts, minute_start) - 1
        if j >= 0:
            return kl[j][4]
        return kl[0][1] if kl and starts[0] <= ts else None

    def close_part(px, oz, ts, reason):
        nonlocal equity, pos
        d = 1 if pos["side"] == "BUY" else -1
        pnl = (px - pos["entry"]) * d * oz
        equity += pnl
        pos["qty"] -= oz
        legs.append({"ts": ts, "side": pos["side"], "entry": pos["entry"],
                     "exit": px, "qty": oz, "reason": reason, "pnl": round(pnl, 2),
                     "fill_ts": pos.get("fill_ts")})
        if pos["qty"] <= 1e-9:
            pos = None

    for e in evs:
        ts = e["ts_ms"]
        # --- PENDING 挂单的事件分发 (v0.2.4: 先确认成交, 再按状态执行) ---
        if pos and pos.get("pending"):
            kind_p = e.get("kind")
            # 1) 成交推进
            if pos.get("fill_plan"):
                # 市价 WAITING_FILL: 成交时刻已预定(消息后首个合成 tick) —
                # 事件时刻到达预定 tick 才转 FILLED; 之前按未成交撤单处理
                if ts >= pos["fill_plan"][1]:
                    pos["pending"] = False
                    pos["fill_ts"] = pos["fill_plan"][1]
                    pos["lw"] = pos["fill_ts"]
            else:
                # 限价挂单: 只扫**事件时刻之前已完成**的 bar (bar 末 <= ts) —
                # 这些 bar 的触及确定发生在事件之前 → 真实成交。
                # 事件所在分钟及之后的 bar 不参与(撤单与触及先后不可分辨时,
                # 保守按撤单处理, 由下方 close_all 分支执行)。
                n_done = bisect_right(starts, ts - 59_999)   # bar 末 <= ts 的数量
                n = pos["entry_search_from"]
                while n < n_done:
                    b = kl[n]
                    if b[3] <= pos["entry"] <= b[2]:
                        # 确定成交 → 转 FILLED, 管理从该 bar 收盘之后开始
                        pos["pending"] = False
                        pos["fill_ts"] = starts[n] + 59_999
                        pos["lw"] = pos["fill_ts"]
                        pos["entry_search_from"] = n + 1
                        break
                    n += 1
                pos["entry_search_from"] = max(pos["entry_search_from"], n_done)
            # 2) 按真实状态执行事件
            if pos.get("pending"):
                # 仍 PENDING: close_all/取消 → 撤挂单(无盈亏);
                # BE/部分止盈无仓位可作用 → 忽略;
                # 新 signal → 落入下方 signal 分支(撤旧挂单 + 开新仓)
                if kind_p == "manage":
                    if e.get("ev") == "close_all":
                        pos = None
                    continue
                if kind_p != "signal":
                    continue
            # FILLED: 落入下方 manage/signal 分支按真实持仓处理

        # --- 推进 FILLED 持仓: tick 级 SL / TP / 1R 判定 (v0.2.3: 同 bar
        # TP/SL 冲突按路径模式解析, 不再固定先查止损) ---
        if pos and not pos.get("pending"):
            d = 1 if pos["side"] == "BUY" else -1
            # lw 常在分钟中间(如 30s 的 BE 事件之后): 起点必须 floor 回 lw
            # 所在分钟 bar, 否则同 bar 剩余 tick(如 40s 的 TP)被永久跳过。
            # 时间戳是真实纪元毫秒 — lw//60_000 是"纪元分钟数"而非数组下标
            # (只有从 0 开始的合成时间戳恰好两者相等), 必须 bisect 得下标;
            # 已处理 tick 由下方 tms <= lw 过滤
            i0 = bisect_left(starts, (pos["lw"] // 60_000) * 60_000)
            i1 = bisect_left(starts, ts)
            for n in range(i0, min(i1, len(kl))):
                b = kl[n]
                done = False
                for tms, price in candle_to_ticks(int(b[0]), float(b[1]),
                                                  float(b[2]), float(b[3]), float(b[4])):
                    # 严格时序: tick 超过事件时刻(ts)后不得再触发 — 否则
                    # 事件之后的未来 tick(如 40s 的 TP)会抢先于事件(30s 的
                    # 全平)执行 (v0.2.4 修复)
                    if tms > ts:
                        break
                    if tms <= pos["lw"]:
                        continue
                    if (price <= pos["sl"]) if d == 1 else (price >= pos["sl"]):
                        close_part(pos["sl"], pos["qty"], tms,
                                   "BE" if pos.get("be") else "SL")
                        done = True
                        break
                    if pos["tp"] and ((price >= pos["tp"]) if d == 1 else (price <= pos["tp"])):
                        close_part(pos["tp"], pos["qty"], tms, "TP")
                        done = True
                        break
                    if not pos.get("r1_done") and cfg.r_action != "none":
                        r1 = pos["entry"] + d * pos["unit"]
                        if (price >= r1) if d == 1 else (price <= r1):
                            if cfg.r_action == "flat":
                                close_part(r1, pos["qty"], tms, "1R flat")
                                done = True
                                break
                            if cfg.r_action == "half_be":
                                close_part(r1, round(pos["qty"] / 2, 2), tms, "1R half")
                                if pos:
                                    pos["sl"] = pos["entry"]
                                    pos["be"] = True
                                    pos["r1_done"] = True
                            else:  # be_only
                                pos["sl"] = pos["entry"]
                                pos["be"] = True
                                pos["r1_done"] = True
                if done:
                    break
            if pos:
                pos["lw"] = ts

        # --- 交易员喊话 (只作用于已成交仓位, 且喊话时刻在成交之后) ---
        kind = e.get("kind")
        if kind == "manage" and pos and not pos.get("pending") \
                and ts > pos.get("fill_ts", 0):
            ev = e.get("ev")
            px = known_price(ts)
            if px is None:
                continue
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
        # --- 新信号 ---
        elif kind == "signal":
            side = e.get("side")
            if side not in ("BUY", "SELL"):
                continue
            d = 1 if side == "BUY" else -1
            entry, sl, tp = e.get("entry"), e.get("sl"), e.get("tp")
            if sl is None or tp is None:
                continue
            i = bisect_left(starts, ts)
            # 参考价 = 消息所在分钟**之前**最后一根已完成 bar 的收盘 (v0.2.2:
            # 旧实现取下一根 bar 收盘 = 未来价)
            ref = known_price(ts)
            if ref is None:
                continue
            if entry is not None and (entry <= 0 or abs(entry - ref) / ref > cfg.price_guard_pct):
                continue
            # 成交: 限价 = 之后首根触及 bar (PENDING); 市价型 = **消息后首个
            # 合成 tick** 的价格(消息所在 bar 起, tms >= ts 过滤) — 与主引擎
            # "消息后首个已知价" 语义一致, 整点/分钟中间/分钟末均正确
            fill = None
            fill_bar_ts = None
            fill_tms = None
            pending = False
            if entry is not None:
                for n in range(i, min(i + cfg.entry_timeout_min, len(kl))):
                    b = kl[n]
                    if b[3] <= entry <= b[2]:
                        fill = entry
                        fill_bar_ts = b[0]
                        fill_tms = max(ts, b[0])   # 限价触及不早于消息
                        pending = True       # 触及可能发生在 bar 内任意时刻
                        break
            else:
                for n in range(max(0, i - 1), min(i + 1, len(kl))):
                    b = kl[n]
                    for tms, price in candle_to_ticks(int(b[0]), float(b[1]),
                                                      float(b[2]), float(b[3]), float(b[4])):
                        if tms >= ts:
                            fill = price
                            fill_bar_ts = b[0]
                            fill_tms = tms
                            break
                    if fill is not None:
                        break
            if fill is None:
                continue
            if any(v <= 0 or abs(v - ref) / ref > cfg.price_guard_pct for v in (fill, sl, tp)):
                continue
            unit = abs(fill - sl)
            if unit <= 0 or (sl - fill) * d >= 0 or (fill - tp) * d >= 0:
                continue
            # 新信号 vs 现有仓位(含 PENDING 挂单: 换新 = 撤旧单, 无盈亏)
            if pos:
                if cfg.on_new_signal == "ignore_same_dir" and pos["side"] == side \
                        and not pos.get("pending"):
                    continue
                if pos.get("pending"):
                    pos = None               # 撤旧挂单
                else:
                    close_part(ref, pos["qty"], ts, "signal_flip")
            if cfg.sizing == "risk":
                lots = min(cfg.max_lots, max(cfg.min_lots,
                                                math.floor(cfg.risk_per_trade / unit) / 100.0))
            else:
                lots = cfg.lots_fixed
            # 成交状态 (审查七): 市价单的预定成交 tick 可能**晚于**后续事件 —
            # 消息时刻 ≠ 成交时刻。成交 tick 在未来 → 先 WAITING_FILL(pending,
            # fill_plan 记预定成交), 成交前的新信号/close_all = 撤单(无盈亏),
            # 与限价挂单同一状态机; 旧实现直接建仓, 成交前的反向信号会
            # signal_flip 出一笔不存在的盈亏
            fill_plan = None
            if not pending and fill_tms > ts:
                fill_plan = (fill, fill_tms)
            fill_ts = None if (pending or fill_plan) else max(fill_tms, ts)
            pos = {"side": side, "entry": fill, "qty": round(lots * cfg.oz_per_lot, 2),
                   "sl": sl, "tp": tp, "unit": unit, "open_ts": ts,
                   "lw": fill_ts or 0,
                   "fill_ts": fill_ts,
                   "pending": pending or fill_plan is not None,
                   "fill_plan": fill_plan,
                   "entry_search_from": i if pending else 0,
                   "be": False, "r1_done": False}

    if pos:
        if pos.get("pending"):
            if pos.get("fill_plan"):
                # 市价 WAITING_FILL: 预定成交 tick 必在数据内 → 到点成交
                pos["pending"] = False
                pos["fill_ts"] = pos["fill_plan"][1]
                pos["lw"] = pos["fill_ts"]
            else:
                # 事件流结束后, PENDING 挂单仍可能在其后被触及 → 扫描到数据末尾,
                # 成交则转 FILLED 并继续走查 SL/TP, 未成交则撤单(无盈亏)
                n = pos["entry_search_from"]
                while n < len(kl):
                    b = kl[n]
                    if b[3] <= pos["entry"] <= b[2]:
                        pos["pending"] = False
                        pos["fill_ts"] = starts[n] + 59_999
                        pos["lw"] = pos["fill_ts"]
                        break
                    n += 1
        if pos and not pos.get("pending"):
            d = 1 if pos["side"] == "BUY" else -1
            # 尾部走查同样 floor 回 lw 所在分钟(与事件循环内一致) —
            # 必须 bisect 得真实下标(时间戳是纪元毫秒, 不是下标)
            i0 = bisect_left(starts, (pos["lw"] // 60_000) * 60_000)
            done = False
            for n in range(i0, len(kl)):
                b = kl[n]
                # 尾部走查同样 tick 化: 同 bar TP/SL 冲突按路径模式解析
                for tms, price in candle_to_ticks(int(b[0]), float(b[1]),
                                                  float(b[2]), float(b[3]), float(b[4])):
                    if tms <= pos["lw"]:
                        continue
                    if (price <= pos["sl"]) if d == 1 else (price >= pos["sl"]):
                        close_part(pos["sl"], pos["qty"], tms,
                                   "BE" if pos.get("be") else "SL")
                        done = True
                        break
                    if pos["tp"] and ((price >= pos["tp"]) if d == 1 else (price <= pos["tp"])):
                        close_part(pos["tp"], pos["qty"], tms, "TP")
                        done = True
                        break
                if done:
                    break
            if not done:
                # 数据截止平仓: 价格用最后一根已完成 bar 的收盘, 时刻也是
                # 该 bar 的**收盘**(开盘+59_999) — 旧实现记 bar 开盘, 极端
                # 情况(分钟中间成交)会出现出场早于开仓的时间倒置
                close_part(kl[-1][4], pos["qty"], kl[-1][0] + 59_999, "end_of_data")
        else:
            pos = None                # 到期未成交, 撤单

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
