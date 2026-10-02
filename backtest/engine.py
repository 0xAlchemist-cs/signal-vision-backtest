# -*- coding: utf-8 -*-
"""Signal Vision Backtest engine: event-driven, second-level timeline replay.

Rules modelled (R-ladder, the default):
  - after entry, at +1R: close 40% of the position and move stop to breakeven;
  - at +2R: close another 40%;
  - the remaining 20% only exits on the trader's own follow-up calls
    (partial TP / full close / stop move); with no calls it rides the BE stop,
    hard-capped at 30 days.
Alternative `tp_ladder` rule: replay the trader's own published TP levels,
closing 1/N at each level (SVB_RULE=tp_ladder).

Inputs (in WORK dir): signals.json + manages.json (see signal_parser package)
Data: 1m klines expanded to 4 ticks (O / adverse extreme / favorable extreme / C),
messages inserted at their exact millisecond between ticks; fills = first tick
after the message. Market/stop slippage 0.05% per side, taker 0.025% per side,
funding rates applied per holding period.

Usage: prefetch | sim | report
"""
import bisect
import concurrent.futures as cf  # noqa: F401 (kept for the parse phase)
import gc
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# working dir for caches/signals/trades; override with SVB_WORK env var
WORK = Path(os.environ.get("SVB_WORK", HERE / "work"))
WORK.mkdir(parents=True, exist_ok=True)


def _load_env() -> None:
    """optional: load a .env inside the work dir (keys, overrides)"""
    env = WORK / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


class _NoopDB:
    """fingerprint-store hook -- plug your own persistence here to enable the
    format-whitelist fast path; without it every candidate goes to the LLM."""

    def init_db(self):
        pass


database = _NoopDB()

RISK_USDT = float(os.getenv("SVB_RISK_USDT", "25.0"))
# R-ladder exit params (defaults: +1R close 40% + BE, +2R close 40%)
R1_LVL = float(os.getenv("GR1_LVL", "1.0"))
R1_FRAC = float(os.getenv("GR1_FRAC", "0.40"))
R2_LVL = float(os.getenv("GR2_LVL", "2.0"))
R2_FRAC = float(os.getenv("GR2_FRAC", "0.40"))
FEE_RATE = 0.00025          # taker per side
SLIP = float(os.getenv("SVB_SLIP", "0.0005"))   # market/stop slippage per side
ENTRY_TIMEOUT_H = 48
POSITION_CAP_D = 30
DEDUPE_H = 6


TICK_OFFSETS = (0, 20_000, 40_000, 59_999)
_KLINE_HOSTS = {"api.bybit.com", "api.binance.com"}


def _http_get(url: str, timeout: int = 15):
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme != "https" or p.hostname not in _KLINE_HOSTS:
        raise ValueError(f"非法行情地址: {url}")
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(float(e.headers.get("Retry-After", "2")) + 0.5)
                continue
            raise
    raise RuntimeError(f"重试耗尽: {url}")


def candle_to_ticks(ts_ms, o, h, l, c):
    bull = c >= o
    first = l if bull else h
    second = h if bull else l
    return [(ts_ms + TICK_OFFSETS[0], o), (ts_ms + TICK_OFFSETS[1], first),
            (ts_ms + TICK_OFFSETS[2], second), (ts_ms + TICK_OFFSETS[3], c)]


