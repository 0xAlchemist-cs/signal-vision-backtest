# -*- coding: utf-8 -*-
"""第十四轮回归测试 (审查十四, 九频道实战沉淀)。

1. SVB_SPREAD 每边点差成本 — CFD/外汇频道的显式成本口径, 计入 fee_usdt 不改成交价;
   默认 0 时结果与现状逐位一致(向后兼容)。
2. 报告可测性警示 — 止损距离中位 <0.3% 时必须输出"1m 粒度不可判定"警示行。
3. phase bound — 零成本边界(SLIP=SPREAD=0)重跑落盘, 报告并列展示;
   计成本为负且零成本翻正时, 报告必须给出"结论降级"明示。
4. 资金费文案 — "资金费按可获得数据计入"(审计遗留措辞修正)。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_TRADE = {
    "trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
    "signal_ts_ms": 1_775_808_000_000, "first_fill_ms": 1_775_808_000_000,
    "entry": 100.0, "sl": 99.7, "sl_source": "trader", "status": "closed", "r": 0.5,
    "net_usdt": 12.5, "pnl_usdt": 12.5, "fee_usdt": 0.1, "funding_usdt": 0.0,
    "legs": [{"risk_share": 1.0, "risk_unit": 0.3, "entry": 100.0,
              "remain_frac": 0.0, "exits": []}],
    "exit_parts": [{"reason": "TP1", "price": 105.0, "ts_ms": 1_775_810_000_000}],
}


def _load(name: str, work: Path, env=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("SVB_WORK", str(work))
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
    else:
        os.environ["SVB_WORK"] = str(work)
        for k, v in (env or {}).items():
            os.environ[k] = v
    spec = importlib.util.spec_from_file_location(name, ROOT / "backtest/engine.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_work(tmp_path):
    (tmp_path / "signals_final.json").write_text("[]", encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")


def test_spread_charged_in_fee(tmp_path, monkeypatch):
    """SVB_SPREAD=0.001 的单腿一进一出: 费用须含 SPREAD 份额。"""
    eng = _load("svb_engine_r14a", tmp_path, {"SVB_SPREAD": "0.001"}, monkeypatch)
    sim = eng.Sim.__new__(eng.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    sim.mkt = None
    pos = json.loads(json.dumps(_TRADE))
    pos["legs"][0]["exits"] = [{"reason": "TP1", "price": 105.0, "frac": 1.0,
                                "ts_ms": 1_775_810_000_000}]
    sim._finish(pos)
    qty = 25.0 / 0.3
    base_fee = qty * 100.0 * 0.00025 + qty * 105.0 * 0.00025
    with_spread = base_fee * (0.00125 / 0.00025)
    assert pos["fee_usdt"] == pytest.approx(round(with_spread, 2), abs=0.011), \
        "fee_usdt 未包含 SPREAD 份额"
    assert pos["fee_usdt"] > round(base_fee, 2)


def test_spread_zero_unchanged(tmp_path, monkeypatch):
    eng = _load("svb_engine_r14b", tmp_path, {"SVB_SPREAD": "0"}, monkeypatch)
    sim = eng.Sim.__new__(eng.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    sim.mkt = None
    pos = json.loads(json.dumps(_TRADE))
    pos["legs"][0]["exits"] = [{"reason": "TP1", "price": 105.0, "frac": 1.0,
                                "ts_ms": 1_775_810_000_000}]
    sim._finish(pos)
    qty = 25.0 / 0.3
    base_fee = qty * 100.0 * 0.00025 + qty * 105.0 * 0.00025
    assert pos["fee_usdt"] == pytest.approx(round(base_fee, 2), abs=0.011)


def test_report_tight_stop_warning(tmp_path, monkeypatch):
    """止损距离中位 <0.3% → 报告必须出现可测性警示。"""
    eng = _load("svb_engine_r14c", tmp_path, monkeypatch=monkeypatch)
    _mk_work(tmp_path)
    trades = []
    for i in range(6):
        t = json.loads(json.dumps(_TRADE))
        t["signal_ts_ms"] += i * 60_000
        trades.append(t)   # entry 100 / sl 99.7 → 0.3%... 用 99.85 → 0.15%
    for t in trades:
        t["sl"] = 99.85
        t["legs"][0]["risk_unit"] = 0.15
    (tmp_path / "trades.json").write_text(json.dumps(trades), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "可测性边界" in text, "紧止损频道报告必须给出可测性警示"
    assert "不可判定" in text


def test_report_no_warning_for_wide_stops(tmp_path, monkeypatch):
    eng = _load("svb_engine_r14d", tmp_path, monkeypatch=monkeypatch)
    _mk_work(tmp_path)
    trades = []
    for i in range(6):
        t = json.loads(json.dumps(_TRADE))
        t["signal_ts_ms"] += i * 60_000
        t["sl"] = 97.0          # 3% 宽止损
        t["legs"][0]["risk_unit"] = 3.0
        trades.append(t)
    (tmp_path / "trades.json").write_text(json.dumps(trades), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "可测性边界" not in text


def test_bound_phase_and_report_line(tmp_path, monkeypatch, capsys):
    """phase bound 落盘 bound.json + 零成本翻正时报告给'结论降级'明示。"""
    eng = _load("svb_engine_r14e", tmp_path, {"SVB_SPREAD": "0.001", "SVB_SLIP": "0.002"}, monkeypatch)
    _mk_work(tmp_path)
    (tmp_path / "trades.json").write_text(json.dumps([_TRADE]), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.002, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30, "spread": 0.001,
        "params": eng._sim_params(), "run_id": "rE", "kline_fp": {}}),
        encoding="utf-8")

    class FakeSim:
        def __init__(self, rule="r_ladder", mkt=None):
            self.trades = []
            self.funding_missing = set()

        def run(self, signals, manages):
            t = json.loads(json.dumps(_TRADE))
            t["r"] = 1.2            # 零成本下翻正
            self.trades.append(t)

    monkeypatch.setattr(eng, "Sim", FakeSim)
    eng.phase_bound()
    b = json.loads((tmp_path / "bound.json").read_text(encoding="utf-8"))
    assert b["sum_r"] == pytest.approx(1.2)
    assert b["run_id"] == "rE", "bound 必须绑定产生它的 sim 的 run_id"
    # 报告: meta 与 bound 的哈希/参数/run_id 全一致 → 零成本边界行正常展示
    hashes = eng._audit_hashes()
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.002, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30, "spread": 0.001,
        "params": eng._sim_params(), "input_hashes": hashes,
        "run_id": "rE", "kline_fp": {}}), encoding="utf-8")
    (tmp_path / "trades.json").write_text(json.dumps([_TRADE]), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "- 零成本边界: ΣR" in text, "绑定完整的 bound 必须被报告采信"
    assert "已忽略" not in text
    assert "翻正" not in text, "计成本为正时不得触发降级语"


def test_bound_flip_flag(tmp_path, monkeypatch):
    """计成本为负、零成本为正 → 报告必须明示'成本敏感'(宽止损, 隔离可测性行)。"""
    eng = _load("svb_engine_r14f", tmp_path, monkeypatch=monkeypatch)
    _mk_work(tmp_path)
    neg = json.loads(json.dumps(_TRADE))
    neg["r"] = -0.8
    neg["net_usdt"] = -20.0
    neg["stop_init"] = 97.0          # 3% 宽止损 → 不触发可测性警示
    neg["legs"] = [{"risk_share": 1.0, "risk_unit": 3.0, "entry": 100.0,
                    "remain_frac": 0.0, "exits": []}]
    (tmp_path / "trades.json").write_text(json.dumps([neg]), encoding="utf-8")
    h = eng._audit_hashes()
    params = eng._sim_params()
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30, "params": params,
        "input_hashes": h, "run_id": "t1", "kline_fp": {}}), encoding="utf-8")
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 1, "sum_r": 1.2, "net_sum_r": 1.2, "rule": "tp_ladder",
        "params": params, "input_hashes": h, "run_id": "t1", "kline_fp": {}}),
        encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "零成本边界" in text and "成本敏感" in text
    assert "不可判定" not in text, "成本敏感与行情可测性是两个结论, 不得混写"


def test_funding_wording(tmp_path, monkeypatch):
    eng = _load("svb_engine_r14g", tmp_path, monkeypatch=monkeypatch)
    _mk_work(tmp_path)
    (tmp_path / "trades.json").write_text(json.dumps([_TRADE]), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "资金费按可获得数据计入" in text
    assert "资金费率已计入" not in text
