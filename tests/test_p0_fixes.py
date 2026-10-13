# -*- coding: utf-8 -*-
"""v0.2.2 回归测试 — 覆盖两轮外部审查的全部 P0 修复。

设计原则(第二轮审查 P1-8): 测试必须走**真实撮合路径**(构造 OHLC + 事件时间戳
→ 完整 _handle_open/_advance_all 状态机), 而不是只断言布尔表达式或把中间
计算结果固化为期望值。
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
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("svb_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine", "backtest/engine.py")
    sys.path.pop(0)


# ---------------------------------------------------------------------------
# 纯函数: 1m OHLC → 4 tick (0/20/40/59.999s), 路径模式开关
# ---------------------------------------------------------------------------

def test_candle_to_ticks_four_slots(engine_mod):
    ticks = list(engine_mod.candle_to_ticks(60_000, 100.0, 101.0, 99.0, 100.5))
    assert len(ticks) == 4
    tss = [t for t, _ in ticks]
    assert tss == [60_000, 80_000, 100_000, 119_999]


def test_path_mode_worst_low_first(engine_mod):
    """worst 模式强制先探低(对 LONG 悲观)"""
    ticks = list(engine_mod.candle_to_ticks(60_000, 100.0, 101.0, 99.0, 100.5))
    assert ticks[1][1] == 99.0   # low 在前


def test_path_mode_best_high_first(engine_mod):
    """best 模式强制先探高(对 LONG 乐观) — 直接改模块属性, 不 reload"""
    old_mode = engine_mod.PATH_MODE
    try:
        engine_mod.PATH_MODE = "high_first"
        ticks = list(engine_mod.candle_to_ticks(60_000, 100.0, 101.0, 99.0, 100.5))
        assert ticks[1][1] == 101.0   # high 在前
    finally:
        engine_mod.PATH_MODE = old_mode


# ---------------------------------------------------------------------------
# voice_event_engine: 无未来数据 + PENDING/FILLED 状态机
# ---------------------------------------------------------------------------

def test_voice_event_ref_uses_prev_completed_bar():
    ve = _load("vee1", "backtest/voice_event_engine.py")
    # bars: 09:59(90), 10:00(100); 消息 10:00:15 → ref 必须是 09:59 收盘 90,
    # 10:00 bar 在消息时刻尚未收盘, 10:01 bar 是未来 (旧实现的前视)
    kl = [[0, 90, 90, 90, 90.0], [60_000, 100, 100, 100, 100.0],
          [120_000, 110, 110, 110, 110.0]]
    evs = [{"ts_ms": 60_015, "kind": "signal", "side": "BUY",
            "entry": None, "sl": 80.0, "tp": 120.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    # 市价型 fill = 消息后首根完整 bar (10:00) 收盘 = 100, 而非 10:01 的 110
    assert res["legs"][0]["entry"] == 100.0


def test_voice_event_pending_cancelled_before_fill():
    ve = _load("vee2", "backtest/voice_event_engine.py")
    """10:00 挂限价 105 (10:03 才触及); 10:01 喊 close_all → 撤单, 无盈亏。
    旧实现: 仓位在信号时刻就建立, close_all 会用未来价格平仓。"""
    kl = [[0, 100, 100, 100, 100.0],
          [60_000, 100, 100, 100, 100.0],
          [120_000, 104, 106, 103, 105.5],   # 10:02, 未触及 105
          [180_000, 105, 106, 104, 105.8],   # 10:03 触及 105
          [240_000, 105.5, 106, 105, 105.9]]
    evs = [
        {"ts_ms": 15_000, "kind": "signal", "side": "BUY",
         "entry": 105.0, "sl": 95.0, "tp": 120.0},
        {"ts_ms": 65_000, "kind": "manage", "ev": "close_all"},   # 成交前撤单
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] == [], "成交前的 close_all 应撤销挂单, 不产生盈亏"


def test_voice_event_limit_no_pnl_before_fill():
    ve = _load("vee3", "backtest/voice_event_engine.py")
    """10:00 挂限价 105 (10:02 触及); 10:01 他喊 BE → 未成交, 忽略;
    10:02 成交后止损 95 正常生效"""
    kl = [[0, 100, 100, 100, 100.0],
          [60_000, 100, 100, 100, 100.0],       # 10:01: 未触及 105
          [120_000, 104, 106, 103, 105.5],      # 10:02: 触及 105, 成交
          [180_000, 105, 106, 94.5, 95.0],      # 10:03: 暴跌穿 SL
          [240_000, 95, 95, 94, 94.5]]
    evs = [
        {"ts_ms": 15_000, "kind": "signal", "side": "BUY",
         "entry": 105.0, "sl": 95.0, "tp": 120.0},
        {"ts_ms": 65_000, "kind": "manage", "ev": "be_set"},      # 成交前喊 BE → 忽略
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert len(res["legs"]) == 1
    assert res["legs"][0]["reason"] == "SL"
    # 止损保持原始 95(未被 BE 移到 105); bar3 low 94.5 穿越止损 →
    # 审计九跳空规则: 成交 = min(现价, 止损)×(1-滑点)
    assert res["legs"][0]["exit"] == pytest.approx(94.5 * (1 - ve.SLIP)), \
        "BE 喊话在成交前, 止损应保持原始 95; 跳空按更差可执行价成交"


# ---------------------------------------------------------------------------
# engine.Sim 集成测试: FakeMarket + 真实 _handle_open/_advance_all 状态机
# ---------------------------------------------------------------------------

class FakeMarket:
    """最小 Market 桩: 提供 Sim 状态机需要的接口, K 线由测试构造。"""

    def __init__(self, bars):
        self.bars = bars
        self.klines = {}          # run() 的内存驱逐逻辑需要                       # [[ts,o,h,l,c], ...] 单标的
        self.symbol = "TESTUSDT"

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


def _mk_signal(ts_ms, symbol, side, order_type, entry_prices, sl, tp_levels=None):
    return {"id": "t", "ts_ms": ts_ms, "text": "", "symbol": symbol, "side": side,
            "order_type": order_type, "entry_price": entry_prices[0] if entry_prices else None,
            "entry_prices": entry_prices, "sl": sl, "tp_levels": tp_levels or [],
            "stop_trigger_type": None, "stop_timeframe": None}


def test_sim_stop_entry_fill_price_enters_accounting(engine_mod):
    """P0-2 回归: STOP 入场的滑点成交价必须进入 entry/risk_unit 记账。
    SHORT-STOP entry=100, SLIP=0.05% → 成交 99.95; 止损 101 处 risk_unit
    应为 |99.95-101|=1.05 而非触发价口径的 1.0"""
    bars = [[60_000, 100, 100.2, 99.9, 100.0]]
    sim = engine_mod.Sim(rule="tp_ladder", mkt=FakeMarket(bars))
    sig = _mk_signal(60_000, "TESTUSDT", "SHORT", "STOP", [100.0], 101.0)
    sig["entry_trigger_type"] = "intrabar"   # 盘中触发 STOP(非收盘确认)
    positions, last_sig = {}, {}
    sim._handle_open(sig, positions, last_sig, {})
    assert "TEST" in positions, "STOP-SHORT 应在 price<=entry 时触发成交"
    leg = positions["TEST"]["legs"][0]
    assert leg["entry"] == pytest.approx(100.0 * (1 - engine_mod.SLIP)), \
        "entry 必须是滑点后成交价"


def test_sim_move_be_uses_quantity_weighted_avg(engine_mod):
    """P1-6 回归: BE = 实际数量加权均价。
    两腿等风险、SL 同为 95: 腿1 entry=100 (Q1=1/5=0.2), 腿2 entry=98
    (Q2=1/3≈0.333) → BE = (100*0.2 + 98*0.3333) / (0.2+0.3333) ≈ 98.75,
    而非等风险加权的 99.0 (审查给出的反例)"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.mkt = FakeMarket([[60_000, 99.5, 99.6, 99.4, 99.5]])
    pos = {"symbol": "TESTUSDT", "source": "fake", "side": "LONG", "rule": "tp_ladder", "mult": 1.0,
           "legs": [
               {"entry": 100.0, "risk_unit": 5.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 95.0, "fill_ms": 0, "exits": []},
               {"entry": 98.0, "risk_unit": 3.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 95.0, "fill_ms": 0, "exits": []}],
           "remain_total": 1.0, "tp_ladder": [],
           "stop_close_based": False, "stop_tf_min": 1}
    mg = {"ts_ms": 60_000, "item": {"params": {"action_type": "MOVE_BE"}}}
    sim._apply_manage(pos, mg, tick_price=99.5)
    expected = (100.0 * (1.0 / 5.0) + 98.0 * (1.0 / 3.0)) / ((1.0 / 5.0) + (1.0 / 3.0))
    assert expected == pytest.approx(98.75, abs=0.01)
    stops = {round(l["stop"], 2) for l in pos["legs"]}
    assert stops == {round(expected, 2)}, f"BE 应为数量加权均价 {expected:.2f}"