class Market:
    """与 v2 相同的并行预取行情层(1m K线 + tick 展开)。"""

    FETCH_WORKERS = 4

    def __init__(self):
        self.instruments: dict[str, str] = {}
        self.klines: dict[str, list] = {}
        self.kl_dir = WORK / "klines1m"
        self.kl_dir.mkdir(parents=True, exist_ok=True)
        self._cond = threading.Condition()
        self._queue: list = []
        self._pending: set = set()
        self._failed: set = set()
        self._sym_locks: dict = {}
        self._loaded: set = set()
        self._have_cache: dict = {}
        self._workers = [threading.Thread(target=self._worker, daemon=True)
                         for _ in range(self.FETCH_WORKERS)]
        for w in self._workers:
            w.start()
        self._load_instruments()

    def _load_instruments(self) -> None:
        cache = WORK / "instruments.json"
        if cache.exists():
            self.instruments = json.loads(cache.read_text(encoding="utf-8"))
            return
        cursor, bybit = "", set()
        while True:
            url = ("https://api.bybit.com/v5/market/instruments-info"
                   f"?category=linear&limit=1000{('&cursor=' + cursor) if cursor else ''}")
            data = _http_get(url)
            lst = ((data.get("result") or {}).get("list")) or []
            for it in lst:
                if it.get("quoteCoin") == "USDT" and it.get("status") == "Trading":
                    bybit.add(it["symbol"])
            cursor = ((data.get("result") or {}).get("nextPageCursor")) or ""
            if not cursor or not lst:
                break
            time.sleep(0.15)
        binance = set()
        try:
            data = _http_get("https://api.binance.com/fapi/v1/exchangeInfo")
            for it in data.get("symbols", []):
                if it.get("quoteAsset") == "USDT" and it.get("contractType") == "PERPETUAL" \
                        and it.get("status") == "TRADING":
                    binance.add(it["symbol"])
        except Exception as e:
            print(f"[market] binance 失败(忽略): {e}")
        self.instruments = {s: "bybit" for s in sorted(bybit)}
        for s in sorted(binance):
            self.instruments.setdefault(s, "binance")
        cache.write_text(json.dumps(self.instruments), encoding="utf-8")
        print(f"[market] bybit {len(bybit)} + binance 兜底 {len(binance - bybit)}")

    def resolve(self, base: str):
        s = (base or "").upper().strip()
        s = re.sub(r"[^A-Z0-9/]", "", s).replace("/", "")
        if not s:
            return None
        cands = ([s, "1000" + s[:-4] + "USDT"] if s.endswith("USDT")
                 else [s + "USDT", "1000" + s + "USDT"])
        for cand in cands:
            if cand in self.instruments:
                return cand, self.instruments[cand], (1000.0 if cand.startswith("1000") else 1.0)
        return None

    def _sym_lock(self, sym: str) -> threading.RLock:
        with self._cond:
            if sym not in self._sym_locks:
                self._sym_locks[sym] = threading.RLock()
            return self._sym_locks[sym]

    def _kfile(self, sym: str) -> Path:
        return self.kl_dir / f"{sym}.json"

    def _cache_save(self, path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _load_cur(self, sym: str) -> list:
        with self._sym_lock(sym):
            if sym not in self._loaded:
                f = self._kfile(sym)
                self.klines[sym] = (json.loads(f.read_text(encoding="utf-8"))
                                    if f.exists() else [])
                self._loaded.add(sym)
            return self.klines[sym]

    def _have(self, sym: str) -> set:
        with self._cond:
            hv = self._have_cache.get(sym)
        if hv is None:
            hv = {int(r[0]) for r in self._load_cur(sym)}
            with self._cond:
                if sym not in self._have_cache:
                    self._have_cache[sym] = hv
                else:
                    hv = self._have_cache[sym]
        return hv

    def _missing_segs(self, sym, t0_ms, t1_ms):
        step = 60_000
        t0_ms = (t0_ms // step) * step
        have = self._have(sym)
        missing = [t for t in range(t0_ms, t1_ms, step) if t not in have]
        if not missing:
            return []
        segs, seg_start, prev = [], missing[0], missing[0]
        for t in missing[1:]:
            if t - prev > step:
                segs.append((seg_start, prev))
                seg_start = t
            prev = t
        segs.append((seg_start, prev))
        return segs

    def _fetch_window(self, sym, source, t0_ms, t1_ms) -> list:
        rows = []
        if source == "bybit":
            end = t1_ms
            while end > t0_ms:
                url = ("https://api.bybit.com/v5/market/kline"
                       f"?category=linear&symbol={sym}&interval=1"
                       f"&start={t0_ms}&end={end}&limit=1000")
                lst = (( _http_get(url).get("result") or {}).get("list")) or []
                if not lst:
                    break
                for it in lst:
                    rows.append([int(it[0]), float(it[1]), float(it[2]),
                                 float(it[3]), float(it[4])])
                oldest = min(int(it[0]) for it in lst)
                if len(lst) < 1000:
                    break
                end = oldest - 1
                time.sleep(0.1)
        else:
            start = t0_ms
            while start < t1_ms:
                url = ("https://api.binance.com/fapi/v1/klines"
                       f"?symbol={sym}&interval=1m&startTime={start}"
                       f"&endTime={t1_ms}&limit=1000")
                lst = _http_get(url)
                if not isinstance(lst, list) or not lst:
                    break
                for it in lst:
                    rows.append([int(it[0]), float(it[1]), float(it[2]),
                                 float(it[3]), float(it[4])])
                if len(lst) < 1000:
                    break
                start = int(lst[-1][0]) + 1
                time.sleep(0.1)
        return rows

    def _ensure_seg(self, sym, source, s0, s1) -> None:
        step = 60_000
        rows = self._fetch_window(sym, source, s0, s1 + step)
        if not rows:
            with self._cond:
                self._failed.add((sym, (s0, s1)))
            return
        with self._sym_lock(sym):
            merged = sorted(self._load_cur(sym) + rows, key=lambda x: int(x[0]))
            seen, cur2 = set(), []
            for r in merged:
                ts = int(r[0])
                if ts not in seen:
                    seen.add(ts)
                    cur2.append(r)
            self.klines[sym] = cur2
            with self._cond:
                self._have_cache.setdefault(sym, set()).update(int(r[0]) for r in rows)
            self._cache_save(self._kfile(sym), cur2)

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                sym, source, s0, s1 = self._queue.pop(0)
            try:
                self._ensure_seg(sym, source, s0, s1)
            except Exception as e:
                print(f"[kline] {sym} {s0}-{s1} 异常: {e}")
                with self._cond:
                    self._failed.add((sym, (s0, s1)))
            finally:
                with self._cond:
                    self._pending.discard((sym, (s0, s1)))
                    self._cond.notify_all()

    def ensure(self, sym, source, t0_ms, t1_ms) -> None:
        deadline = time.time() + 240
        while time.time() < deadline:
            segs = [s for s in self._missing_segs(sym, t0_ms, t1_ms)
                    if (sym, s) not in self._failed]
            if not segs:
                return
            with self._cond:
                for s in segs:
                    if (sym, s) not in self._pending:
                        self._pending.add((sym, s))
                        self._queue.append((sym, source, s[0], s[1]))
                self._cond.notify_all()
                self._cond.wait(timeout=3)

    def prefetch(self, sym, source, t0_ms, t1_ms) -> None:
        try:
            segs = [s for s in self._missing_segs(sym, t0_ms, t1_ms)
                    if (sym, s) not in self._failed]
        except Exception:
            return
        with self._cond:
            for s in segs:
                if (sym, s) not in self._pending:
                    self._pending.add((sym, s))
                    self._queue.append((sym, source, s[0], s[1]))
            self._cond.notify_all()

    def candles(self, sym, t0_ms, t1_ms) -> list:
        lst = self._load_cur(sym)
        if not lst:
            return []
        i0 = bisect.bisect_left(lst, t0_ms, key=lambda r: int(r[0]))
        i1 = bisect.bisect_right(lst, t1_ms, key=lambda r: int(r[0]))
        return lst[i0:i1]

    def evict(self, sym) -> None:
        with self._cond:
            self.klines.pop(sym, None)
            self._loaded.discard(sym)
            self._have_cache.pop(sym, None)

    def trim(self, sym, keep_from_ms) -> None:
        cur = self.klines.get(sym)
        if not cur:
            return
        i = bisect.bisect_left(cur, keep_from_ms, key=lambda r: int(r[0]))
        if i > 0:
            self.klines[sym] = cur[i:]

    def tick_price_at(self, sym, source, ts_ms):
        minute = ts_ms // 60_000 * 60_000
        self.ensure(sym, source, minute - 60_000, minute + 60_000)
        px = None
        for c in self.candles(sym, minute - 60_000, minute):
            for tms, price in candle_to_ticks(int(c[0]), float(c[1]), float(c[2]),
                                              float(c[3]), float(c[4])):
                if tms <= ts_ms:
                    px = price
        return px


# ---------------------------------------------------------------------------
# 输入装载
# ---------------------------------------------------------------------------

def load_signals() -> list:
    # 共识版优先(二次盲读合并), 退化到首版
    path = WORK / "signals_final.json"
    if not path.exists():
        path = WORK / "signals.json"
    sigs = json.loads(path.read_text(encoding="utf-8"))
    sigs.sort(key=lambda s: s["ts_ms"])
    print(f"[sig] {len(sigs)} 条开仓信号 ({path.name})")
    return sigs


def load_manages() -> list:
    path = WORK / "manages.json"
    if not path.exists():
        return []
    mg = json.loads(path.read_text(encoding="utf-8"))
    mg.sort(key=lambda s: s["ts_ms"])
    print(f"[mg] {len(mg)} 条管理消息")
    return mg


# ---------------------------------------------------------------------------
# 模拟器
# ---------------------------------------------------------------------------

def _slip_price(price: float, side: str, is_long: bool, adverse: bool) -> float:
    """市价/止损成交的滑点: 不利方向 0.05%。"""
    if not adverse:
        return price
    if (is_long and not side == "BUY") or (not is_long and side == "BUY"):
        return price * (1 - SLIP)
    return price * (1 + SLIP)


class Sim:
    def __init__(self, rule: str = "r_ladder"):
        self.mkt = Market()
        self.trades: list = []
        self.rule = rule          # r_ladder(R-ladder) | tp_ladder(信号止盈位均分)
        self._fund_cache: dict[str, list] = {}   # sym -> [[ts,rate],...] 升序

    # ---- 资金费(Bybit 主源, Binance 兜底; 磁盘缓存) ----

    def _fetch_funding(self, sym: str, source: str, t0_ms: int, t1_ms: int) -> list:
        fdir = WORK / "funding"
        fdir.mkdir(parents=True, exist_ok=True)
        f = fdir / f"{sym}.json"
        rows: list = []
        if f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))
        need = [t for t in (t0_ms, t1_ms)
                if not rows or t < rows[0][0] or t > rows[-1][0]]
        if need:
            lo, hi = min(min(need) if need else t0_ms, rows[0][0] if rows else t0_ms), \
                max(max(need) if need else t1_ms, rows[-1][0] if rows else t1_ms)
            got = []
            try:
                if source == "bybit":
                    cursor = ""
                    for attempt in range(3):
                        try:
                            while True:
                                url = ("https://api.bybit.com/v5/market/funding/history"
                                       f"?category=linear&symbol={sym}"
                                       f"&startTime={lo}&endTime={hi}&limit=200"
                                       + (f"&cursor={cursor}" if cursor else ""))
                                d = _http_get(url, timeout=10)
                                lst = (d.get("result") or {}).get("list") or []
                                for it in lst:
                                    got.append([int(it["fundingRateTimestamp"]),
                                                float(it["fundingRate"])])
                                cursor = (d.get("result") or {}).get("nextPageCursor") or ""
                                if not cursor or not lst:
                                    break
                                time.sleep(0.1)
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                            time.sleep(2)
                else:
                    start = lo
                    for attempt in range(3):
                        try:
                            while start < hi:
                                url = ("https://api.binance.com/fapi/v1/fundingRate"
                                       f"?symbol={sym}&startTime={start}&endTime={hi}&limit=1000")
                                lst = _http_get(url, timeout=10)
                                if not isinstance(lst, list) or not lst:
                                    break
                                for it in lst:
                                    got.append([int(it["fundingTime"]), float(it["fundingRate"])])
                                start = int(lst[-1]["fundingTime"]) + 1
                                if len(lst) < 1000:
                                    break
                                time.sleep(0.1)
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                            time.sleep(2)
            except Exception as e:
                print(f"[funding] {sym} 获取失败(按无资金费处理): {e}")
                got = []
            if got:
                rows = sorted({int(r[0]): r for r in (rows + got)}.values(), key=lambda x: x[0])
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                tmp.replace(f)
        return rows

    def _funding_events(self, sym, source, t0_ms, t1_ms) -> list:
        rows = self._fetch_funding(sym, source, t0_ms, t1_ms)
        return [[ts, rate] for ts, rate in rows if t0_ms <= ts <= t1_ms]

    # ---- 事件流 ----

    def run(self, signals: list, manages: list) -> None:
        # 管理消息按"解析后的合约名"分桶(秒级插入持仓走查); 缺 symbol/解析不到的
        # 动作挂孤儿桶, 由事件循环里"唯一活跃仓"认领
        mg_by_sym: dict[str, list] = {}
        orphans: list = []
        for m in manages:
            for it in m.get("items", []):
                base = re.sub(r"USDT$|USDC$", "", str(it.get("symbol") or "").upper())
                res = self.mkt.resolve(base) if base else None
                if res:
                    mg_by_sym.setdefault(res[0], []).append(
                        {"ts_ms": m["ts_ms"], "item": it, "text": m.get("text", "")})
                else:
                    orphans.append({"ts_ms": m["ts_ms"], "item": it,
                                    "text": m.get("text", "")})
        for lst in mg_by_sym.values():
            lst.sort(key=lambda x: x["ts_ms"])
        orphans.sort(key=lambda x: x["ts_ms"])

        # 开跑前把全部开仓信号的入场窗口预取入队(4 线程并行拉取)
        for sig in signals:
            base = re.sub(r"USDT$|USDC$", "", str(sig["symbol"]).upper())
            res = self.mkt.resolve(base)
            if res:
                symbol, source, _ = res
                self.mkt.prefetch(symbol, source, sig["ts_ms"],
                                  sig["ts_ms"] + ENTRY_TIMEOUT_H * 3600 * 1000)
        # 等待入场窗口全部落盘(每标的每段最长 240s 的 ensure 兜底由 worker 承担)
        _t0 = time.time()
        while self.mkt._queue or self.mkt._pending:
            time.sleep(5)
            if time.time() - _t0 > 3600:
                print("[prefetch] 等待超时 1h, 带缺口继续")
                break

        positions: dict[str, dict] = {}
        last_sig: dict[str, tuple] = {}

        for sig in signals:
            self._advance_all(positions, sig["ts_ms"])
            # 孤儿管理消息: 仅当唯一活跃仓时认领(与生产单仓口径一致)
            while orphans and orphans[0]["ts_ms"] <= sig["ts_ms"]:
                o = orphans.pop(0)
                if len(positions) == 1:
                    only = next(iter(positions.values()))
                    mg_by_sym.setdefault(only["symbol"], []).append(
                        {"ts_ms": o["ts_ms"], "item": o["item"], "text": o["text"]})
                    mg_by_sym[only["symbol"]].sort(key=lambda x: x["ts_ms"])
            self._handle_open(sig, positions, last_sig, mg_by_sym)
            if len(self.mkt.klines) > 48:
                active = {p["symbol"] for p in positions.values()}
                keep_from = sig["ts_ms"] - 35 * 86400 * 1000
                for s in list(self.mkt.klines):
                    if s not in active:
                        self.mkt.evict(s)
                    else:
                        self.mkt.trim(s, keep_from)
                gc.collect()

        self._advance_all(positions, int(time.time() * 1000))
        for s in list(self.mkt.klines):
            self.mkt.evict(s)
        gc.collect()
        for base, p in list(positions.items()):
            px = self.mkt.tick_price_at(p["symbol"], p["source"], int(time.time() * 1000))
            if px is None:
                px = p["legs"][0]["entry"]
            fev = p.get("fev") or []
            while p.get("fev_i", 0) < len(fev) and fev[p["fev_i"]][0] <= int(time.time() * 1000):
                self._apply_funding(p, fev[p["fev_i"]][1], px)
                p["fev_i"] = p.get("fev_i", 0) + 1
            for leg in p["legs"]:
                if leg["remain_frac"] > 1e-9:
                    leg["exits"].append({"price": px, "frac": leg["remain_frac"],
                                         "ts_ms": None, "reason": "数据截止持仓"})
                    leg["remain_frac"] = 0.0
            p["status"] = "open"
            self._finish(p)

    def _pending_manages(self, mg_by_sym, symbol, upto_ms) -> list:
        lst = mg_by_sym.get(symbol, [])
        out = []
        while lst and lst[0]["ts_ms"] <= upto_ms:
            out.append(lst.pop(0))
        return out

    # ---- tick 推进(秒级: 消息在 tick 之间按毫秒插入) ----

    def _advance_all(self, positions: dict, until_ms: int) -> None:
        for base in list(positions):
            pos = positions[base]
            t0 = int(pos.get("walked_ms") or pos["first_fill_ms"])
            t1 = min(until_ms, int((pos["first_fill_ms"] / 1000
                                    + POSITION_CAP_D * 86400) * 1000))
            if t1 <= t0:
                continue
            self.mkt.ensure(pos["symbol"], pos["source"], t0, t1)
            pend = self._pending_manages(pos["mg_by_sym"], pos["symbol"], t1)
            pi = 0
            closed = False
            for c in self.mkt.candles(pos["symbol"], t0, t1):
                for tms, price in candle_to_ticks(int(c[0]), float(c[1]),
                                                  float(c[2]), float(c[3]), float(c[4])):
                    if tms <= t0 or tms > t1:
                        continue
                    # 消息在上一 tick 与本 tick 之间 → 本 tick 价成交
                    while pi < len(pend) and pend[pi]["ts_ms"] <= tms:
                        self._apply_manage(pos, pend[pi], price)
                        pi += 1
                        if pos.get("force_closed"):
                            break
                    if pos.get("force_closed"):
                        break
                    # 资金费时点(秒级): 按未平分数与当时名义价值收付
                    fev = pos.get("fev") or []
                    while pos.get("fev_i", 0) < len(fev) and fev[pos["fev_i"]][0] <= tms:
                        self._apply_funding(pos, fev[pos["fev_i"]][1], price)
                        pos["fev_i"] = pos.get("fev_i", 0) + 1
                    pos["walked_ms"] = tms
                    if self._tick_legs(pos, price, tms):
                        closed = True
                        break
                if closed or pos.get("force_closed"):
                    break
            if pos["remain_total"] <= 1e-9 or pos.get("force_closed"):
                pos["status"] = "closed"
                self._finish(pos)
                positions.pop(pos["base"], None)

    def _apply_funding(self, pos, rate: float, price: float) -> None:
        is_long = pos["side"] == "LONG"
        delta = 0.0
        for leg in pos["legs"]:
            if leg["remain_frac"] <= 1e-9:
                continue
            qty = (RISK_USDT * leg["risk_share"]) / leg["risk_unit"]
            notional = qty * price * leg["remain_frac"]
            pay = rate * notional
            delta += -pay if is_long else pay
        pos["funding_usdt"] = pos.get("funding_usdt", 0.0) + delta

    def _tick_legs(self, pos, price, tms) -> bool:
        is_long = pos["side"] == "LONG"
        all_done = True
        # close_based 止损: 只在指定周期的收盘 tick 判定(盘中插针不算)。
        # tick 槽位 59,999ms 即该分钟收盘; 4H/1D 用 epoch 分钟对齐。
        close_based = pos.get("stop_close_based")
        tf_min = int(pos.get("stop_tf_min") or 1)
        epoch_min = tms // 60_000
        is_stop_tick = (not close_based) or (epoch_min % tf_min == tf_min - 1
                                             and tms % 60_000 == 59_999)
        # tp_ladder 规则: 信号自带的止盈位阶梯, 每档平 1/N(位置级共享, 各腿按自身余量跟)
        ladder = pos.get("tp_ladder") or []
        if ladder:
            for tp in ladder:
                if tp["done"]:
                    continue
                if (is_long and price >= tp["price"]) or (not is_long and price <= tp["price"]):
                    tp["done"] = True
                    fill = tp["price"] * (1 - SLIP) if is_long else tp["price"] * (1 + SLIP)
                    for leg in pos["legs"]:
                        if leg["remain_frac"] <= 1e-9:
                            continue
                        take = min(tp["frac"], leg["remain_frac"])
                        leg["exits"].append({"price": fill, "frac": take, "ts_ms": tms,
                                             "reason": f"TP{tp['i']}@{tp['price']:g}"})
                        leg["remain_frac"] -= take
            if all(l["remain_frac"] <= 1e-9 for l in pos["legs"]):
                pos["remain_total"] = 0.0
                return True
        for leg in pos["legs"]:
            if leg["remain_frac"] <= 1e-9:
                continue
            all_done = False
            # 1) 止损(硬/保本/收盘条件), 滑点不利
            stop_eff = leg["stop"] * (1 - SLIP) if is_long else leg["stop"] * (1 + SLIP)
            breached = (is_long and price <= stop_eff) or (not is_long and price >= stop_eff)
            if breached and is_stop_tick:
                leg["exits"].append({"price": stop_eff, "frac": leg["remain_frac"],
                                     "ts_ms": tms,
                                     "reason": f"止损@{leg['stop']:g}" + ("收盘" if close_based else "")})
                leg["remain_frac"] = 0.0
                continue
            if breached and close_based:
                continue   # 盘中破位但未收盘 → 不触发
            # 2) +R1 平 F1 + 保本 (r_ladder 规则; tp_ladder 模式跳过)
            # 2026-09-28 出场参数化: GR1_LVL/GR1_FRAC/GR2_LVL/GR2_FRAC 环境变量,
            # 缺省 1.0/0.40/2.0/0.40 = 原版 R 阶梯
            if pos.get("rule") != "tp_ladder" and not leg["t1_done"]:
                r1 = leg["entry"] + R1_LVL * leg["risk_unit"] if is_long \
                    else leg["entry"] - R1_LVL * leg["risk_unit"]
                if (is_long and price >= r1) or (not is_long and price <= r1):
                    fill = r1 * (1 - SLIP) if is_long else r1 * (1 + SLIP)
                    leg["exits"].append({"price": fill, "frac": R1_FRAC, "ts_ms": tms,
                                         "reason": f"{R1_LVL:g}R平{R1_FRAC*100:.0f}%"})
                    leg["remain_frac"] -= R1_FRAC
                    leg["stop"] = leg["entry"]
                    leg["t1_done"] = True
            # 3) +R2 平 F2 (r_ladder 规则; hybrid=先1R半仓保本后全跟交易员, 跳过R2)
            if pos.get("rule") not in ("tp_ladder", "hybrid") \
                    and leg["t1_done"] and not leg["t2_done"]:
                r2 = leg["entry"] + R2_LVL * leg["risk_unit"] if is_long \
                    else leg["entry"] - R2_LVL * leg["risk_unit"]
                if (is_long and price >= r2) or (not is_long and price <= r2):
                    fill = r2 * (1 - SLIP) if is_long else r2 * (1 + SLIP)
                    leg["exits"].append({"price": fill, "frac": R2_FRAC, "ts_ms": tms,
                                         "reason": f"{R2_LVL:g}R平{R2_FRAC*100:.0f}%"})
                    leg["remain_frac"] -= R2_FRAC
                    leg["t2_done"] = True
            # 4) 30 天上限(到点平剩余)
            cap_ms = pos["first_fill_ms"] + POSITION_CAP_D * 86400 * 1000
            if tms >= cap_ms and leg["remain_frac"] > 1e-9:
                leg["exits"].append({"price": price, "frac": leg["remain_frac"],
                                     "ts_ms": tms, "reason": "30天上限平仓"})
                leg["remain_frac"] = 0.0
        pos["remain_total"] = sum(l["remain_frac"] * l["risk_share"] for l in pos["legs"])
        return pos["remain_total"] <= 1e-9

    # ---- 管理消息(秒级, 成交=消息后首个 tick 价) ----

    def _apply_manage(self, pos, mg, tick_price) -> None:
        item = mg["item"]
        params = item.get("params") or {}
        action = str(params.get("action_type") or params.get("action") or "")
        is_long = pos["side"] == "LONG"
        if action in ("CLOSE_ALL", "closed", "stopped_out", "stopped_be"):
            for leg in pos["legs"]:
                if leg["remain_frac"] > 1e-9:
                    fill = tick_price * ((1 - SLIP) if is_long else (1 + SLIP))
                    leg["exits"].append({"price": fill, "frac": leg["remain_frac"],
                                         "ts_ms": mg["ts_ms"], "reason": "喊单全平"})
                    leg["remain_frac"] = 0.0
            pos["remain_total"] = 0.0
            pos["force_closed"] = True
        elif action in ("MOVE_BE", "stop_be", "stop_move"):
            # 生产 parse_ov_manage 产出 new_sl; 兼容旧字段 stop_price/new_stop。
            # 缺这个键时显式移损价会被静默退化成保本价(实测 13/30 条受影响)
            new_stop = (params.get("stop_price") or params.get("new_stop")
                        or params.get("new_sl"))
            mult = pos["mult"]
            ns = float(new_stop) * mult if new_stop else pos["legs"][0]["entry"]
            eff = ns * ((1 - SLIP) if is_long else (1 + SLIP))
            if (is_long and eff >= tick_price) or (not is_long and eff <= tick_price):
                for leg in pos["legs"]:
                    if leg["remain_frac"] > 1e-9:
                        fill = tick_price * ((1 - SLIP) if is_long else (1 + SLIP))
                        leg["exits"].append({"price": fill, "frac": leg["remain_frac"],
                                             "ts_ms": mg["ts_ms"], "reason": "移损越价即平"})
                        leg["remain_frac"] = 0.0
                pos["remain_total"] = 0.0
                pos["force_closed"] = True
            else:
                for leg in pos["legs"]:
                    leg["stop"] = ns
        elif action in ("PARTIAL_TP", "partial"):
            pct = min(float(params.get("percent") or 50) / 100.0, 1.0)
            fill = tick_price * ((1 - SLIP) if is_long else (1 + SLIP))
            for leg in pos["legs"]:
                if leg["remain_frac"] > 1e-9:
                    frac = min(pct, leg["remain_frac"])
                    leg["exits"].append({"price": fill, "frac": frac, "ts_ms": mg["ts_ms"],
                                         "reason": f"喊单止盈{params.get('percent', 50):g}%"})
                    leg["remain_frac"] -= frac
            pos["remain_total"] = sum(l["remain_frac"] * l["risk_share"] for l in pos["legs"])
            if pos["remain_total"] <= 1e-9:
                pos["force_closed"] = True

    # ---- 开仓 ----

    def _handle_open(self, sig, positions, last_sig, mg_by_sym) -> None:
        base = re.sub(r"USDT$|USDC$", "", str(sig["symbol"]).upper())
        side = str(sig.get("side") or "").upper()
        if side not in ("LONG", "SHORT"):
            return
        res = self.mkt.resolve(base)
        if not res:
            self._skip(sig, base, side, "无合约标的")
            return
        symbol, source, mult = res
        is_long = side == "LONG"

        entry_prices = [float(p) * mult for p in (sig.get("entry_prices") or []) if p]
        if not entry_prices and sig.get("entry_price"):
            entry_prices = [float(sig["entry_price"]) * mult]
        sl = sig.get("sl")
        sl = float(sl) * mult if sl is not None else None
        market_entry = (str(sig.get("order_type") or "").upper() == "MARKET"
                        or not entry_prices)
        if sl is None:
            self._skip(sig, base, side, "无止损跳过")
            return
        entry_ref = sum(entry_prices) / len(entry_prices) if entry_prices else None
        ref = entry_ref if entry_ref is not None else (sl * (1.01 if is_long else 0.99))
        if (is_long and sl >= ref) or (not is_long and sl <= ref):
            self._skip(sig, base, side, "止损方向无效")
            return
        dedup = (symbol, side, round(entry_ref or 0, 6), round(sl, 6))
        prev = last_sig.get(symbol)
        if prev and prev[0] == dedup and sig["ts_ms"] - prev[1] < DEDUPE_H * 3600 * 1000:
            return
        last_sig[symbol] = (dedup, sig["ts_ms"])

        if base in positions:
            old = positions.pop(base)
            px = self.mkt.tick_price_at(old["symbol"], old["source"],
                                        max(sig["ts_ms"], int(old["walked_ms"] or 0)))
            for leg in old["legs"]:
                if leg["remain_frac"] > 1e-9:
                    leg["exits"].append({"price": px or leg["entry"],
                                         "frac": leg["remain_frac"],
                                         "ts_ms": sig["ts_ms"], "reason": "换新撤旧"})
                    leg["remain_frac"] = 0.0
            self._finish(old)

        msg_ms = sig["ts_ms"]
        if market_entry:
            window_ms = 60_000
        else:
            window_ms = ENTRY_TIMEOUT_H * 3600 * 1000
        self.mkt.ensure(symbol, source, msg_ms, msg_ms + window_ms)
        # 含消息所在分钟的 K 线(消息秒级插入该分钟 tick 之间, 成交不能跳过本分钟)
        candles = self.mkt.candles(symbol, max(0, msg_ms - 60_000), msg_ms + window_ms)
        if not candles:
            self._skip(sig, base, side, "无K线数据")
            return

        # 逐腿找成交 tick(市场单=消息后首个 tick; 限价=首次触价)
        legs_spec = []
        if market_entry:
            fill_px = fill_tms = None
            for c in candles:
                for tms, price in candle_to_ticks(int(c[0]), float(c[1]), float(c[2]),
                                                  float(c[3]), float(c[4])):
                    if tms < msg_ms:
                        continue
                    fill_px = price * (1 + SLIP) if is_long else price * (1 - SLIP)
                    fill_tms = tms
                    break
                if fill_px is not None:
                    break
            if fill_px is None:
                self._skip(sig, base, side, "无K线数据")
                return
            if (is_long and fill_px <= sl) or (not is_long and fill_px >= sl):
                self._skip(sig, base, side, "开仓即越过止损")
                return
            legs_spec.append((fill_px, fill_px, fill_tms))
        else:
            # "CMP till X"/"CMP and X" 型文字 → 生产 zone_two_legs 双腿
            two_leg = bool(re.search(r"(?i)CMP\s*(?:till|until|&|and)\s*\$?\s*\d",
                                     sig.get("text") or ""))
            # 信号时点现价 = 消息后首个 tick
            ref_px = None
            for c in candles:
                for tms, price in candle_to_ticks(int(c[0]), float(c[1]), float(c[2]),
                                                  float(c[3]), float(c[4])):
                    if tms >= msg_ms:
                        ref_px = price
                        break
                if ref_px is not None:
                    break
            for ep in sorted(set(entry_prices), reverse=not is_long):
                # 生产 zone_two_legs 语义: "CMP till X" = 市价腿(立即) + 限价腿(挂 X 等回触)
                if two_leg and not legs_spec:
                    fill = ref_px * (1 + SLIP) if is_long else ref_px * (1 - SLIP)
                    legs_spec.append((fill, fill, msg_ms))
                # 限价挂在"立即成交"一侧(买限≥现价/卖限≤现价)时, 交易所按市价立即
                # 成交而不会等回触
                if ref_px is not None and ((is_long and ep >= ref_px)
                                           or (not is_long and ep <= ref_px)):
                    fill = ref_px * (1 + SLIP) if is_long else ref_px * (1 - SLIP)
                    legs_spec.append((fill, fill, msg_ms))
                    continue
                got = None
                for c in candles:
                    for tms, price in candle_to_ticks(int(c[0]), float(c[1]), float(c[2]),
                                                      float(c[3]), float(c[4])):
                        if tms < msg_ms:
                            continue
                        if (is_long and price <= ep) or (not is_long and price >= ep):
                            got = (ep, ep, tms)
                            break
                    if got:
                        break
                if got:
                    legs_spec.append(got)
            if not legs_spec:
                self._skip(sig, base, side, "48h未成交")
                return

        n = len(legs_spec)
        legs = []
        first_fill_ms = min(sp[2] for sp in legs_spec)
        for ep, fpx, ftms in legs_spec:
            risk_unit = abs(ep - sl)
            if risk_unit <= 0:
                continue
            legs.append({"entry": ep, "risk_unit": risk_unit, "risk_share": 1.0 / n,
                         "remain_frac": 1.0, "stop": sl, "t1_done": False,
                         "t2_done": False, "fill_ms": ftms, "exits": []})
        if not legs:
            return
        # tp_ladder 规则: 信号自带的止盈位(图读或文字), 每档平 1/N
        # hybrid(2026-09-28): 同样构建 TP 阶梯——+R1 平 F1+保本后, 剩余跟此阶梯+喊单
        tp_ladder = []
        if self.rule in ("tp_ladder", "hybrid"):
            tps = [float(p) * mult for p in (sig.get("tp_levels")
                                             or sig.get("tp_prices") or []) if p]
            n_tp = len(tps)
            first_fill = legs_spec[0][1] if legs_spec else None
            for i, tp in enumerate(sorted(set(tps), reverse=not is_long)):
                skipped = first_fill is not None and (
                    (is_long and tp <= first_fill) or (not is_long and tp >= first_fill))
                tp_ladder.append({"i": i + 1, "price": tp, "frac": 1.0 / n_tp,
                                  "done": skipped})

        pos = {"trader": "trader-A", "symbol": symbol, "base": base, "side": side,
               "signal_ts_ms": msg_ms, "first_fill_ms": first_fill_ms,
               "walked_ms": first_fill_ms, "stop_init": sl, "mult": mult,
               "source": source, "legs": legs, "remain_total": 1.0,
               "status": None, "msg_id": sig.get("id"), "text": (sig.get("text") or "")[:200],
               "mg_by_sym": mg_by_sym, "sl_source": sig.get("sl_source"),
               "funding_usdt": 0.0, "fev_i": 0,
               "fev": self._funding_events(symbol, source, first_fill_ms,
                                           first_fill_ms + (POSITION_CAP_D + 1) * 86400 * 1000),
               "rule": self.rule, "tp_ladder": tp_ladder,
               "stop_close_based": str(sig.get("stop_trigger_type") or "") == "close_based",
               "stop_tf_min": {"1H": 60, "4H": 240, "1D": 1440, "15m": 15}.get(
                   str(sig.get("stop_timeframe") or ""), 60),
               "force_closed": False}
        positions[base] = pos
        self.mkt.prefetch(symbol, source, msg_ms, msg_ms + 6 * 86400 * 1000)

    def _skip(self, sig, base, side, reason):
        self.trades.append({"trader": "trader-A", "symbol": base, "side": side,
                            "signal_ts_ms": sig["ts_ms"], "status": reason, "r": None,
                            "msg_id": sig.get("id"), "text": (sig.get("text") or "")[:200]})

    def _finish(self, pos) -> None:
        r_total = fee_usdt = 0.0
        for leg in pos["legs"]:
            # 名义价值口径: 数量 = 该腿风险金 / 单位风险
            qty = (RISK_USDT * leg["risk_share"]) / leg["risk_unit"]
            fee_usdt += qty * leg["entry"] * FEE_RATE
            for ep in leg["exits"]:
                move = ((ep["price"] - leg["entry"]) if pos["side"] == "LONG"
                        else (leg["entry"] - ep["price"]))
                # 出场分数是"腿内占比", 换算到仓位级要乘该腿的风险占比
                r_total += (move / leg["risk_unit"]) * ep["frac"] * leg["risk_share"]
                fee_usdt += qty * ep["price"] * ep["frac"] * FEE_RATE
        pos["r"] = round(r_total, 4)
        pos["pnl_usdt"] = round(r_total * RISK_USDT, 2)
        pos["net_usdt"] = round(pos["pnl_usdt"] + pos.get("funding_usdt", 0.0)
                                - fee_usdt, 2)
        pos["fee_usdt"] = round(fee_usdt, 2)
        pos["entry"] = sum(l["entry"] * l["risk_share"] for l in pos["legs"]) \
            / sum(l["risk_share"] for l in pos["legs"])
        pos["exit_parts"] = sorted(
            (ep for leg in pos["legs"] for ep in leg["exits"]),
            key=lambda x: x["ts_ms"] or 9e15)
        self.trades.append(pos)


