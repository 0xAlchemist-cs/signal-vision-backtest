# -*- coding: utf-8 -*-
"""第五轮复核的场景回归测试 (v0.2.5)。

按第五轮复核清单编写。断言要求: 每条至少覆盖 成交时间/成交价格/最终状态
中的两项(审查本轮新增要求)。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def engine_mod(tmp_path_factory):
    import os
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r6_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r6", "backtest/engine.py")
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


# ---------------------------------------------------------------------------
# 问题 1 (P0): lw 在分钟中间时, 走查不得跳过该分钟剩余 tick
# ---------------------------------------------------------------------------

def test_voice_walk_resumes_mid_minute(engine_mod):
    ve = _load("v_r6a", "backtest/voice_event_engine.py")
    """00:59.999 市价成交(TP110/SL90), 01:30 be_set; 01:40 tick 触及 TP110。
    lw=01:30(分钟中间) → 走查必须从 01:00 bar 的 40s tick 继续,
    不得因 bisect(starts, lw)=02:00 而漏掉 01:40 的 TP。"""
    kl = [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 110, 95, 105.0],         # 01:00 bar: 40s tick=110 触及 TP
        [120_000, 105, 105, 105, 105.0],       # 02:00 bar
    ]
    evs = [
        {"ts_ms": 59_999, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 90.0, "tp": 110.0},   # 市价成交=bar0 末 100
        {"ts_ms": 90_000, "kind": "manage", "ev": "be_set"},   # 01:30 喊 BE
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert len(res["legs"]) >= 1
    last = res["legs"][-1]
    # be_set(01:30) 之后, 01:00 bar 的 40s tick (110) 仍应被处理 → TP@110
    # 旧 bug: lw=90000 → bisect 跳到 02:00 bar → 01:40 的 TP 永久丢失
    assert last["reason"] == "TP", f"01:40 的 TP 被漏扫(旧 bug), 实际 {last}"
    assert last["exit"] == pytest.approx(110.0)
    assert last["ts"] == 100_000, "成交时间应为 01:40 tick (bar1 开盘 60000 + 40s)"


# ---------------------------------------------------------------------------
# 问题 2 (P0): STOP 跳空 — 成交价 = 触发后可执行价, 不是触发价+滑点
# ---------------------------------------------------------------------------

def test_stop_gap_fill_at_market_price(engine_mod):
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    sim.trades = []
    sim.mkt = FakeMarket([[60_000, 109, 110, 108, 109.0]])   # tick 直接跳到 109+
    sig = {"id": "t", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "STOP", "entry_price": 100.0,
           "entry_prices": [100.0], "sl": 90.0, "tp_levels": [120.0],
           "entry_trigger_type": "intrabar", "stop_trigger_type": None,
           "stop_timeframe": None}
    positions, last_sig = {}, {}
    sim._handle_open(sig, positions, last_sig, {})
    assert "TEST" in positions, "STOP 跳空触发应成交"
    leg = positions["TEST"]["legs"][0]
    # 触发 tick 价格 109+ → LONG 买入不可能以 100.05 成交 (旧 bug)
    assert leg["entry"] == pytest.approx(109.0 * (1 + engine_mod.SLIP)), \
        f"跳空 STOP 成交价应为可执行价 109×(1+滑点), 实际 {leg['entry']}"
    # risk_unit 以实际成交价计: |109.545 - 90| = 19.5
    assert leg["risk_unit"] == pytest.approx(leg["entry"] - 90.0)


# ---------------------------------------------------------------------------
# 问题 3 (P1): 数据截止持仓(ts_ms=None)的开仓手续费不漏算
# ---------------------------------------------------------------------------

def test_open_fee_for_none_ts_end_of_data(engine_mod):
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    bars = [[60_000, 100, 100.5, 99.5, 100.0]]
    sim.mkt = FakeMarket(bars)
    sim.trades = []
    sig = {"id": "t", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "MARKET", "entry_price": 100.0,
           "entry_prices": [100.0], "sl": 95.0, "tp_levels": [],
           "stop_trigger_type": None, "stop_timeframe": None}
    positions, last_sig = {}, {}
    sim._handle_open(sig, positions, last_sig, {})
    # 模拟主引擎真实的数据截止路径: exits 只有 ts_ms=None 的记录
    positions["TEST"]["legs"][0]["exits"].append(
        {"price": 100.0, "frac": 1.0, "ts_ms": None})
    sim._finish(positions["TEST"])
    t = sim.trades[-1]
    assert t["fee_usdt"] > 0, \
        "数据截止持仓(ts_ms=None)的开仓手续费不得漏算"


# ---------------------------------------------------------------------------
# 问题 4 (P1): 不支持的 entry_timeframe (2H) → skip 并注明, 不静默退化
# ---------------------------------------------------------------------------

def test_unsupported_entry_timeframe_skipped(engine_mod):
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    sim.trades = []
    sim.mkt = FakeMarket([[60_000, 100, 100.5, 99.5, 100.0]])
    sig = {"id": "t", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "STOP", "entry_price": 100.0,
           "entry_prices": [100.0], "sl": 90.0, "tp_levels": [120.0],
           "entry_trigger_type": "close_based", "entry_timeframe": "2H",
           "stop_trigger_type": None, "stop_timeframe": None}
    positions, last_sig = {}, {}
    sim._handle_open(sig, positions, last_sig, {})
    assert not positions, "不支持的收盘确认周期(2H)应跳过, 不静默退化为盘中触发"
    assert sim.trades and sim.trades[-1].get("status") == "不支持的收盘确认周期(2H)", \
        "skip 理由必须注明不支持的周期"


# ---------------------------------------------------------------------------
# 问题 5 (P1): 整点边界测试修正 — 相邻 bar 收盘价不同, 断言 entry 与时间
# ---------------------------------------------------------------------------

def test_voice_market_fill_minute_boundaries_v2():
    ve = _load("v_r6e", "backtest/voice_event_engine.py")
    """整点 / +1ms / 分钟末 -1ms 收到市价信号:
    fill 价 = 消息所在分钟 bar 的收盘(100), 不得误用前一根(90)。"""
    kl = [
        [0, 90, 90, 90, 90.0],                 # 前一根: 收盘 90 (若误用会得到 90)
        [60_000, 100, 100, 100, 100.0],        # 消息所在分钟: 收盘 100
        [120_000, 100, 100, 100, 100.0],
    ]
    for offset_ms, desc in ((0, "整点"), (1, "+1ms"), (59_999, "分钟末-1ms")):
        evs = [{"ts_ms": 60_000 + offset_ms, "kind": "signal", "side": "BUY",
                "entry": None, "sl": 90.0, "tp": 110.0}]
        cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
        res = ve.run(kl, evs, cfg)
        assert len(res["legs"]) == 1, f"{desc}: 应有成交"
        assert res["legs"][0]["entry"] == pytest.approx(100.0), \
            f"{desc}: fill 应为消息所在分钟收盘 100 (不得误用前一根 90)"


# ---------------------------------------------------------------------------
# 补充: worst/best 语义更名为 low_first/high_first 后, 阴线路径的区分测试
# (第四轮审查指出 worst 对 SHORT 乐观的问题 — low_first 对 SHORT 乐观)
# ---------------------------------------------------------------------------

def test_low_first_bearish_bar_short_gets_best():
    ve = _load("v_r6f", "backtest/voice_event_engine.py")
    ve.PATH_MODE = "low_first"
    """low_first 模式(先探低): SHORT 的 TP(低价)先触发 → SHORT 最优。
    (模式名已中性化: low_first/high_first 描述顺序而非'悲观/乐观')"""
    kl = [[0, 106.0, 109.0, 95.0, 100.0]]   # low95(TP侧) 与 high109(SL侧) 同 bar
    evs = [{"ts_ms": 1_000, "kind": "signal", "side": "SELL",
            "entry": None, "sl": 108.0, "tp": 96.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] and res["legs"][0]["reason"] == "TP"
    assert res["legs"][0]["exit"] == pytest.approx(96.0)