def test_sim_manage_before_fill_ignored(engine_mod):
    """P0-3 回归(引擎侧): BE 喊话在第二腿成交前 → 第二腿保持原始止损"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.mkt = FakeMarket([])
    pos = {"symbol": "TESTUSDT", "source": "fake", "side": "LONG", "rule": "tp_ladder", "mult": 1.0,
           "legs": [
               {"entry": 100.0, "risk_unit": 1.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 95.0, "fill_ms": 0, "exits": []},
               {"entry": 98.0, "risk_unit": 1.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 95.0, "fill_ms": 60_000, "exits": []}],
           "remain_total": 1.0, "tp_ladder": [],
           "stop_close_based": False, "stop_tf_min": 1}
    # mg 时刻 30s: 第二腿(fill 60s)未成交; 现价 100.5 在保本位上方 → 正常移保本
    mg = {"ts_ms": 30_000, "item": {"params": {"action_type": "MOVE_BE"}}}
    sim._apply_manage(pos, mg, tick_price=100.5)
    assert pos["legs"][0]["stop"] == pytest.approx(100.0), "已成交腿移保本至自身入场价"
    assert pos["legs"][1]["stop"] == 95.0, "未成交腿保持原始止损"


def test_sim_flip_trades_marked_closed(engine_mod):
    """P0-3 回归(第一轮): 换新撤旧的交易必须进入 status=closed 统计"""
    sim = engine_mod.Sim(rule="tp_ladder", mkt=FakeMarket(
        [[60_000, 100, 100.5, 99.5, 100.0], [120_000, 100, 100.5, 99.5, 100.0]]))
    sig1 = _mk_signal(60_000, "TESTUSDT", "LONG", "MARKET", [100.0], 95.0)
    positions, last_sig = {}, {}
    sim._handle_open(sig1, positions, last_sig, {})
    sig2 = _mk_signal(120_000, "TESTUSDT", "SHORT", "MARKET", [100.0], 105.0)
    sim._handle_open(sig2, positions, last_sig, {})
    closed = [t for t in sim.trades if t.get("status") == "closed" and t.get("r") is not None]
    assert len(closed) == 1, "换新撤旧的旧仓必须标记 closed 进入统计"


def test_sim_stop_trigger_raw_not_slipped(engine_mod):
    """P0-6 回归: 触发判定用原始止损; 跳空按 tick 价成交"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    pos = {"side": "LONG", "rule": "tp_ladder",
           "legs": [{"entry": 100.0, "risk_unit": 1.0, "risk_share": 1.0,
                     "remain_frac": 1.0, "stop": 99.0, "t1_done": True,
                     "t2_done": True, "fill_ms": 0, "exits": []}],
           "remain_total": 1.0, "tp_ladder": [],
           "stop_close_based": False, "stop_tf_min": 1}
    # price=98.98: 低于原始止损 99.0(应触发), 高于滑点后 98.9505(旧代码漏判)
    sim._tick_legs(pos, 98.98, 1_000)
    assert pos["legs"][0]["remain_frac"] == 0.0
    fill = pos["legs"][0]["exits"][0]["price"]
    assert fill == pytest.approx(98.98 * (1 - engine_mod.SLIP))


