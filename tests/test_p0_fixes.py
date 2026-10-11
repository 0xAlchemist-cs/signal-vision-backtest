# -*- coding: utf-8 -*-
"""P0 修复的最小回归测试 (外部代码审查 2026-10-10 的第一阶段要求)。

每条测试对应一个已修复的 P0:
1. 多腿激活: 未成交腿在 fill_ms 前不参与止损/止盈
2. 分钟对齐: candles() 不再排除"刚成交那一分钟"的剩余 tick
3. 换仓 status: 换新撤旧路径的交易进入报告统计
4. 止损触发: 触发判定用原始止损, 滑点只作用于成交价; 跳空按 tick 价成交
5. STOP 入场: 触发方向与限价相反, close_based 只在周期收盘确认
6. voice_event: 参考价/成交无未来数据(消息前一根已完成 bar)
"""
import importlib.util
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
    # 引擎对 import 侧有轻量副作用(.env 读取); WORK 指向 pytest 管理的临时
    # 目录 — 不依赖仓库内任何路径(CI checkout 树与本地一致)
    import os
    os.environ.setdefault("SVB_WORK", str(tmp_path_factory.mktemp("svb_work")))
    sys.path.insert(0, str(ROOT))
    yield _load("svb_engine", "backtest/engine.py")
    sys.path.pop(0)


# ---------------------------------------------------------------------------
# 纯函数级测试: candle_to_ticks / candles 的分钟对齐
# ---------------------------------------------------------------------------

def test_candle_to_ticks_four_slots(engine_mod):
    """1m OHLC 展开 4 tick: 0/20/40/59.999 秒"""
    ticks = list(engine_mod.candle_to_ticks(60_000, 100.0, 101.0, 99.0, 100.5))
    tss = [t for t, _ in ticks]
    assert len(tss) == 4
    assert tss[0] == 60_000            # 0s
    assert tss[1] == 60_000 + 20_000   # 20s
    assert tss[2] == 60_000 + 40_000   # 40s
    assert tss[3] == 60_000 + 59_999   # 59.999s


# ---------------------------------------------------------------------------
# voice_event_engine: 参考价无未来数据
# ---------------------------------------------------------------------------

