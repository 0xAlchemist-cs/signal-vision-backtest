# -*- coding: utf-8 -*-
"""第六轮审查的场景回归测试 (v0.2.6)。

P0: voice 走查起点 i0 = lw // 60_000 把"纪元分钟数"当数组下标 —
合成测试数据(时间戳从 0 开始)恰好掩盖该错误; 真实 Unix 时间戳下
起点是 ~29,000,000 而 klines 数组只有几千根 → 循环永不执行 →
事件推进与尾部回放全部失效, TP/SL 永不触发。
回归测试必须用真实 Unix 时间戳, 覆盖 事件推进 + 尾部回放 两条路径。
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BASE = 1_775_808_000_000   # 真实纪元毫秒(2026-04), 分钟对齐: BASE % 60_000 == 0


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def engine_mod(tmp_path_factory):
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r7_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r7", "backtest/engine.py")
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
# P0-A: 真实时间戳 + 分钟中间管理事件 → 尾部回放必须从 lw 所在分钟继续
# ---------------------------------------------------------------------------

def test_real_unix_ts_manage_event_walk_tp_not_skipped():
    ve = _load("v_r7a", "backtest/voice_event_engine.py")
    """be_set 在 +01:30(分钟中间), TP tick 在 +01:40。lw=+01:30 → 走查
    起点必须用 bisect 回到 +01:00 bar 的真实下标; 旧代码
    i0 = lw//60_000 ≈ 29,596,801 > len(kl)=3 → 循环空转 → TP 永久丢失。"""
    kl = [
        [BASE, 100, 100, 100, 100.0],
        [BASE + 60_000, 100, 110, 95, 105.0],   # +01:00: 40s tick=110 触及 TP
        [BASE + 120_000, 105, 105, 105, 105.0],
    ]
    evs = [
        {"ts_ms": BASE + 59_999, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 90.0, "tp": 110.0},            # 市价成交 = +00:00 末 100
        {"ts_ms": BASE + 90_000, "kind": "manage", "ev": "be_set"},   # +01:30
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"], "真实时间戳下 TP 必须触发 (旧 bug: 走查起点=纪元分钟数)"
    last = res["legs"][-1]
    assert last["reason"] == "TP", f"实际 {last}"
    assert last["exit"] == pytest.approx(110.0)
    assert last["ts"] == BASE + 100_000, "TP 应在 +01:40 tick 成交"


# ---------------------------------------------------------------------------
# P0-B: 真实时间戳 + 无后续事件 → 尾部回放必须走到出场
# ---------------------------------------------------------------------------

def test_real_unix_ts_tail_walk_reaches_exit():
    ve = _load("v_r7b", "backtest/voice_event_engine.py")
    """信号之后再无事件: 数据末尾回放同样用 lw//60_000 当下标 →
    循环空转 → 持仓永远不出场 → legs 为空。"""
    kl = [
        [BASE, 100, 100, 100, 100.0],
        [BASE + 60_000, 100, 110, 95, 105.0],
        [BASE + 120_000, 105, 105, 105, 105.0],
    ]
    evs = [{"ts_ms": BASE + 15_000, "kind": "signal", "side": "BUY",
            "entry": None, "sl": 90.0, "tp": 110.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"], "数据末尾回放必须触发 TP (旧 bug: 尾部起点=纪元分钟数)"
    last = res["legs"][-1]
    assert last["reason"] == "TP"
    assert last["exit"] == pytest.approx(110.0)
    assert last["ts"] == BASE + 100_000


# ---------------------------------------------------------------------------
# P0-C: 真实时间戳 → 事件驱动推进本身必须能触发 TP(管理事件之前)
# ---------------------------------------------------------------------------

def test_real_unix_ts_event_walk_triggers_tp_before_manage():
    ve = _load("v_r7c", "backtest/voice_event_engine.py")
    """+01:40 的 TP tick 在 +01:50 的 be_set 之前 — 事件驱动的 FILLED
    推进循环必须用真实下标走到它。旧代码循环空转 → TP 被漏掉,
    持仓错误存活到 be_set 之后。"""
    kl = [
        [BASE, 100, 100, 100, 100.0],
        [BASE + 60_000, 100, 120, 95, 105.0],   # +01:00: 40s tick=120 触及 TP110
        [BASE + 120_000, 105, 105, 105, 105.0],
    ]
    evs = [
        {"ts_ms": BASE + 59_999, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 90.0, "tp": 110.0},
        {"ts_ms": BASE + 110_000, "kind": "manage", "ev": "be_set"},   # +01:50
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"], "事件驱动推进必须触发 TP (旧 bug: 推进起点=纪元分钟数)"
    leg = res["legs"][0]
    assert leg["reason"] == "TP"
    assert leg["exit"] == pytest.approx(110.0)
    assert leg["ts"] == BASE + 100_000, "TP 应在 +01:40 tick 成交, 早于 +01:50 的 be_set"


# ---------------------------------------------------------------------------
# P1 (顺带发现): PENDING 挂单不得被 FILLED 走查幻影平仓
# (尾部回放 316 行早有 pending 守卫, 事件推进 156 行漏了 — 不一致)
# ---------------------------------------------------------------------------

def test_pending_limit_not_phantom_closed_by_walk():
    ve = _load("v_r7d", "backtest/voice_event_engine.py")
    ve.PATH_MODE = "high_first"
    """SELL LIMIT 99 挂单: bar1(事件所在 bar, 未完成)同时触及挂单价(low 99)
    与 SL(high 105) — 成交确认的模糊窗口内 dispatch 不确认成交(保守),
    但旧代码随后对 PENDING 仓位跑 FILLED 走查 → 幻影 SL 平仓。
    正确行为: 模糊窗口内不确认成交 → 新信号到达 = 撤旧挂单, 无盈亏。"""
    kl = [
        [0, 100, 100, 100, 100.0],          # bar0: 不触及 99
        [60_000, 100, 105, 99, 100.0],      # bar1: low 99 触及挂单价 + high 105 触及 SL
        [120_000, 100, 100, 100, 100.0],
    ]
    evs = [
        {"ts_ms": 0, "kind": "signal", "side": "SELL", "entry": 99.0,
         "sl": 105.0, "tp": 90.0},
        {"ts_ms": 90_000, "kind": "signal", "side": "BUY", "entry": None,
         "sl": 80.0, "tp": 130.0},           # bar1 未完成时到达 → 撤旧挂单 + 开新仓
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    phantom = [l for l in res["legs"]
               if l["side"] == "SELL" and l["reason"] in ("SL", "BE")]
    assert not phantom, f"未成交挂单被走查幻影平仓: {phantom}"
    assert res["legs"], "新 BUY 市价信号应有成交后的腿"


# ---------------------------------------------------------------------------
# 审查二 (P1): 开仓手续费必须按**显式成交状态**计, 不能由出场时间推断
# ---------------------------------------------------------------------------

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


def test_open_fee_charged_for_filled_leg_at_end_of_data(engine_mod):
    """已成交 + 数据截止(出场 ts_ms=None): 开仓费必须计入。
    旧实现由出场时间推断 cancelled_before_fill → 出场 ts 全为 None 时
    误判为未成交 → 只收平仓费 (假阳性: 只断言 fee>0 测不出来)。"""
    sim = _mk_sim(engine_mod, [[60_000, 100, 100.5, 99.5, 100.0]])
    positions, last_sig = {}, {}
    sim._handle_open(_mkt_sig(60_000), positions, last_sig, {})
    positions["TEST"]["legs"][0]["exits"].append(
        {"price": 100.0, "frac": 1.0, "ts_ms": None})
    sim._finish(positions["TEST"])
    t = sim.trades[-1]
    leg = t["legs"][0]
    qty = engine_mod.RISK_USDT * leg["risk_share"] / leg["risk_unit"]
    close_fee = qty * 100.0 * 1.0 * engine_mod.FEE_RATE
    open_fee = qty * leg["entry"] * engine_mod.FEE_RATE
    assert t["fee_usdt"] == pytest.approx(round(open_fee + close_fee, 2)), \
        f"开仓费 {open_fee} + 平仓费 {close_fee} 都必须计, 实际 {t['fee_usdt']}"
    assert t["fee_usdt"] > round(close_fee, 2), \
        "开仓费不得被跳过(旧假阳性: 只剩平仓费时 fee>0 也通过)"


def test_cancelled_before_fill_leg_zero_fee(engine_mod):
    """换新撤旧: 未成交腿(成交 tick 在新信号之后)不收开仓费;
    已成交腿正常收开仓费+出场费 — 两腿费用分开核对。"""
    sim = _mk_sim(engine_mod, [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 91, 89, 90.0],       # 触及 LIMIT 90 → 腿1 成交@90
        [120_000, 90, 95, 79, 80.0],       # 新信号后才会触及 LIMIT 80 → 腿2 被撤
    ])
    positions, last_sig = {}, {}
    sig1 = {"id": "a", "ts_ms": 60_000, "text": "", "symbol": "TESTUSDT",
            "side": "LONG", "order_type": "LIMIT", "entry_price": 90.0,
            "entry_prices": [90.0, 80.0], "sl": 82.0, "tp_levels": [110.0],
            "stop_trigger_type": None, "stop_timeframe": None}
    sim._handle_open(sig1, positions, last_sig, {})
    assert "TEST" in positions and len(positions["TEST"]["legs"]) == 2
    sim._handle_open(_mkt_sig(120_000, entry=80.0, sl=75.0), positions, last_sig, {})
    cancelled = [t for t in sim.trades
                 if any(l.get("cancelled_before_fill") for l in t["legs"])]
    assert cancelled, "被撤的未成交腿必须显式标记 cancelled_before_fill"
    t = cancelled[0]
    live = [l for l in t["legs"] if not l.get("cancelled_before_fill")]
    assert live, "腿1(已成交)应保留"
    qty = engine_mod.RISK_USDT * live[0]["risk_share"] / live[0]["risk_unit"]
    expect = qty * live[0]["entry"] * engine_mod.FEE_RATE          # 开仓费
    for ep in live[0]["exits"]:
        expect += qty * ep["price"] * ep["frac"] * engine_mod.FEE_RATE
    assert t["fee_usdt"] == pytest.approx(round(expect, 2)), \
        f"只有已成交腿计费, 实际 {t['fee_usdt']} 期望 {expect}"


# ---------------------------------------------------------------------------
# 审查三 (P1): PARTIAL_TP 的 percent 口径必须显式 (original|remaining)
# ---------------------------------------------------------------------------

def _partial_mg(ts_ms, percent=50):
    return {"ts_ms": ts_ms,
            "item": {"symbol": "TESTUSDT",
                     "params": {"action_type": "PARTIAL_TP", "percent": percent}},
            "text": ""}


def test_partial_tp_percent_basis_remaining(engine_mod):
    """remaining 口径: 每次平**当前剩余**的 50% → 两次喊单剩 25%。
    旧实现固定 original 口径且无口径配置 → 两次喊单剩 0 (红)。"""
    engine_mod.PERCENT_BASIS = "remaining"
    sim = _mk_sim(engine_mod, [
        [60_000, 100, 100.5, 99.5, 100.0],
        [120_000, 100, 100.5, 99.5, 100.0],
    ])
    positions, last_sig = {}, {}
    sim._handle_open(_mkt_sig(60_000), positions, last_sig, {})
    pos = positions["TEST"]
    tick = sim.mkt.tick_price_at("TESTUSDT", "fake", 70_000)
    sim._apply_manage(pos, _partial_mg(70_000), tick)
    sim._apply_manage(pos, _partial_mg(130_000), tick)
    leg = pos["legs"][0]
    assert leg["remain_frac"] == pytest.approx(0.25), \
        f"remaining 口径两次 50% 应剩 25%, 实际 {leg['remain_frac']}"
    assert sum(ep["frac"] for ep in leg["exits"]) == pytest.approx(0.75)


def test_partial_tp_percent_basis_original(engine_mod):
    """original 口径(默认=已发布回测口径): 每次平**原始仓位**的 50%
    → 两次喊单全平。钉住默认行为不漂移。"""
    engine_mod.PERCENT_BASIS = "original"
    sim = _mk_sim(engine_mod, [
        [60_000, 100, 100.5, 99.5, 100.0],
        [120_000, 100, 100.5, 99.5, 100.0],
    ])
    positions, last_sig = {}, {}
    sim._handle_open(_mkt_sig(60_000), positions, last_sig, {})
    pos = positions["TEST"]
    tick = sim.mkt.tick_price_at("TESTUSDT", "fake", 70_000)
    sim._apply_manage(pos, _partial_mg(70_000), tick)
    sim._apply_manage(pos, _partial_mg(130_000), tick)
    assert pos["legs"][0]["remain_frac"] == pytest.approx(0.0)
    assert pos.get("force_closed"), "original 口径两次 50% 应全平"


# ---------------------------------------------------------------------------
# 审查四 (P0): 孤儿管理消息按真实时刻进入事件流 — 不依赖后续开仓信号认领
# ---------------------------------------------------------------------------

def test_orphan_manages_after_last_signal_applied(engine_mod):
    """10:00 开仓成交 → 10:05 移保本(无币种) → 10:10 全平(无币种) →
    之后没有新开仓信号。旧实现只在遍历开仓信号时认领孤儿 →
    最后一条信号之后的管理消息全部丢失 (红)。"""
    sim = _mk_sim(engine_mod, [
        [60_000, 100, 105, 101, 104.0],    # low 101: BE 止损(100.05)之上, 不误触
        [120_000, 104, 106, 103, 105.0],
        [180_000, 105, 106, 104, 105.0],
    ])
    signals = [_mkt_sig(60_000)]
    manages = [
        {"ts_ms": 65_000, "items": [{"params": {"action_type": "MOVE_BE"}}],
         "text": "move sl to be"},
        {"ts_ms": 125_000, "items": [{"params": {"action_type": "CLOSE_ALL"}}],
         "text": "close all"},
    ]
    sim.run(signals, manages)
    assert sim.trades, "应有交易记录"
    t = sim.trades[-1]
    last_exit = t["exit_parts"][-1]
    assert last_exit["reason"] == "喊单全平", \
        f"信号之后的孤儿管理消息必须执行, 实际 {last_exit['reason']}"
    assert last_exit["ts_ms"] == 125_000, \
        f"全平应发生在消息时刻, 实际 {last_exit['ts_ms']}"


# ---------------------------------------------------------------------------
# 审查五 (P1): voice 数据截止平仓时刻 = 最后一根 bar 的收盘, 且 >= 开仓时刻
# ---------------------------------------------------------------------------

def test_voice_end_of_data_exit_ts_not_before_fill():
    ve = _load("v_r7e", "backtest/voice_event_engine.py")
    """分钟中间市价成交(+00:40) + 单根 bar: 旧实现把数据截止平仓记在
    bar 开盘(+00:00) — 早于开仓时刻。必须记在 bar 收盘(+00:59.999)。"""
    kl = [[BASE, 100, 100, 100, 100.0]]
    evs = [{"ts_ms": BASE + 30_000, "kind": "signal", "side": "BUY",
            "entry": None, "sl": 90.0, "tp": 110.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] and res["legs"][0]["reason"] == "end_of_data"
    for leg in res["legs"]:
        assert leg["ts"] == BASE + 59_999, \
            f"数据截止平仓应记在最后一根 bar 收盘, 实际 {leg['ts']}"
        assert leg["ts"] >= leg["fill_ts"], \
            f"出场时间不得早于开仓时间: exit={leg['ts']} fill={leg['fill_ts']}"
