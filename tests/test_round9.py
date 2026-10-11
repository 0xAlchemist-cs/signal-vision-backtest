# -*- coding: utf-8 -*-
"""第八轮审查的场景回归测试 (v0.2.9)。

主题: 订单归属与数量守恒。两条长期不变量:
  1. 出场数量总和 <= 实际成交数量
  2. 任何管理消息必须属于一个有效的持仓生命周期
"""
import importlib.util
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
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r9_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r9", "backtest/engine.py")
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
# 审查一 (高): 换仓契约 = replace_on_fill — 新单未成交不得提前关闭旧仓
# ---------------------------------------------------------------------------

def test_replace_on_fill_unfilled_stop_keeps_old(engine_mod):
    """旧 LONG 持有 → 新 SHORT STOP 95 从未触发 → 旧仓必须原样存活。
    旧实现在撮合新单**之前**就撤掉旧仓 = 系统性提前平仓 (红)。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 100, 99, 100.0],      # 旧 LONG 建仓后价格横盘, 不触及 95
        [120_000, 100, 100, 99, 100.0],
    ])
    positions, last_sig = {}, {}
    sim._handle_open(_mkt_sig(60_000, "LONG", 100.0, 90.0), positions, last_sig, {})
    assert "TEST" in positions
    sig2 = {"id": "b", "ts_ms": 130_000, "text": "", "symbol": "TESTUSDT",
            "side": "SHORT", "order_type": "STOP", "entry_price": 95.0,
            "entry_prices": [95.0], "sl": 100.0, "tp_levels": [85.0],
            "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig2, positions, last_sig, {})
    assert "TEST" in positions, "新 STOP 未触发, 旧仓不得被提前关闭"
    assert positions["TEST"]["side"] == "LONG", \
        f"旧仓应保持 LONG, 实际 {positions.get('TEST', {}).get('side')}"
    flips = [t for t in sim.trades if any(
        ep.get("reason") == "换新撤旧" for l in t.get("legs", []) for ep in l.get("exits", []))]
    assert not flips, "未成交的新信号不得触发换新撤旧"


def test_replace_on_fill_filled_stop_closes_old(engine_mod):
    """对照: 新 STOP 实际触发成交 → 旧仓按换新撤旧关闭, 新仓建立。
    (钉住 replace_on_fill 的正向路径)"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 100, 99, 100.0],
        [120_000, 100, 100, 93, 95.0],      # low 93 ≤ 95 → SHORT STOP 触发
    ])
    positions, last_sig = {}, {}
    sim._handle_open(_mkt_sig(60_000, "LONG", 100.0, 90.0), positions, last_sig, {})
    sig2 = {"id": "b", "ts_ms": 130_000, "text": "", "symbol": "TESTUSDT",
            "side": "SHORT", "order_type": "STOP", "entry_price": 95.0,
            "entry_prices": [95.0], "sl": 100.0, "tp_levels": [85.0],
            "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig2, positions, last_sig, {})
    closed = [t for t in sim.trades if t.get("side") == "LONG"]
    assert closed and any(ep.get("reason") == "换新撤旧"
                          for l in closed[0].get("legs", []) for ep in l.get("exits", [])), \
        "新单成交后旧 LONG 应按换新撤旧关闭"
    assert "TEST" in positions and positions["TEST"]["side"] == "SHORT", \
        "新 SHORT 仓应建立"


# ---------------------------------------------------------------------------
# 审查二 (P0): 前一 setup 的遗留管理消息不得作用于新仓
# ---------------------------------------------------------------------------

def test_stale_manage_does_not_kill_next_position(engine_mod):
    """10:00 LONG 建仓 → 10:02 止损离场 → 10:03 CLOSE_ALL(未消费) →
    10:05 同标的再开仓。旧管理消息必须失效, 新仓不得被 force_closed (红)。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 100, 94, 96.0],       # pos1: low 94 ≤ SL95 → 10:02 止损
        [120_000, 96, 100, 95, 99.0],
        [180_000, 99, 100, 98, 99.0],       # pos2 建仓后横盘
    ])
    signals = [_mkt_sig(60_000), _mkt_sig(180_000, sl=90.0)]   # sl 不同 → 不触发去重
    manages = [{"ts_ms": 130_000,
                "items": [{"symbol": "TESTUSDT",
                           "params": {"action_type": "CLOSE_ALL"}}],
                "text": "close all"}]
    sim.run(signals, manages)
    closed = [t for t in sim.trades if t.get("status") in ("closed", "open")
              and t.get("signal_ts_ms") == 180_000]
    assert closed, "第二笔仓应有交易记录"
    t = closed[0]
    reasons = [ep.get("reason") for l in t.get("legs", []) for ep in l.get("exits", [])]
    assert not t.get("force_closed"), \
        f"前一 setup 的遗留 CLOSE_ALL 不得 force_closed 新仓: {reasons}"
    assert "喊单全平" not in reasons, f"遗留消息被误执行: {reasons}"


# ---------------------------------------------------------------------------
# 审查三 (P0): 每条腿独立校验成交价与止损的方向关系
# ---------------------------------------------------------------------------

def test_per_leg_stop_direction_validation(engine_mod):
    """LONG 双腿 [95, 90], SL 92: 限价均低于现价(不会即时成交), bar1 low 89
    同时触及两腿。平均价 92.5 > 92 整体通过, 但腿 90 已越过止损 —
    abs() 掩盖方向错误 → 旧实现建出 2 条腿 (红)。正确: 剔除无效腿。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 99, 89, 100.0],       # low 89: 同时触及 95 腿与 90 腿
    ])
    positions, last_sig = {}, {}
    sig = {"id": "a", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "LIMIT", "entry_price": 95.0,
           "entry_prices": [95.0, 90.0], "sl": 92.0, "tp_levels": [105.0],
           "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig, positions, last_sig, {})
    assert "TEST" in positions, "存在有效腿(95)时不得整单丢弃"
    legs = positions["TEST"]["legs"]
    assert len(legs) == 1, f"越止损的 90 腿必须剔除, 实际 {[(l['entry'], l['risk_unit']) for l in legs]}"
    assert legs[0]["entry"] == pytest.approx(95.0)
    # 不变量: 每条腿的成交价与止损方向关系成立
    for l in legs:
        assert l["entry"] > 92.0, "LONG 腿成交价必须高于止损"


# ---------------------------------------------------------------------------
# 审查四 (P1): 无有效持仓可平时, TP 档不得被空消耗
# ---------------------------------------------------------------------------

def test_tp_not_consumed_without_live_legs(engine_mod):
    """双腿 [90,80]: 90 腿成交后被喊单止盈 100% 平掉, 80 腿尚未成交;
    价格触及 TP95 — 无任何腿可平, TP 不得标记 done。之后 80 腿成交再触
    TP95 → 仍应触发 (旧实现: TP 已 done, 腿 80 永远无法止盈, 红)。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 91, 89, 90.0],        # 90 腿成交 @100s
        [120_000, 90, 97, 85, 90.0],        # 13s 喊单平100% → 16s tick 97 触 TP(无腿可平)
        [180_000, 85, 95, 79, 85.0],        # 20s: 80 腿成交; 22s: 触 TP95 → 应触发
    ])
    positions, last_sig = {}, {}
    sig = {"id": "a", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
           "side": "LONG", "order_type": "LIMIT", "entry_price": 90.0,
           "entry_prices": [90.0, 80.0], "sl": 75.0, "tp_levels": [95.0],
           "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig, positions, last_sig, {})
    pos = positions["TEST"]
    tick = sim.mkt.tick_price_at("TESTUSDT", "fake", 130_000)
    sim._apply_manage(pos, {"ts_ms": 130_000,
                            "item": {"symbol": "TESTUSDT",
                                     "params": {"action_type": "PARTIAL_TP",
                                                "percent": 100}},
                            "text": "tp 100%"}, tick)
    sim._advance_all(positions, 999_999_999)
    assert "TEST" not in positions or pos.get("force_closed") or \
        all(l["remain_frac"] <= 1e-9 for l in pos["legs"]), \
        "80 腿成交后触及 TP95 应全平离场"
    t = sim.trades[-1]
    tp_exits = [ep for l in t.get("legs", []) for ep in l.get("exits", [])
                if str(ep.get("reason", "")).startswith("TP")]
    assert tp_exits, \
        f"TP 档不得在无持仓可平时被空消耗, 80 腿应能触发 TP95; 实际 exits " \
        f"{[ep.get('reason') for l in t.get('legs', []) for ep in l.get('exits', [])]}"


# ---------------------------------------------------------------------------
# 审查五 (P1): 不变量 — 出场数量总和 <= 实际成交数量 (R 阶梯超额平仓)
# ---------------------------------------------------------------------------

def test_r_ladder_frac_capped_at_remainder(engine_mod):
    """GR 配置 R1=70% + R2=70%: 出场总和不得超过 100% (数量守恒不变量)。
    旧实现直接减 R2_FRAC → 累计出场 140%, remain_frac 为负 (红)。"""
    engine_mod.R1_FRAC = 0.70
    engine_mod.R2_FRAC = 0.70
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 106, 100, 105.0],     # high 106 ≥ R1(105) → 平 70%
        [120_000, 105, 111, 104, 110.0],    # high 111 ≥ R2(110) → 旧: 再平 70% = 140%
    ], rule="r_ladder")
    positions, last_sig = {}, {}
    sim._handle_open(_mkt_sig(60_000, "LONG", 100.0, 95.0), positions, last_sig, {})
    sim._advance_all(positions, 999_999_999)
    for t in sim.trades:
        for l in t.get("legs", []):
            total = sum(ep["frac"] for ep in l.get("exits", []))
            assert total <= 1.0 + 1e-9, \
                f"不变量破坏: 出场数量总和 {total} > 实际成交 1.0"
            assert l["remain_frac"] >= -1e-9, \
                f"remain_frac 不得为负, 实际 {l['remain_frac']}"


# ---------------------------------------------------------------------------
# 审查六 (P2): voice ignore_same_dir 契约钉住 —
#   同向已成交持仓 → 忽略新信号; 同向未成交订单 → 允许换新
# ---------------------------------------------------------------------------

def test_voice_ignore_same_dir_filled_position():
    ve = _load("v_r9a", "backtest/voice_event_engine.py")
    """同向信号 vs 已成交持仓 → 忽略(不换新), 原仓继续走到出场。"""
    kl = [[0, 100, 100, 100, 100.0], [60_000, 100, 100, 100, 100.0]]
    evs = [
        {"ts_ms": 0, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 90.0, "tp": 110.0},
        {"ts_ms": 30_000, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 85.0, "tp": 120.0},   # 同向, 应被忽略
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01,
                    on_new_signal="ignore_same_dir")
    res = ve.run(kl, evs, cfg)
    assert not [l for l in res["legs"] if l["reason"] == "signal_flip"], \
        "同向信号对已成交持仓不得换新"
    assert len(res["legs"]) == 1, f"同向新信号应被忽略, 实际 {res['legs']}"


def test_voice_ignore_same_dir_pending_replaced():
    ve = _load("v_r9b", "backtest/voice_event_engine.py")
    """同向信号 vs 未成交挂单 → 允许换新(撤旧挂新)。契约: ignore_same_dir
    只保护**已成交持仓**, 挂单始终可被同向新信号替换。"""
    kl = [[0, 100, 100, 100, 100.0], [60_000, 100, 100, 100, 100.0]]
    evs = [
        {"ts_ms": 0, "kind": "signal", "side": "SELL", "entry": 90.0,
         "sl": 95.0, "tp": 80.0},                    # 挂单(数据内永不触及)
        {"ts_ms": 30_000, "kind": "signal", "side": "SELL", "entry": 85.0,
         "sl": 90.0, "tp": 75.0},                    # 同向新挂单 → 换新
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01,
                    on_new_signal="ignore_same_dir")
    res = ve.run(kl, evs, cfg)
    assert not [l for l in res["legs"] if l["reason"] == "signal_flip"], \
        "挂单换新不产生盈亏"
    assert res["legs"] == [], "两笔挂单均未成交, 最终无盈亏腿(换新=撤旧挂新)"
