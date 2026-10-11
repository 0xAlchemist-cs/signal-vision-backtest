# -*- coding: utf-8 -*-
"""第七轮审查的场景回归测试 (v0.2.8)。

主题: 订单从"收到信号"到"真正成交"之间的状态与时间一致性 —
voice 市价单的 WAITING_FILL、主引擎即时撮合的成交时刻、
强制关闭路径的未成交腿计费、TP 阶梯去重归一化。
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BASE = 1_775_808_000_000   # 真实纪元毫秒(2026-04), 分钟对齐


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def engine_mod(tmp_path_factory):
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r8_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r8", "backtest/engine.py")
    sys.path.pop(0)


class FakeMarket:
    def __init__(self, bars):
        self.bars = bars
        self.symbol = "TESTUSDT"
        self.klines = {}

    def resolve(self, base):
        return (self.symbol, "fake", 1.0)

    def ensure(self, *a, **k): pass

    def prefetch(self, *a, **k): pass

    def evict(self, *a, **k): pass

    def trim(self, *a, **k): pass

    def candles(self, sym, t0_ms, t1_ms):
        t0 = (t0_ms // 60_000) * 60_000
        return [b for b in self.bars if t0 <= b[0] <= t1_ms]

    def tick_price_at(self, sym, source, ts_ms):
        prev = None
        for b in self.bars:
            if b[0] <= ts_ms:
                prev = b[4]
            else:
                break
        return prev


def _mk_sim(engine_mod, bars):
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    sim.mkt = FakeMarket(bars)
    sim._fetch_funding = lambda *a, **k: []   # 测试不联网
    return sim


def _mkt_sig(ts_ms, side="LONG", entry=100.0, sl=95.0):
    return {"id": f"s{ts_ms}", "ts_ms": ts_ms, "text": "", "symbol": "TESTUSDT",
            "side": side, "order_type": "MARKET", "entry_price": entry,
            "entry_prices": [entry], "sl": sl, "tp_levels": [],
            "stop_trigger_type": None, "stop_timeframe": None}


# ---------------------------------------------------------------------------
# 审查一 (P0): voice 市价单预定成交 tick 晚于后续事件 → 未成交不得当持仓
# ---------------------------------------------------------------------------

def test_voice_market_fill_waiting_new_signal_cancels():
    ve = _load("v_r8a", "backtest/voice_event_engine.py")
    """BUY 市价 05s(预定 20s tick 成交), 10s 又发 SELL — 10s 时 BUY 尚未
    成交, 不得 signal_flip 出一笔不存在的盈亏。正确: 撤 BUY, 只建 SELL。"""
    kl = [[0, 100, 100, 100, 100.0], [60_000, 100, 100, 100, 100.0]]
    evs = [
        {"ts_ms": 5_000, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 90.0, "tp": 110.0},
        {"ts_ms": 10_000, "kind": "signal", "side": "SELL",
         "entry": None, "sl": 130.0, "tp": 70.0},
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    flips = [l for l in res["legs"] if l["reason"] == "signal_flip"]
    assert not flips, f"未成交的市价单被提前平仓(signal_flip): {flips}"
    assert res["legs"], "SELL 应正常建仓并走到数据截止"
    assert all(l["side"] == "SELL" for l in res["legs"]), \
        f"BUY 从未成交, 不得产生 BUY 方向的盈亏腿: {res['legs']}"


def test_voice_market_fill_waiting_close_all_cancels():
    ve = _load("v_r8b", "backtest/voice_event_engine.py")
    """BUY 市价 05s(预定 20s 成交), 10s 喊 close_all — 订单尚未成交,
    应撤单(无盈亏), 不得持仓走到数据截止。"""
    kl = [[0, 100, 100, 100, 100.0], [60_000, 100, 100, 100, 100.0]]
    evs = [
        {"ts_ms": 5_000, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 90.0, "tp": 110.0},
        {"ts_ms": 10_000, "kind": "manage", "ev": "close_all"},
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] == [], \
        f"未成交订单应在 close_all 时撤单, 实际产生盈亏腿: {res['legs']}"


# ---------------------------------------------------------------------------
# 审查二 (P0): 主引擎即时撮合(CMP/可立即成交 LIMIT)成交时刻 = 首个合成 tick
# ---------------------------------------------------------------------------

def test_engine_immediate_fill_ms_is_tick_time(engine_mod):
    """可立即成交的 LIMIT(挂价 >= 现价): 成交时刻 = 消息后首个合成 tick
    (20s), 不是消息时刻(5s) — 否则成交时间被回填早于真实成交, 成交前
    收到的换新信号会误判该腿已成交。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 100.5, 99.5, 100.0],
    ])
    positions, last_sig = {}, {}
    sig = {"id": "a", "ts_ms": 5_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "LIMIT", "entry_price": 100.0,
           "entry_prices": [100.0], "sl": 95.0, "tp_levels": [110.0],
           "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig, positions, last_sig, {})
    assert "TEST" in positions, "挂价>=现价 应立即成交"
    leg = positions["TEST"]["legs"][0]
    assert leg["fill_ms"] == 20_000, \
        f"即时成交应记首个合成 tick 时刻 20000, 实际 {leg['fill_ms']}"


# ---------------------------------------------------------------------------
# 审查三 (P0): 强制关闭(CLOSE_ALL / 移损越价即平)必须撤掉未成交腿
# ---------------------------------------------------------------------------

def test_close_all_cancels_unfilled_leg_fee(engine_mod):
    """双腿 LIMIT [90, 80]: 腿1 @100s 成交, 腿2 预定 @160s 成交;
    130s 喊 CLOSE_ALL → 腿2 必须撤单且开仓费为 0 (旧实现收虚构开仓费)。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 91, 89, 90.0],       # 腿1 成交@90 @100s tick
        [120_000, 90, 95, 79, 80.0],       # 腿2 预定 @160s tick 成交(不会发生)
    ])
    positions, last_sig = {}, {}
    sig = {"id": "a", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "LIMIT", "entry_price": 90.0,
           "entry_prices": [90.0, 80.0], "sl": 82.0, "tp_levels": [110.0],
           "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig, positions, last_sig, {})
    assert "TEST" in positions and len(positions["TEST"]["legs"]) == 2
    pos = positions["TEST"]
    tick = sim.mkt.tick_price_at("TESTUSDT", "fake", 130_000)
    sim._apply_manage(pos, {"ts_ms": 130_000,
                            "item": {"symbol": "TESTUSDT",
                                     "params": {"action_type": "CLOSE_ALL"}},
                            "text": "close all"}, tick)
    # 撮合按价格升序建腿: legs[0]=80 腿(预定 160s 成交), legs[1]=90 腿(100s 已成交)
    unfilled = [l for l in pos["legs"] if l.get("cancelled_before_fill")]
    assert len(unfilled) == 1 and unfilled[0]["entry"] == pytest.approx(80.0), \
        "强制关闭必须显式撤掉未成交腿(标记 cancelled_before_fill)"
    sim._finish(pos)
    t = sim.trades[-1]
    leg1 = [l for l in t["legs"] if not l.get("cancelled_before_fill")][0]
    qty = engine_mod.RISK_USDT * leg1["risk_share"] / leg1["risk_unit"]
    expect = qty * leg1["entry"] * engine_mod.FEE_RATE          # 腿1 开仓费
    for ep in leg1["exits"]:
        expect += qty * ep["price"] * ep["frac"] * engine_mod.FEE_RATE
    assert t["fee_usdt"] == pytest.approx(round(expect, 2)), \
        f"未成交腿不得收开仓费, 实际 {t['fee_usdt']} 期望 {expect}"


# ---------------------------------------------------------------------------
# 审查四 (P1): TP 阶梯 — 重复价位先去重再定档数
# ---------------------------------------------------------------------------

def test_tp_ladder_duplicate_prices_fractions_sum_to_one(engine_mod):
    """tp_levels=[105,105] (模型重复输出): 先去重再定档数 → 1 档平全仓。
    旧实现分母 2、阶梯只有 1 档 → 每档只平 50%, 剩余 50% 永不触发。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 106, 99, 100.0],     # 40s tick=106 触及 TP105
    ])
    positions, last_sig = {}, {}
    sig = {"id": "a", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "MARKET", "entry_price": 100.0,
           "entry_prices": [100.0], "sl": 95.0, "tp_levels": [105.0, 105.0],
           "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig, positions, last_sig, {})
    pos = positions["TEST"]
    assert sum(tp["frac"] for tp in pos["tp_ladder"]) == pytest.approx(1.0), \
        f"去重后止盈比例总和必须为 100%, 实际 {pos['tp_ladder']}"
    sim._advance_all(positions, 180_000)
    assert "TEST" not in positions, "TP105 触及应全平 (旧: 只平 50% 残仓)"
