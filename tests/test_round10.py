# -*- coding: utf-8 -*-
"""第九轮审计的场景回归测试 (v0.2.10)。

主题: 历史事件的时间因果关系 —
  P0  replace_on_fill 不得用**未来成交时刻**提前结算旧仓
  P1  无效腿剔除后, first_fill_ms / TP 参考价 / 风险份额不得引用被剔除腿
  P1  voice 跳空止损按更差的可执行价成交 (与主引擎同规则)
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
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r10_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r10", "backtest/engine.py")
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


def _mk_sim(engine_mod, bars, rule="tp_ladder"):
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.rule = rule
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
# 审计九-1 (P0): replace_on_fill 的时间因果 —
# 旧仓必须在新单成交时刻**之前**正常接受止损/止盈/管理, 不得被未来时刻倒填
# ---------------------------------------------------------------------------

def test_replace_on_fill_old_position_walks_to_stop_first(engine_mod):
    """10:01 旧 LONG@100 SL95 → 10:06 新 SHORT STOP 90 → 10:29 旧仓应止损
    → 10:59 新 STOP 才触发。旧实现用 10:59 的未来时刻+价格直接结算旧仓,
    10:29 的止损被跳过 (红)。正确: 旧仓先正常推进 → 止损离场, 无换新撤旧。"""
    sim = _mk_sim(engine_mod, [
        [BASE, 100, 100, 100, 100.0],
        [BASE + 60_000, 100, 100, 99, 100.0],        # 10:01 旧 LONG 建仓
        [BASE + 120_000, 100, 100, 99, 100.0],
        [BASE + 1_740_000, 100, 100, 94, 95.0],      # 10:29 low 94 ≤ SL95 → 应止损
        [BASE + 3_540_000, 96, 96, 89, 90.0],        # 10:59 low 89 ≤ 90 → 新 STOP 触发
    ])
    signals = [
        _mkt_sig(BASE + 60_000, "LONG", 100.0, 95.0),
        {"id": "b", "ts_ms": BASE + 300_000, "text": "", "symbol": "TESTUSDT",
         "side": "SHORT", "order_type": "STOP", "entry_price": 90.0,
         "entry_prices": [90.0], "sl": 100.0, "tp_levels": [85.0],
         "stop_trigger_type": None, "stop_timeframe": None},
    ]
    sim.run(signals, [])
    old = [t for t in sim.trades if t.get("signal_ts_ms") == BASE + 60_000
           and t.get("legs")]
    assert old, "旧仓应有交易记录"
    old = old[0]
    reasons = [ep.get("reason", "") for l in old.get("legs", [])
               for ep in l.get("exits", [])]
    fills = [ep.get("ts_ms") for l in old.get("legs", [])
             for ep in l.get("exits", [])]
    assert not any("换新撤旧" in r for r in reasons), \
        f"旧仓应在 10:29 止损而非被未来时刻的换新结算: {list(zip(reasons, fills))}"
    assert any(r.startswith("止损") for r in reasons), \
        f"10:29 的止损必须发生: {list(zip(reasons, fills))}"
    sl_exits = [ep for l in old.get("legs", []) for ep in l.get("exits", [])
                if str(ep.get("reason", "")).startswith("止损")]
    assert sl_exits[0]["ts_ms"] == BASE + 1_780_000, \
        f"止损应在 10:29:40 tick, 实际 {sl_exits[0]['ts_ms']}"
    # 新 SHORT 正常建立
    new = [t for t in sim.trades if t.get("signal_ts_ms") == BASE + 300_000
           and t.get("legs")]
    assert new and new[0]["side"] == "SHORT", "新 SHORT 应正常开仓"


# ---------------------------------------------------------------------------
# 审计九-2 (P1): 无效腿剔除后, first_fill_ms / TP 参考 / 风险份额重算
# ---------------------------------------------------------------------------

def test_filtered_legs_recompute_time_tp_ref_and_share(engine_mod):
    """LONG STOP [92, 100], SL95, TP98: 现价 93 → 92 腿立即触发但成交 93.05
    越过止损 → 剔除; 100 腿 10:40 tick 成交。旧实现保留无效腿的
    first_fill_ms(10:01) / TP 参考(93.05) / 份额 0.5 → TP98 不被跳过,
    有效腿@100 在 98 被'止盈'出亏损单 (红)。"""
    sim = _mk_sim(engine_mod, [
        [0, 93, 93, 93, 93.0],
        [60_000, 93, 99, 92.5, 98.0],      # 92 腿即时触发@93.05(无效); 100 未触及
        [120_000, 98, 101, 97, 100.0],     # 100 腿 @160s tick 成交
    ])
    positions, last_sig = {}, {}
    sig = {"id": "a", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "STOP", "entry_price": 92.0,
           "entry_prices": [92.0, 100.0], "sl": 95.0, "tp_levels": [98.0],
           "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig, positions, last_sig, {})
    assert "TEST" in positions
    pos = positions["TEST"]
    assert pos["first_fill_ms"] == 160_000, \
        f"first_fill_ms 必须来自有效腿, 实际 {pos['first_fill_ms']}"
    assert len(pos["legs"]) == 1 and pos["legs"][0]["risk_share"] == pytest.approx(1.0), \
        f"风险份额应重算到有效腿, 实际 {[(l['entry'], l['risk_share']) for l in pos['legs']]}"
    assert pos["tp_ladder"][0]["done"], \
        "TP98 低于有效腿入场价 100 → 初始化时必须跳过"
    sim._advance_all(positions, 999_999_999)
    assert "TEST" in positions, "TP 被跳过 → 持仓不得被 98 '止盈' 离场"
    leg_exits = [ep for l in positions["TEST"]["legs"] for ep in l.get("exits", [])]
    assert not [ep for ep in leg_exits if str(ep.get("reason", "")).startswith("TP")], \
        f"有效腿@100 不得在 98 被'止盈'出亏损单: {leg_exits}"


# ---------------------------------------------------------------------------
# 审计九-3 (P1): voice 跳空止损按更差的可执行价成交 (与主引擎同规则)
# ---------------------------------------------------------------------------

def test_voice_gap_stop_long_fills_at_market():
    ve = _load("v_r10a", "backtest/voice_event_engine.py")
    """LONG SL95, 行情跳空到 90 → 成交 ≈ 90×(1-滑点), 不是 95。
    旧实现按止损价 95 结算, 每单位亏损被低估 (红)。"""
    kl = [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 100, 90, 95.0],      # 40s tick 跳空到 90
    ]
    evs = [{"ts_ms": 59_999, "kind": "signal", "side": "BUY",
            "entry": None, "sl": 95.0, "tp": 110.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] and res["legs"][0]["reason"] == "SL"
    leg = res["legs"][0]
    assert leg["exit"] == pytest.approx(90.0 * (1 - ve.SLIP)), \
        f"跳空止损应按可执行价 90×(1-滑点), 实际 {leg['exit']}"


def test_voice_gap_stop_short_fills_at_market():
    ve = _load("v_r10b", "backtest/voice_event_engine.py")
    """SHORT SL105, 行情跳空到 110 → 成交 ≈ 110×(1+滑点), 不是 105。"""
    kl = [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 110, 100, 105.0],    # 40s tick 跳空到 110
    ]
    evs = [{"ts_ms": 59_999, "kind": "signal", "side": "SELL",
            "entry": None, "sl": 105.0, "tp": 90.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] and res["legs"][0]["reason"] == "SL"
    leg = res["legs"][0]
    assert leg["exit"] == pytest.approx(110.0 * (1 + ve.SLIP)), \
        f"SHORT 跳空止损应按 110×(1+滑点), 实际 {leg['exit']}"


def test_voice_gap_stop_tail_walk_same_rule():
    ve = _load("v_r10c", "backtest/voice_event_engine.py")
    """尾部回放(数据末尾走查)同样适用跳空规则 — 成交更差者。"""
    kl = [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 100, 90, 95.0],
    ]
    evs = [{"ts_ms": 0, "kind": "signal", "side": "BUY",
            "entry": None, "sl": 95.0, "tp": 110.0}]   # 无后续事件 → 尾部走查
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] and res["legs"][0]["reason"] == "SL"
    assert res["legs"][0]["exit"] == pytest.approx(90.0 * (1 - ve.SLIP)), \
        f"尾部走查跳空止损应按可执行价, 实际 {res['legs'][0]['exit']}"