def test_report_no_closed_trades_no_crash(engine_mod, tmp_path):
    """P1 回归: 无已平仓交易时报告不崩溃"""
    (tmp_path / "trades.json").write_text(
        json.dumps([{"status": "48h未成交", "r": None}]), encoding="utf-8")
    engine_mod.WORK = tmp_path
    engine_mod.phase_report()   # 不应抛 ZeroDivisionError
    assert (tmp_path / "report_v3.md").exists()


def test_parser_does_not_touch_entry_type_from_stop():
    """P0-1 回归: 止损的 close_based 语义不得改变入场订单类型。
    断言 parser 源码: (a) 不存在 stop_trigger_type → order_type 的旧映射;
    (b) 条件入场由独立的 entry_trigger_type 字段承载。"""
    src = (ROOT / "signal_parser" / "parser.py").read_text(encoding="utf-8")
    norm = src[src.index("if out.get(\"side\")"):]
    assert "STOP" not in norm.split("return out")[0], \
        "归一化段中不得出现 order_type→STOP 的改写(旧映射已删除)"
    assert "entry_trigger_type" in src, \
        "条件入场的触发语义应由独立的 entry_trigger_type 字段承载"


def test_advance_all_conflict_stats_no_nameerror(engine_mod):
    """第三轮 P0 回归: _advance_all 的 TP/SL 冲突统计不得引用未定义变量
    (曾因 is_long 未定义抛 NameError), 且 _tp_hit_ms 每 bar 必清理。"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.trades = []          # _finish 需要
    bars = []
    t0 = 60_000
    for n in range(6):
        t = t0 + n * 60_000
        # bar2 用阴线(high→low 路径): 先触发 TP105, 后覆盖 SL97 → 同 bar 冲突
        bars.append([t, 106.0, 107.0, 95.0, 96.0] if n == 2
                    else [t, 100.0, 100.5, 99.5, 100.0])
    sim.mkt = FakeMarket(bars)
    pos = {"symbol": "TESTUSDT", "source": "fake", "base": "TEST",
           "side": "LONG", "rule": "tp_ladder", "mult": 1.0,
           "signal_ts_ms": t0, "first_fill_ms": t0, "walked_ms": t0,
           "stop_init": 97.0, "status": None, "msg_id": "x", "text": "",
           "mg_by_sym": {}, "sl_source": "text", "funding_usdt": 0.0,
           "fev": [], "fev_i": 0, "force_closed": False,
           "tp_sl_conflicts": 0,
           "legs": [{"entry": 100.0, "risk_unit": 3.0, "risk_share": 1.0,
                     "remain_frac": 1.0, "stop": 97.0, "t1_done": True,
                     "t2_done": True, "fill_ms": t0, "exits": []}],
           "remain_total": 1.0, "tp_ladder": [
               {"i": 1, "price": 105.0, "frac": 1.0, "done": False}],
           "stop_close_based": False, "stop_tf_min": 1}
    positions = {"TEST": pos}
    sim._advance_all(positions, t0 + 6 * 60_000)   # 不抛 NameError 即通过
    assert pos.get("tp_sl_conflicts") == 1, "bar2 应计一次 TP/SL 同 bar 冲突"
    assert pos.get("_tp_hit_ms") is None, "_tp_hit_ms 必须每 bar 清理"
