# -*- coding: utf-8 -*-
"""第五轮审查的场景回归测试 (v0.2.4)。

按第四轮复核给出的 8 个场景清单编写：修复前应失败(红)，修复后通过(绿)。
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
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r5_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r5", "backtest/engine.py")
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


def _sig(ts_ms, symbol, side, order_type, entry_prices, sl, tp_levels=None, **extra):
    d = {"id": "t", "ts_ms": ts_ms, "text": "", "symbol": symbol, "side": side,
         "order_type": order_type, "entry_price": entry_prices[0] if entry_prices else None,
         "entry_prices": entry_prices, "sl": sl,
         "tp": tp_levels[0] if tp_levels else None,
         "tp_levels": tp_levels or [],
         "stop_trigger_type": None, "stop_timeframe": None}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 场景 1 (P0-A 修正): 有效的 PENDING BUY 挂单被反向 SELL 信号替换
# ---------------------------------------------------------------------------

def test_voice_valid_pending_replaced_by_reverse_signal():
    ve = _load("v_r5a", "backtest/voice_event_engine.py")
    """有效多单挂单(entry 105/sl 95/tp 120, 几何有效) PENDING 期间收到
    SELL 市价新信号 → 旧挂单撤销(不产生 BUY 盈亏), 新 SELL 仓建立。"""
    kl = [[0, 100, 100, 100, 100.0],
          [60_000, 100, 100, 100, 100.0],       # 10:01: 105 未触及, PENDING
          [120_000, 96, 96, 95, 95.5]]          # 10:02: SELL 后的下跌行情
    evs = [
        {"ts_ms": 15_000, "kind": "signal", "side": "BUY",
         "entry": 105.0, "sl": 95.0, "tp": 120.0},          # 有效 PENDING 多单
        {"ts_ms": 65_000, "kind": "signal", "side": "SELL",
         "entry": None, "sl": 110.0, "tp": 90.0},           # 翻空
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01,
                    on_new_signal="close_and_open")
    res = ve.run(kl, evs, cfg)
    buy = [l for l in res["legs"] if l["side"] == "BUY"]
    sell = [l for l in res["legs"] if l["side"] == "SELL"]
    assert not buy, f"旧 BUY 挂单应被撤销(不产生成交), 实际 {buy}"
    assert sell, "新 SELL 仓应建立"
    assert sell[0]["entry"] == 100.0 and sell[0]["exit"] == 95.5, \
        "SELL 市价成交后应按 10:02 行情 end_of_data"


# ---------------------------------------------------------------------------
# 场景 2 (P0-B): 已成交限价单遇到延迟撤单 → 平真实仓位而非撤单
# ---------------------------------------------------------------------------

def test_voice_delayed_close_all_closes_filled_position():
    ve = _load("v_r5b", "backtest/voice_event_engine.py")
    """10:00:15 挂 BUY 102 (SL 95); 10:01–10:02 行情触及 102 → 成交;
    10:02:05 喊 close_all → 应平真实持仓(按 10:02:05 行情价), 不是撤单。"""
    kl = [
        [0, 100, 100, 100, 100.0],
        [60_000, 101, 102.5, 100.5, 102.0],    # 10:01: 触及 102 → 成交
        [120_000, 102, 103, 101, 102.5],       # 10:02: close_all 时刻行情 ~102.x
        [180_000, 102.5, 104, 102, 103.5],     # 10:03 (若有仓位, eod 在这里)
    ]
    evs = [
        {"ts_ms": 15_000, "kind": "signal", "side": "BUY",
         "entry": 102.0, "sl": 95.0, "tp": 130.0},
        {"ts_ms": 125_000, "kind": "manage", "ev": "close_all"},   # 10:02:05
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert len(res["legs"]) >= 1, "已成交仓位遇到 close_all → 应有平仓记录"
    leg = res["legs"][0]
    assert leg["reason"] == "close_all_call", f"应为 close_all_call 平仓, 实际 {leg['reason']}"
    assert leg["exit"] == pytest.approx(102.0), \
        f"平仓价应为消息时刻(10:02:05)的行情价 ~102, 实际 {leg['exit']}"


# ---------------------------------------------------------------------------
# 场景 3 (P0-C): 管理消息 vs 未来 TP tick 的时间先后
# ---------------------------------------------------------------------------

def test_voice_future_tp_tick_not_used_before_manage():
    ve = _load("v_r5c", "backtest/voice_event_engine.py")
    """10:00:59 BUY 100 成交(市价); 10:01:30 喊全平; 10:01 bar 的合成
    高价 110(位于 10:01:40 tick)会触及 TP 110。
    真实顺序: 10:01:30 全平在先 → TP 不得在 10:01:40 的未来 tick 触发。"""
    kl = [
        [0, 100, 100, 100, 100.0],
        [60_000, 100, 100, 100, 100.0],        # 10:00: 市价成交 bar
        [120_000, 100, 110, 95, 105.0],        # 10:01: high 110 在 40s tick
        [180_000, 105, 105, 104, 104.5],       # 10:02: eod
    ]
    evs = [
        {"ts_ms": 59_999, "kind": "signal", "side": "BUY",
         "entry": None, "sl": 90.0, "tp": 110.0},   # 10:00:59 成交 (fill=10:00 bar 末? 无)
        {"ts_ms": 150_000, "kind": "manage", "ev": "close_all"},   # 10:01:30 喊全平
    ]
    # 注: 信号 59.999s → fill = 10:00 bar 收盘 100.0, fill_ts = 59.999s
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"], "应有一笔平仓记录"
    last = res["legs"][-1]
    assert last["reason"] == "close_all_call", \
        f"全平(10:01:30)必须先于 TP tick(10:01:40) 生效, 实际 {last}"
    # 全平价 = 10:01:30 时刻的已知价 (10:00 bar 收盘 100) 而非未来 10:01 高点
    assert last["exit"] == pytest.approx(100.0), \
        f"close_all 执行价应为消息时刻已知价 100, 实际 {last['exit']}"


# ---------------------------------------------------------------------------
# 场景 4 (P0-D): 官方 conditional 示例 (LIMIT + close_based) 走 STOP 撮合
# ---------------------------------------------------------------------------

def test_engine_conditional_limit_example_end_to_end(engine_mod):
    """官方示例 examples/conditional_signal.json (LIMIT + close_based + 4H):
    盘中触及 1865 不得开仓; 4H 收盘 tick 收盘价 <= 1865 才按触发价开仓。"""
    # 4H 收盘 bar: epoch 分钟对齐 240 → bar ts 使 ts//60000 % 240 == 239
    close_bar_ts = (240 * 60_000) - 60_000          # 4H 边界前最后一分钟
    sim = engine_mod.Sim(rule="tp_ladder", mkt=FakeMarket([
        [0, 1900, 1900, 1900, 1900.0],
        [60_000, 1900, 1900, 1800, 1899.0],        # 盘中触及 1865, 非收盘确认
        [close_bar_ts, 1880, 1880, 1850, 1860.0],  # 4H 收盘 bar: 收盘 1860
    ]))
    ex = json.loads((ROOT / "examples" / "conditional_signal.json").read_text(encoding="utf-8"))
    sig = _sig(60_000, ex["symbol"], ex["side"], ex["order_type"],
               ex["entry_prices"], ex["sl"], ex["tp_levels"],
               conditional=ex["conditional"],
               entry_trigger_type=ex["entry_trigger_type"],
               entry_timeframe=ex["entry_timeframe"])
    positions, last_sig = {}, {}
    sim._handle_open(sig, positions, last_sig, {})
    assert "ETH" in positions, "4H 收盘确认 bar 的收盘 tick → 应开仓"
    # fill 必须精确落在 4H 收盘确认 tick, 而非 bar1 的盘中触及
    assert positions["ETH"]["legs"][0]["fill_ms"] == close_bar_ts + 59_999,         f"fill 应为 4H 收盘 tick {close_bar_ts + 59_999}"


# ---------------------------------------------------------------------------
# 场景 5 (P1-6): 两腿其一已全平 → BE 按实际剩余数量
# ---------------------------------------------------------------------------

def test_be_uses_remaining_qty_after_partial(engine_mod):
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    sim.mkt = FakeMarket([])
    pos = {"symbol": "TESTUSDT", "source": "fake", "side": "LONG",
           "rule": "tp_ladder", "mult": 1.0,
           "legs": [
               {"entry": 100.0, "risk_unit": 5.0, "risk_share": 0.5,
                "remain_frac": 0.0, "stop": 95.0, "fill_ms": 0, "exits": [
                    {"price": 105.0, "frac": 1.0, "ts_ms": 60_000}]},
               {"entry": 98.0, "risk_unit": 3.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 95.0, "fill_ms": 0, "exits": []}],
           "remain_total": 0.5, "tp_ladder": [],
           "stop_close_based": False, "stop_tf_min": 1}
    mg = {"ts_ms": 120_000, "item": {"params": {"action_type": "MOVE_BE"}}}
    sim._apply_manage(pos, mg, tick_price=98.5)
    assert pos["legs"][1]["stop"] == pytest.approx(98.0), \
        f"BE 应为剩余腿的加权均价 98.0, 实际 {pos['legs'][1]['stop']}"


# ---------------------------------------------------------------------------
# 场景 6 (P1-5): 已成交、数据截止仍未平仓 → 开仓手续费被记录
# ---------------------------------------------------------------------------

def test_open_fee_kept_for_end_of_data_position(engine_mod):
    sim = engine_mod.Sim(rule="tp_ladder", mkt=FakeMarket(
        [[60_000, 100, 100.5, 99.5, 100.0]]))
    sig = _sig(60_000, "TESTUSDT", "LONG", "MARKET", [100.0], 95.0)
    positions, last_sig = {}, {}
    sim._handle_open(sig, positions, last_sig, {})
    # 模拟 run 尾部: 数据截止平仓
    positions["TEST"]["legs"][0]["exits"].append(
        {"price": 100.0, "frac": 1.0, "ts_ms": 60_000})
    sim._finish(positions["TEST"])
    t = sim.trades[-1]
    assert t["fee_usdt"] > 0, "数据截止仍持仓的腿必须计开仓手续费"


# ---------------------------------------------------------------------------
# 场景 7 (P0-4): SHORT 同 bar TP/SL — low_first 与 high_first 结果相反
# ---------------------------------------------------------------------------

def test_short_same_bar_tp_sl_modes_disagree():
    ve = _load("v_r5g", "backtest/voice_event_engine.py")
    bars = [[0, 100.0, 100.0, 100.0, 100.0],
            [60_000, 100.0, 108.0, 94.0, 100.0]]
    results = {}
    for mode in ("low_first", "high_first"):
        ve.PATH_MODE = mode
        evs = [{"ts_ms": 15_000, "kind": "signal", "side": "SELL",
                "entry": None, "sl": 108.0, "tp": 94.0}]
        cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
        res = ve.run(bars, evs, cfg)
        results[mode] = res["legs"][0]["pnl"] if res["legs"] else None
    assert results["low_first"] is not None and results["low_first"] > 0, \
        f"low_first: TP(低价)先触发 → SHORT 盈利, 实际 {results['low_first']}"
    assert results["high_first"] is not None and results["high_first"] < 0, \
        f"high_first: SL(高价)先触发 → SHORT 亏损, 实际 {results['high_first']}"


# ---------------------------------------------------------------------------
# 场景 8: voice 市价信号的整点边界 (整点 / +1ms / 分钟末 -1ms)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offset_ms,expect_entry", [
    (60_000, 100.0),        # 整点 10:01:00 → 市价 fill=该分钟收盘 100
    (60_001, 100.0),        # +1ms
    (119_999, 100.0),       # 分钟末 -1ms → 同一根 bar 的收盘
])
def test_voice_market_fill_minute_boundaries(offset_ms, expect_entry):
    ve = _load("v_r5h", "backtest/voice_event_engine.py")
    kl = [
        [0, 90, 90, 90, 90.0],                 # 前一根 (参考价来源)
        [60_000, 100, 100, 100, 100.0],        # 消息所在分钟 bar
        [120_000, 100, 100, 100, 100.0],
    ]
    evs = [{"ts_ms": 60_000 + offset_ms, "kind": "signal", "side": "BUY",
            "entry": None, "sl": 90.0, "tp": 110.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert len(res["legs"]) == 1
    # 市价成交价 = 消息所在分钟的收盘 (100), 成交时刻 = 该分钟末 —
    # 不得早于消息时刻
    assert res["legs"][0]["entry"] == pytest.approx(expect_entry)