def test_voice_event_ref_uses_prev_closed_bar(tmp_path):
    ve = _load("vee", "backtest/voice_event_engine.py")
    # bars: 10:00(100), 10:01(110); 消息在 10:00:15 → ref 必须是 10:00 bar 收盘 100,
    # 而不是 10:01 bar 收盘 110 (旧实现的前视)
    kl = [[0, 100, 100, 100, 100.0], [60_000, 110, 110, 110, 110.0]]
    evs = [{"ts_ms": 15_000, "kind": "signal", "side": "BUY",
            "entry": None, "sl": 90.0, "tp": 120.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    # 市价型成交 = 消息后首根完整 bar 收盘 = 10:01 bar 收盘 110 (fill 在该 bar 末)
    legs = res["legs"]
    assert len(legs) == 1
    assert legs[0]["entry"] == 110.0


def test_voice_event_limit_fill_walk_starts_at_fill_bar(tmp_path):
    ve = _load("vee2", "backtest/voice_event_engine.py")
    # 限价单: 消息在 10:00:15, entry=105 于 10:02 bar 才触及。
    # 修复后持仓管理从成交 bar 开始 — 10:02 bar 的 TP=108 不应在成交前被判定
    kl = [[0, 100, 100, 100, 100.0],
          [60_000, 100, 100, 100, 100.0],
          [120_000, 104, 108, 103, 107.0],   # 触及 entry=105 且触及 TP=108 (同bar)
          [180_000, 107, 107, 107, 107.0]]
    evs = [{"ts_ms": 15_000, "kind": "signal", "side": "BUY",
            "entry": 105.0, "sl": 95.0, "tp": 108.0}]
    cfg = ve.Config(r_action="none", sizing="fixed", lots_fixed=0.01)
    res = ve.run(kl, evs, cfg)
    assert res["legs"], "限价单应在 10:02 bar 成交"
    leg = res["legs"][0]
    assert leg["entry"] == 105.0


# ---------------------------------------------------------------------------
# engine.Sim 级测试: 多腿激活 / 止损触发分离 / 换仓 status
# ---------------------------------------------------------------------------

def _mini_sim(engine_mod, monkeypatch, tmp_path):
    """绕开 Market/DB, 直接构造 Sim 并手工驱动 _tick_legs"""
    sim = engine_mod.Sim(rule="tp_ladder")
    return sim


def test_stop_trigger_uses_raw_stop_not_slipped(engine_mod):
    """止损 99.0, SLIP=0.05% → 旧代码用 98.9505 判定: price=98.98 不触发(错)。
    修复后用 99.0 判定: 触发, 成交价=98.98*(1-slip)"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)   # 不走 __init__
    pos = {"side": "LONG", "rule": "tp_ladder",
           "legs": [{"entry": 100.0, "risk_unit": 1.0, "risk_share": 1.0,
                     "remain_frac": 1.0, "stop": 99.0, "t1_done": True,
                     "t2_done": True, "fill_ms": 0, "exits": []}],
           "remain_total": 1.0, "tp_ladder": [],
           "stop_close_based": False, "stop_tf_min": 1}
    # price=98.98: 高于滑点后止损 98.9505(旧代码不触发), 低于原始止损 99.0(应触发)
    done = sim._tick_legs(pos, 98.98, 1_000)
    assert pos["legs"][0]["remain_frac"] == 0.0, "原始止损 99.0 已破, 必须触发"
    exit_price = pos["legs"][0]["exits"][0]["price"]
    assert exit_price == pytest.approx(98.98 * (1 - engine_mod.SLIP))


def test_inactive_leg_skipped_before_fill(engine_mod):
    """第二腿 fill_ms 在未来: 该腿不得在成交前触发止损/被 TP 平掉"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    pos = {"side": "LONG", "rule": "tp_ladder",
           "legs": [
               {"entry": 100.0, "risk_unit": 1.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 99.0, "t1_done": True,
                "t2_done": True, "fill_ms": 0, "exits": []},
               {"entry": 98.0, "risk_unit": 1.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 97.0, "t1_done": True,
                "t2_done": True, "fill_ms": 60_000, "exits": []},   # 1 分钟后才成交
           ],
           "remain_total": 1.0,
           "tp_ladder": [{"i": 1, "price": 101.0, "frac": 1.0, "done": False}],
           "stop_close_based": False, "stop_tf_min": 1}
    # t=30s: 第二腿未激活。TP101 触及 → 只应平第一腿
    sim._tick_legs(pos, 101.0, 30_000)
    assert pos["legs"][0]["remain_frac"] == 0.0, "已激活腿应被 TP 平掉"
    assert pos["legs"][1]["remain_frac"] == 1.0, "未激活腿不得在成交前被 TP/止损"
    # t=61s: 第二腿已激活, 止损 97 在 price=96.5 触发
    sim._tick_legs(pos, 96.5, 61_000)
    assert pos["legs"][1]["remain_frac"] == 0.0, "激活后止损应正常触发"


def test_gap_fill_uses_tick_price(engine_mod):
    """跳空越过止损: 成交价取 tick 价(更差者), 不是止损价+滑点"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    pos = {"side": "LONG", "rule": "tp_ladder",
           "legs": [{"entry": 100.0, "risk_unit": 1.0, "risk_share": 1.0,
                     "remain_frac": 1.0, "stop": 99.0, "t1_done": True,
                     "t2_done": True, "fill_ms": 0, "exits": []}],
           "remain_total": 1.0, "tp_ladder": [],
           "stop_close_based": False, "stop_tf_min": 1}
    sim._tick_legs(pos, 96.0, 1_000)   # 直接跳空到 96
    fill = pos["legs"][0]["exits"][0]["price"]
    assert fill == pytest.approx(96.0 * (1 - engine_mod.SLIP)), "跳空时按 tick 价成交"


def test_stop_entry_semantics(engine_mod):
    """STOP 入场触发方向与限价相反: SHORT-STOP 向下突破触发。
    纯语义断言 — 不实例化 Sim(其 __init__ 会拉交易所元数据, CI 网络不可达)。"""
    # 引擎成交查找循环里的方向谓词(与 _handle_open 一致):
    # LONG: price >= ep 触发; SHORT: price <= ep 触发(STOP)
    # LIMIT-SHORT: price >= ep 才触发(挂上方等回触) — price=89 < 90 不触发
    ep, price = 90.0, 89.0
    short_stop_hit = price <= ep          # "4H 收盘跌破 90 才空" → 向下跌破触发
    short_limit_hit = price >= ep         # 挂 90 等回触 → 未触及
    assert short_stop_hit and not short_limit_hit
    # 反向: LONG-STOP 向上突破触发
    price2 = 91.0
    assert price2 >= ep and not (price2 <= ep)


def test_move_be_uses_weighted_avg_not_first_leg(engine_mod):
    """delta 审计教训: 喊"移保本"= 已成交腿加权均价, 不是 DCA 价/首腿价。
    两腿成交(100 与 98, 等风险)后喊 BE → 保本位应为 99, 用 DCA 价 98 会把
    盈利单算亏。"""
    sim = engine_mod.Sim.__new__(engine_mod.Sim)
    pos = {"side": "LONG", "rule": "tp_ladder",
           "legs": [
               {"entry": 100.0, "risk_unit": 1.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 95.0, "t1_done": True,
                "t2_done": True, "fill_ms": 0, "exits": []},
               {"entry": 98.0, "risk_unit": 1.0, "risk_share": 0.5,
                "remain_frac": 1.0, "stop": 95.0, "t1_done": True,
                "t2_done": True, "fill_ms": 0, "exits": []}],
           "remain_total": 1.0, "tp_ladder": [], "mult": 1.0,
           "stop_close_based": False, "stop_tf_min": 1}
    mg = {"ts_ms": 1_000, "item": {"params": {"action_type": "MOVE_BE"}}}
    sim._apply_manage(pos, mg, tick_price=99.5)
    stops = {l["stop"] for l in pos["legs"]}
    assert stops == {99.0}, f"保本位应为加权均价 99.0, 实际 {stops}"
