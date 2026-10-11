# -*- coding: utf-8 -*-
"""第四轮审查的场景回归测试 (v0.2.3)。

8 个场景按外部审查清单编写；修复前应失败(红)，修复后通过(绿)。
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
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("r4_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine_r4", "backtest/engine.py")
    sys.path.pop(0)


class FakeMarket:
    def __init__(self, bars):
        self.bars = bars
        self.klines = {}          # run() 的内存驱逐逻辑需要
        self.symbol = "TESTUSDT"

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


def _sig(ts_ms, symbol, side, order_type, entry_prices, sl, tp_levels=None):
    # voice 引擎契约读单值 "tp"; 主引擎契约读 "tp_levels" — 两者都给
    return {"id": "t", "ts_ms": ts_ms, "text": "", "symbol": symbol, "side": side,
            "order_type": order_type, "entry_price": entry_prices[0] if entry_prices else None,
            "entry_prices": entry_prices, "sl": sl,
            "tp": tp_levels[0] if tp_levels else None,
            "tp_levels": tp_levels or [],
            "stop_trigger_type": None, "stop_timeframe": None}


# ---------------------------------------------------------------------------
# 场景 1 (P0-1): 25s 移损、30s 推进截止 → 指令不得从队列消失
# ---------------------------------------------------------------------------

def test_manage_not_lost_when_until_cuts_bar(engine_mod):
    """bar00 的 tick 序列 0/20/40/59.999s; 25s 的 MOVE_BE 需在 40s tick 执行,
    但 30s 的下一条信号把 until_ms 截到 30s → 旧实现弹出消息后 tick 40s 被
    t1 过滤, 指令永久丢失。修复后: 未执行的消息留回队列, 数据末尾前仍生效。"""
    bars = []
    t0 = 60_000
    for n in range(8):
        t = t0 + n * 60_000
        # bar0: tick40s=100.5 (BE 移损时点上方), tick59.999=99.95 (跌破 BE)
        bars.append([t, 100.0, 100.5, 99.95, 99.95] if n == 0
                    else [t, 100.2, 100.4, 100.1, 100.2])
    sim = engine_mod.Sim(rule="tp_ladder", mkt=FakeMarket(bars))
    sig_a = _sig(t0 + 20_000, "TESTUSDT", "LONG", "MARKET", [100.0], 99.0)
    mg = {"ts_ms": t0 + 25_000,
          "items": [{"symbol": "TESTUSDT", "params": {"action_type": "MOVE_BE"}}]}
    sig_b = _sig(t0 + 30_000, "TEST2USDT", "LONG", "MARKET", [200.0], 198.0)
    sim.run([sig_a, sig_b], [mg])
    # A 仓的 BE 必须生效(消息不丢)。他喊 BE 时价格 99.95 已跌破保本位 100
    # → 正确语义 = "移损越价即平"(市价小亏平仓); 旧 bug 下消息丢失,
    # A 仓会存活到数据末(出场=数据截止平仓)。
    a_legs = [t for t in sim.trades if t.get("symbol") == "TESTUSDT"
              and t.get("r") is not None]
    assert a_legs, "A 仓应有成交记录"
    reasons = [e["reason"] for t in a_legs for e in t.get("exit_parts", [])]
    assert any("移损越价即平" in r or "止损" in r for r in reasons), \
        f"25s 的 MOVE_BE 被截断丢失(旧 bug): 出场链 {reasons}"
    assert not any("数据截止" in r for r in reasons), \
        f"A 仓不应存活到数据截止: {reasons}"


# ---------------------------------------------------------------------------
# 场景 2 (P0-2): PENDING 买单期间收到新 SELL 信号 → 撤旧单、处理新单
# ---------------------------------------------------------------------------

def test_voice_new_signal_cancels_pending(engine_mod):
    ve = _load("ve_r4", "backtest/voice_event_engine.py")
    kl = [[0, 100, 100, 100, 100.0],
          [60_000, 100, 100, 100, 100.0],       # 10:01: BUY105 未触及
          [120_000, 105, 106, 104, 105.5],      # 10:02: 触及 105 (旧单若存活)
          [180_000, 96, 96, 95, 95.5]]          # 10:03: SELL 新单后的行情
    evs = [
        {"ts_ms": 15_000, "kind": "signal", "side": "BUY",
         "entry": 105.0, "sl": 115.0, "tp": 90.0},          # PENDING
        {"ts_ms": 65_000, "kind": "signal", "side": "SELL",
         "entry": None, "sl": 110.0, "tp": 90.0},           # 10:01 翻空(市价)
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01,
                    on_new_signal="close_and_open")
    res = ve.run(kl, evs, cfg)
    sides = [(l["side"], l["reason"]) for l in res["legs"]]
    buy_legs = [s for s in sides if s[0] == "BUY"]
    sell_legs = [s for s in sides if s[0] == "SELL"]
    assert not buy_legs, f"旧 BUY 挂单应被新信号撤销, 实际 {buy_legs}"
    assert sell_legs, "新 SELL 信号应被处理"
    # SELL 市价成交在消息后首根完整 bar 收盘 = 10:02 收盘 105.5,
    # 之后 10:03 跌到 95.5: 若 TP=90 或 SL=110 触发前无 close, eod 平仓
    assert sell_legs[0][1] in ("TP", "SL", "end_of_data")


# ---------------------------------------------------------------------------
# 场景 3+8 (P0-3): 4H 收盘突破开空 — 收盘确认前绝不触发; parser 输出 text
# ---------------------------------------------------------------------------

def test_engine_reads_entry_timeframe_for_conditional(engine_mod):
    """conditional + entry_trigger_type=close_based + entry_timeframe=1m:
    close_based 的成交 tick 只能是**收盘 tick**(59.999s) — 断言 fill_ms
    精确等于确认 bar 的收盘时刻, 而非盘中触及 bar 的任意 tick。"""
    sim = engine_mod.Sim(rule="tp_ladder", mkt=FakeMarket([
        [0, 1900, 1900, 1900, 1900.0],
        [60_000, 1900, 1900, 1800, 1899.0],    # 盘中触及 1865 但收盘 1899
        [120_000, 1880, 1880, 1850, 1860.0],   # 1m 收盘 1860 <= 1865 → 触发
    ]))
    sig = _sig(0, "TESTUSDT", "SHORT", "STOP", [1865.0], 1900.0, [1800.0])
    sig["entry_trigger_type"] = "close_based"
    sig["entry_timeframe"] = "1m"
    positions, last_sig = {}, {}
    sim._handle_open(sig, positions, last_sig, {})
    assert "TEST" in positions, "bar2 收盘 1860 确认 → 应开仓"
    leg = positions["TEST"]["legs"][0]
    # close_based: 触发 tick 必须 = 确认 bar 的收盘 tick (120000+59.999s)
    assert leg["fill_ms"] == 120_000 + 59_999, \
        f"fill 应在确认 bar 收盘 tick, 实际 {leg['fill_ms']}"
    # bar1 (60000) 的盘中触及(1800 ≤ 1865)不得触发: fill_ms ≠ 60000+*


def test_parser_outputs_text_and_entry_trigger():
    """P0-3 回归: parse_signal 必须把原始 text 写进输出(否则
    entry_trigger_type 的启发式永远拿不到 '收盘' 指令)。"""
    sp = _load("sp_r4", "signal_parser/parser.py")
    captured = {}

    def fake_post(payload):
        captured["text"] = payload["messages"][1]["content"]
        return {"choices": [{"message": {"content": json.dumps({
            "should_trade": True, "symbol": "ETH", "side": "SHORT",
            "order_type": "LIMIT", "entry_prices": [1865.0], "sl": 1900.0,
            "tp_levels": [1800.0], "conditional": True})}}]}

    sp._post = fake_post
    out = sp.parse_signal("4H close under 1865 and we go short. stops 1892")
    assert out.get("text"), "parse_signal 必须把原始 text 写进输出"
    assert "close under" in out["text"]
    assert out.get("entry_trigger_type") == "close_based", \
        "文本含 'close under' → 条件入场应为 close_based"


# ---------------------------------------------------------------------------
# 场景 4 (P1): 撤单消息所在分钟的触及 — 保守处理(撤单优先)
# ---------------------------------------------------------------------------

def test_voice_cancel_in_touch_bar_is_conservative(engine_mod):
    ve = _load("ve_r4b", "backtest/voice_event_engine.py")
    """10:00:50 挂 BUY 105; 10:01:10 撤单; 10:01 bar high 106 触及挂单价。
    撤单(10:01:10)与触及的先后 OHLC 不可分辨 → 保守: 撤单优先, 不成交。"""
    kl = [[0, 100, 100, 100, 100.0],
          [60_000, 100, 106, 100, 105.0],       # 10:01: high 触及 105
          [120_000, 105, 106, 104, 105.5]]
    evs = [
        {"ts_ms": 50_000, "kind": "signal", "side": "BUY",
         "entry": 105.0, "sl": 95.0, "tp": 120.0},
        {"ts_ms": 70_000, "kind": "manage", "ev": "close_all"},   # 10:01:10 撤
    ]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"] == [], "撤单与触及同分钟不可分辨 → 保守撤单"


# ---------------------------------------------------------------------------
# 场景 5 (P1-6): 两腿其一已全平 → BE 按实际剩余数量计算
# ---------------------------------------------------------------------------

def test_be_excludes_fully_closed_leg(engine_mod):
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
    # 只剩腿2: BE = 98 (腿1 已全平, 不参与加权)
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
    # 模拟 run 尾部: 数据截止平仓(exit ts = 数据末 bar 开盘; fill 同 minute
    # → fill_ms > exit_ts 不成立 → 开仓费必须计)
    positions["TEST"]["legs"][0]["exits"].append(
        {"price": 100.0, "frac": 1.0, "ts_ms": 60_000})
    sim._finish(positions["TEST"])
    t = sim.trades[-1]
    assert t["fee_usdt"] > 0, "数据截止仍持仓的腿必须计开仓手续费"


# ---------------------------------------------------------------------------
# 场景 7 (P0-4): SHORT 同 bar TP/SL — low_first 与 high_first 结果相反
# ---------------------------------------------------------------------------

def test_short_same_bar_tp_sl_modes_disagree(engine_mod):
    """SHORT: TP 在下方、SL 在上方。信号 bar0 内市价成交(分钟末);
    bar1 同 bar 触及 TP94 与 SL108 — low_first 模式 TP 先触发(赚);
    high_first 模式 SL 先触发(亏) — 两模式结果必须不同且方向可解释。"""
    bars = [[0, 100.0, 100.0, 100.0, 100.0],   # 信号 bar: 市价 fill = 收盘 100
            [60_000, 100.0, 108.0, 94.0, 100.0]]   # 冲突 bar: 同 bar 触及两者
    results = {}
    for mode in ("low_first", "high_first"):
        ve = _load(f"ve_{mode}", "backtest/voice_event_engine.py")
        ve.PATH_MODE = mode                      # 直接设模块属性(不 reload)
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
# 场景 8 (P1): CLI smoke 不吞失败
# ---------------------------------------------------------------------------

def test_cli_smoke_entry_exists():
    """CLI 入口验证不再用 || echo 吞失败: --help 应成功退出。"""
    import subprocess
    r = subprocess.run([sys.executable, "-m", "signal_parser", "--help"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, f"python -m signal_parser --help 失败: {r.stderr[:200]}"