# ---------------------------------------------------------------------------
# 预取 / 报告
# ---------------------------------------------------------------------------

def phase_prefetch() -> None:
    wins = json.loads((WORK / "kline_windows.json").read_text(encoding="utf-8"))
    mkt = Market()
    for w in wins:
        mkt.prefetch(w["symbol"], "bybit", w["t0"], w["t1"])
    print(f"[prefetch] {len(wins)} 标的窗口已入队(4 线程后台拉取)")
    deadline = time.time() + 3600 * 4
    while time.time() < deadline:
        with mkt._cond:
            q = len(mkt._queue)
        if q == 0:
            done = len(mkt._pending)
            if done == 0:
                break
        time.sleep(20)
        print(f"  队列 {q}, 内存标的 {len(mkt.klines)}")
    print("[prefetch] 完成")


def phase_sim() -> None:
    database.init_db()
    signals = load_signals()
    manages = load_manages()
    sim = Sim(rule=os.getenv("SVB_RULE", "r_ladder"))
    sim.run(signals, manages)
    (WORK / "trades.json").write_text(json.dumps(sim.trades, ensure_ascii=False),
                                      encoding="utf-8")
    print(f"[sim] 共 {len(sim.trades)} 条记录")


def _audit_hashes() -> dict:
    import hashlib as _h
    out = {}
    for name in ("msgs.jsonl", "signals_final.json", "signals.json", "manages.json"):
        p = WORK / name
        if p.exists():
            out[name] = _h.sha256(p.read_bytes()).hexdigest()[:16]
    return out


