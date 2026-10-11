# -*- coding: utf-8 -*-
"""第六轮审查的场景回归测试 (v0.2.6)。

P0: voice 走查起点 i0 = lw // 60_000 把"纪元分钟数"当数组下标 —
合成测试数据(时间戳从 0 开始)恰好掩盖该错误; 真实 Unix 时间戳下
起点是 ~29,000,000 而 klines 数组只有几千根 → 循环永不执行 →
事件推进与尾部回放全部失效, TP/SL 永不触发。
回归测试必须用真实 Unix 时间戳, 覆盖 事件推进 + 尾部回放 两条路径。
"""
import importlib.util
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
