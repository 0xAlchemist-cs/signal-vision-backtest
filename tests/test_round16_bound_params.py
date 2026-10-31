# -*- coding: utf-8 -*-
"""第十六轮回归测试 (round-15 增补的外部审计修复)。

1. P1 参数指纹 — 相同输入、不同 SVB_RULE(或其它影响结果的参数)时, 旧 bound
   必须失效; bound.json 与 sim_meta.json 都保存统一 params 指纹, 报告逐项核对。
2. P1 零成本语义 — bound 模式下资金费必须同样归零(net == 毛), 并提供
   "零执行成本(含资金费)"与现状的明确区分; 有资金费事件的端到端验证。
3. P1 措辞拆分 — 成本翻正只能说"成本敏感"(毛优势不足以覆盖成本), 不得直接
   写"1m 粒度不可判定"; 行情可测性只由止损距离分布单独评估。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BASE = 1_775_808_000_000


def _load(name: str, work: Path, monkeypatch=None, env=None):
    if monkeypatch is not None:
        monkeypatch.setenv("SVB_WORK", str(work))
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
    else:
        os.environ["SVB_WORK"] = str(work)
    spec = importlib.util.spec_from_file_location(name, ROOT / "backtest/engine.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_work(tmp_path):
    (tmp_path / "signals_final.json").write_text("[]", encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")


def _trade(r=5.0, net=-75.0, stop=99.7):
    return {"trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
            "signal_ts_ms": BASE, "first_fill_ms": BASE,
            "entry": 100.0, "stop_init": stop, "sl_source": "trader",
            "status": "closed", "r": r, "pnl_usdt": r * 25.0,
            "fee_usdt": 200.0, "funding_usdt": net - r * 25.0,
            "net_usdt": net, "legs": [], "exit_parts": []}


def _meta(eng, tmp_path, rule="tp_ladder", stop=99.7):
    return {"rule": rule, "slip": 0.0005, "percent_basis": "original",
            "risk_usdt": 25.0, "position_cap_d": 30,
            "input_hashes": eng._audit_hashes()}


# ---------- 1. 参数指纹 ----------

def test_rule_change_invalidates_bound(tmp_path, monkeypatch):
    """同输入换 SVB_RULE → 旧 bound 必须失效(不得为另一条规则的结论背书)。"""
    eng = _load("svb_engine_r16a", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    (tmp_path / "trades.json").write_text(json.dumps([_trade(r=-0.8, net=-22.0)]),
                                          encoding="utf-8")
    h = eng._audit_hashes()
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30,
        "params": {"rule": "tp_ladder", "slip": 0.0005, "spread": 0.0,
                   "percent_basis": "original", "risk_usdt": 25.0,
                   "path_mode": "synthetic", "position_cap_d": 30},
        "input_hashes": h}), encoding="utf-8")
    # 旧 bound: 同输入但 rule=r_ladder
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 3, "sum_r": 20.0, "net_sum_r": 20.0, "rule": "r_ladder",
        "params": {"rule": "r_ladder", "slip": 0.0005, "spread": 0.0,
                   "percent_basis": "original", "risk_usdt": 25.0,
                   "path_mode": "synthetic", "position_cap_d": 30},
        "input_hashes": h}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "已忽略" in text, "params 指纹不一致的旧 bound 必须被忽略"
    assert "零成本边界: ΣR +20.00" not in text
    assert "成本敏感" not in text


def test_matching_params_bound_accepted(tmp_path, monkeypatch):
    eng = _load("svb_engine_r16b", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    (tmp_path / "trades.json").write_text(json.dumps([_trade(r=-0.8, net=-22.0)]),
                                          encoding="utf-8")
    h = eng._audit_hashes()
    params = {"rule": "tp_ladder", "slip": 0.0005, "spread": 0.0,
              "percent_basis": "original", "risk_usdt": 25.0,
              "path_mode": "synthetic", "position_cap_d": 30}
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30, "params": params,
        "input_hashes": h}), encoding="utf-8")
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 3, "sum_r": 20.0, "net_sum_r": 20.0, "rule": "tp_ladder",
        "params": params, "input_hashes": h}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "零成本边界: ΣR +20.00" in text
    assert "成本敏感" in text


# ---------- 2. 零成本语义: 资金费归零 ----------

def test_bound_disables_funding(tmp_path, monkeypatch):
    """NO_COSTS 模式下 _apply_funding 必须为空操作; 正常模式照常计费。"""
    eng = _load("svb_engine_r16c", tmp_path, monkeypatch)
    sim = eng.Sim.__new__(eng.Sim)
    pos = {"side": "LONG", "funding_usdt": 0.0,
           "legs": [{"risk_share": 1.0, "risk_unit": 0.3, "remain_frac": 1.0,
                     "fill_ms": 0}]}
    # 正常模式: 多单付资金费
    eng.NO_COSTS = False
    sim._apply_funding(pos, 0.0001, 100.0)
    assert pos["funding_usdt"] < 0, "正常模式多单必须付资金费"
    # bound 模式: 资金费归零
    eng.NO_COSTS = True
    pos2 = {"side": "LONG", "funding_usdt": 0.0,
            "legs": [{"risk_share": 1.0, "risk_unit": 0.3, "remain_frac": 1.0,
                      "fill_ms": 0}]}
    sim._apply_funding(pos2, 0.0001, 100.0)
    assert pos2["funding_usdt"] == 0.0, "零成本模式资金费必须为空操作"


def test_bound_net_equals_gross_with_funding_events(tmp_path, monkeypatch):
    """端到端: 即使存在资金费事件, bound 的 net_sum_r 必须等于 sum_r。"""
    eng = _load("svb_engine_r16d", tmp_path, monkeypatch,
                {"SVB_SLIP": "0.001", "SVB_SPREAD": "0.001"})
    _mk_work(tmp_path)
    # Sim 替身捕获成本开关状态; 用真实 _apply_funding 语义模拟一笔带资金费的平仓
    captured = {}

    class FakeSim:
        def __init__(self, rule="r_ladder", mkt=None):
            self.trades = []
            self.funding_missing = set()
            captured["no_costs"] = eng.NO_COSTS

        def run(self, signals, manages):
            t = {"trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
                 "signal_ts_ms": BASE, "first_fill_ms": BASE,
                 "entry": 100.0, "stop_init": 99.7, "status": "closed",
                 "r": 0.4, "pnl_usdt": 10.0, "fee_usdt": 0.0,
                 "funding_usdt": 0.0,     # NO_COSTS 下 funding 不结算 → 保持 0
                 "net_usdt": 10.0, "legs": [], "exit_parts": []}
            # 模拟: 若资金费仍在结算, 这里会是 -15U(净转负)
            if not captured["no_costs"]:
                t["funding_usdt"] = -15.0
                t["net_usdt"] = -5.0
            self.trades.append(t)

    monkeypatch.setattr(eng, "Sim", FakeSim)
    eng.phase_bound()
    b = json.loads((tmp_path / "bound.json").read_text(encoding="utf-8"))
    assert b["net_sum_r"] == pytest.approx(b["sum_r"]), \
        "真·零成本(含资金费归零)下 bound 的净ΣR必须等于毛ΣR"


# ---------- 3. 措辞拆分: 成本敏感 ≠ 1m 不可判定 ----------

def test_cost_flip_says_cost_sensitive_not_undecidable(tmp_path, monkeypatch):
    """宽止损(3%) + 净负 + 零成本翻正 → 只能说成本敏感, 不得说 1m 不可判定。"""
    eng = _load("svb_engine_r16e", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    h = eng._audit_hashes()
    # 3% 宽止损 → 不触发可测性警示
    (tmp_path / "trades.json").write_text(json.dumps(
        [_trade(r=-2.0, net=-50.0, stop=97.0)]), encoding="utf-8")
    params = {"rule": "tp_ladder", "slip": 0.0005, "spread": 0.0,
              "percent_basis": "original", "risk_usdt": 25.0,
              "path_mode": "synthetic", "position_cap_d": 30}
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30, "params": params,
        "input_hashes": h}), encoding="utf-8")
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 1, "sum_r": 6.0, "net_sum_r": 6.0, "rule": "tp_ladder",
        "params": params, "input_hashes": h}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "成本敏感" in text, "净负+零成本翻正必须明示成本敏感"
    assert "不可判定" not in text, "宽止损+路径无歧义时, 成本翻正不得写成 1m 不可判定"


def test_bound_params_captured_before_zeroing(tmp_path, monkeypatch):
    """指纹必须在清零前抓取 — 否则 bound 的 params(slip=0) 永远对不上 sim_meta。"""
    eng = _load("svb_engine_r16f", tmp_path, monkeypatch,
                {"SVB_SLIP": "0.002", "SVB_SPREAD": "0.001"})
    _mk_work(tmp_path)

    class FakeSim:
        def __init__(self, rule="r_ladder", mkt=None):
            self.trades = []
            self.funding_missing = set()

        def run(self, signals, manages):
            pass

    monkeypatch.setattr(eng, "Sim", FakeSim)
    eng.phase_bound()
    b = json.loads((tmp_path / "bound.json").read_text(encoding="utf-8"))
    assert b["params"]["slip"] == 0.002 and b["params"]["spread"] == 0.001,         "bound 的参数指纹必须是正常成本口径, 不得用清零后的值"
    assert b["params"]["rule"] == "r_ladder"