def phase_report() -> None:
    trades = json.loads((WORK / "trades.json").read_text(encoding="utf-8"))
    closed = [t for t in trades if t.get("status") == "closed" and t.get("r") is not None]
    open_pos = [t for t in trades if t.get("status") == "open"]
    skipped: dict[str, int] = {}
    for t in trades:
        st = t.get("status")
        if st not in ("closed", "open") and st:
            skipped[st] = skipped.get(st, 0) + 1
    wins = [t for t in closed if t["r"] > 0]
    total_r = sum(t["r"] for t in closed)
    total_net = sum(t.get("net_usdt", t.get("pnl_usdt", 0)) for t in closed)
    total_fund = sum(t.get("funding_usdt", 0) for t in closed)
    total_fee = sum(t.get("fee_usdt", 0) for t in closed)
    seq = sorted(closed, key=lambda x: (x.get("exit_parts") or [{}])[-1].get("ts_ms") or 0)
    cum = peak = dd = 0.0
    for t in seq:
        cum += t["r"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    rule_txt = ("+1R 平40%+保本; +2R 平40%; 剩20%只按喊单"
                if os.getenv("SVB_RULE", "r_ladder") == "r_ladder"
                else "信号止盈位阶梯, 每档平1/N; 无喊单跟止损; 30天上限")
    lines = ["# 影子回测(图解析 + 规则模拟)", "",
             f"- 信号(文字+图 vision 解析): {len(trades)}; 成交平仓 {len(closed)}; "
             f"持仓中 {len(open_pos)}",
             f"- 规则: {rule_txt}; 48h 未成交撤",
             f"- 撮合: 1m K线 4 tick(0/20/40/59.999s), 消息按毫秒插入, "
             f"市价/止损滑点{SLIP*100:.2f}%, taker 0.025%/边, 资金费率已计入",
             f"- 胜率 {len(wins)/len(closed)*100:.1f}% | ΣR {total_r:+.2f} | "
             f"均R {total_r/len(closed):+.3f} | 最大回撤 {dd:+.2f}R",
             f"- 折算(25U/单): 毛利 {total_r*RISK_USDT:+.0f}U | "
             f"资金费 {total_fund:+.1f}U | 手续费 -{total_fee:.1f}U | "
             f"净额 {total_net:+.0f}U",
             f"- 跳过: {json.dumps(skipped, ensure_ascii=False)}",
             f"- 审计哈希: {json.dumps(_audit_hashes(), ensure_ascii=False)}", "",
             "| # | 时间 | 标的 | 方向 | 入场 | 止损源 | 出场 | R | 净U |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, t in enumerate(seq, 1):
        exits = " + ".join(f"{e['reason']}@{e['price']:g}" for e in t.get("exit_parts", []))
        ts = datetime.fromtimestamp(t["signal_ts_ms"] / 1000, tz=timezone.utc) \
            .strftime("%Y-%m-%d %H:%M:%S") if t.get("signal_ts_ms") else ""
        net = t.get("net_usdt", t.get("pnl_usdt", 0))
        lines.append(f"| {i} | {ts} | {t['symbol']} | {t['side']} | "
                     f"{t.get('entry', 0):g} | {t.get('sl_source') or '-'} | {exits} | "
                     f"{t['r']:+.2f} | {net:+.1f} |")
    (WORK / "report_v3.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:13]))


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "sim"
    if phase == "prefetch":
        phase_prefetch()
    elif phase == "sim":
        phase_sim()
    elif phase == "report":
        phase_report()


if __name__ == "__main__":
    main()
