# -*- coding: utf-8 -*-
"""第十轮审计的三信号时序测试 (v0.2.11)。

因果不变量: 任何旧仓的推进/结算不得越过下一个全局事件时刻。
A(10:01 建仓) → B(10:06 SHORT STOP, 11:00 才触发) → C(10:11 市价)。
正确: C 在 A 仍存活时(10:11)替换 A; B 未成交被 C 取代, 不得开仓。
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
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r11_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r11", "backtest/engine.py")
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


def test_three_signals_c_replaces_alive_a_and_supersedes_b(engine_mod):
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    sim.mkt = FakeMarket([
        [BASE, 100, 100, 100, 100.0],
        [BASE + 60_000, 100, 100, 99, 100.0],      # 10:01 A 建仓
        [BASE + 120_000, 100, 100, 99, 100.0],
        [BASE + 660_000, 100, 100, 99, 100.0],     # 10:12 C 建仓 bar
        [BASE + 1_740_000, 96, 96, 94, 95.0],      # 10:30 — A 若存活到此处 = 因果错误
        [BASE + 3_540_000, 96, 96, 89, 90.0],      # 11:00 B 的原定触发
    ])
    sim._fetch_funding = lambda *a, **k: []

    sig_a = {"id": "a", "ts_ms": BASE + 60_000, "text": "", "symbol": "TESTUSDT",
             "side": "LONG", "order_type": "MARKET", "entry_price": 100.0,
             "entry_prices": [100.0], "sl": 95.0, "tp_levels": [],
             "stop_trigger_type": None, "stop_timeframe": None}
    sig_b = {"id": "b", "ts_ms": BASE + 300_000, "text": "", "symbol": "TESTUSDT",
             "side": "SHORT", "order_type": "STOP", "entry_price": 90.0,
             "entry_prices": [90.0], "sl": 100.0, "tp_levels": [85.0],
             "stop_trigger_type": None, "stop_timeframe": None}
    sig_c = {"id": "c", "ts_ms": BASE + 600_000, "text": "", "symbol": "TESTUSDT",
             "side": "LONG", "order_type": "MARKET", "entry_price": 100.0,
             "entry_prices": [100.0], "sl": 85.0, "tp_levels": [],
             "stop_trigger_type": None, "stop_timeframe": None}
    sim.run([sig_a, sig_b, sig_c], [])

    # A: 必须在 C 成交时刻(10:12)被换新撤旧, 不得活到 10:30 的止损
    a = [t for t in sim.trades if t.get("signal_ts_ms") == BASE + 60_000 and t.get("legs")]
    assert a, "A 应有交易记录"
    a = a[0]
    a_exits = [(ep.get("reason"), ep.get("ts_ms"))
               for l in a.get("legs", []) for ep in l.get("exits", [])]
    assert any(r == "换新撤旧" and ts == BASE + 660_000 for r, ts in a_exits), \
        f"A 应在 10:12 被 C 换新撤旧, 实际 {a_exits}"
    assert not any(str(r).startswith("止损") for r, _ in a_exits), \
        f"A 不得活到 10:30 的止损(B 处理时跳到了未来): {a_exits}"

    # B: 未成交且被 C 取代 → 不得开仓 (无 legs 的 skip 记录即可)
    b_opened = [t for t in sim.trades
                if t.get("side") == "SHORT" and t.get("legs")]
    assert not b_opened, \
        f"B(10:06) 在 C(10:11) 取代后不得于 11:00 开仓: {[t.get('status') for t in b_opened]}"

    # C: 正常建立并存活到数据截止
    c = [t for t in sim.trades if t.get("signal_ts_ms") == BASE + 600_000 and t.get("legs")]
    assert c and c[0]["side"] == "LONG", "C 应正常开仓"
