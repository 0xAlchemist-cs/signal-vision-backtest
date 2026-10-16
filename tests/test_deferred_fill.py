# -*- coding: utf-8 -*-
"""P0 回归: 预成交 payload 曾漏 first_fill_ms → _create_position KeyError。
只在"预成交到点且未被取代 → 真实建仓"路径触发 (此前的被取代路径短路了该分支)。
场景: A LONG 建仓后止损离场 → B(SHORT STOP, 预成交) 到点 → 无旧仓 → 直接建仓。"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


@pytest.fixture()
def eng(tmp_path):
    os.environ["SVB_WORK"] = str(tmp_path)
    spec = importlib.util.spec_from_file_location("eng_df", ROOT / "backtest" / "engine.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eng_df"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeMarket:
    def __init__(self, bars):
        self.bars = bars
        self.symbol = "TESTUSDT"
        self.klines = {}

    def resolve(self, base):
        return (self.symbol, "fake", 1.0)

    def ensure(self, *a, **k):
        pass

    def prefetch(self, *a, **k):
        pass

    def evict(self, *a, **k):
        pass

    def trim(self, *a, **k):
        pass

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


def _mk(eng, bars):
    sim = eng.Sim.__new__(eng.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    sim.mkt = FakeMarket(bars)
    sim._fetch_funding = lambda *a, **k: []
    return sim


def test_deferred_fill_creates_position_end_to_end(eng):
    """A(10:00 多,10:05 止损离场) → B(SHORT STOP 预成交,10:20 到点) →
    到点时无旧仓未被取代 → 必须真实建仓。payload 漏 first_fill_ms 曾致 KeyError。"""
    T0 = 1750731960000
    sim = _mk(eng, [
        [T0 - 60000, 100, 100, 100, 100.0],
        [T0, 100, 100, 99, 100.0],          # A MARKET 建仓
        [T0 + 60000, 100, 100, 94, 100.0],  # A SL 95 → 止损离场 (10:05)
        [T0 + 120000, 100, 100, 93, 100.0],
        [T0 + 180000, 100, 92, 91, 100.0],  # B(SHORT STOP 95) 到点触发
        [T0 + 240000, 100, 91, 90, 100.0],  # B 持仓继续
    ])
    signals = [
        {"id": "a", "ts_ms": T0, "text": "", "symbol": "TESTUSDT",
         "side": "LONG", "order_type": "MARKET", "entry_price": 100.0,
         "entry_prices": [100.0], "sl": 95.0, "tp_levels": [],
         "stop_trigger_type": None, "stop_timeframe": None},
        {"id": "b", "ts_ms": T0 + 60000, "text": "", "symbol": "TESTUSDT",
         "side": "SHORT", "order_type": "STOP", "entry_price": 95.0,
         "entry_prices": [95.0], "sl": 100.0, "tp_levels": [90.0],
         "stop_trigger_type": "intrabar", "stop_timeframe": None},
    ]
    sim.run(signals, [])
    trades = [t for t in sim.trades if t.get("r") is not None]
    assert trades, "两笔都应有记录"
    b = [t for t in trades if t.get("side") == "SHORT"]
    assert b and b[0].get("status") in ("closed", "open"), \
        f"预成交未被取代 → SHORT 应真实建仓, 实际 {[t.get('status') for t in b]}"
    assert b[0].get("first_fill_ms"), "预成交建仓必须带 first_fill_ms"


def test_deferred_superseded_by_newer_signal(eng):
    """对照: 预成交到点前同标的已被更新信号成交 → 预成交按未成交失效。"""
    T0 = 1750731960000
    sim = _mk(eng, [
        [T0 - 60000, 100, 100, 100, 100.0],
        [T0, 100, 100, 99, 100.0],          # A 建仓
        [T0 + 60000, 100, 100, 99, 100.0],  # A 存活 (无止损)
        [T0 + 180000, 100, 100, 99, 100.0],
    ])
    sim._fetch_funding = lambda *a, **k: []
    signals = [
        {"id": "a", "ts_ms": T0, "text": "", "symbol": "TESTUSDT",
         "side": "LONG", "order_type": "MARKET", "entry_price": 100.0,
         "entry_prices": [100.0], "sl": 95.0, "tp_levels": [],
         "stop_trigger_type": None, "stop_timeframe": None},
    ]
    sim.run(signals, [])
    # A 已在持仓, 模拟一条延迟 STOP 预成交挂起后再到点
    deferred = {"fill_ts": T0 + 180000, "seq": 0, "first_fill_ms": T0 + 180000,
                "sig": {"id": "b", "ts_ms": T0 + 60000, "text": "", "symbol": "TESTUSDT",
                        "side": "SHORT", "order_type": "STOP", "entry_price": 95.0,
                        "entry_prices": [95.0], "sl": 100.0, "tp_levels": [90.0]},
                "legs_spec": [(95.0, 95.0 * 0.9995, T0 + 180000)], "legs": [],
                "sl": 100.0, "mult": 1.0, "symbol": "TESTUSDT", "source": "fake",
                "base": "TEST", "side": "SHORT", "msg_ms": T0 + 60000}
    positions = {}
    sim._resolve_deferred(deferred, positions, {}, {})
    trades_all = sim.trades
    skips = [t for t in trades_all if t.get("status") == "被更新信号取代(未成交)"]
    shorts = [t for t in trades_all if t.get("side") == "SHORT" and t.get("legs")]
    assert skips or not shorts, \
        f"被取代的预成交不得建仓, 实际 SHORT 仓: {[(t.get('status'), t.get('signal_ts_ms')) for t in shorts]}; trades={[(t.get('status'), t.get('signal_ts_ms')) for t in trades_all]}"
