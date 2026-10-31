# -*- coding: utf-8 -*-
"""第十五轮回归测试 (round-14 增补的外部审计修复)。

1. P1 翻正判断必须用净R — 毛R为正但成本后为负时, 报告必须给出"结论降级"明示
   (round-14 用毛R判断, 此场景静默漏报)。
2. P1 可测性警示必须读真实引擎字段 stop_init — 端到端: 用真实 Sim 产出交易
   (只有 stop_init, 无 sl 键), 紧止损(<0.3%)时报告必须出现警示。
3. P1 bound.json 必须与本次 sim 绑定 — 输入哈希不匹配的旧 bound 必须被忽略并告警。
4. P2 修复 round-14 的永真断言, 并把 bound 明确为零滑点+零点差+零手续费。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BASE = 1_775_808_000_000   # 真实纪元毫秒, 分钟对齐


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


# ---------- 1. 净R翻正判断 ----------

def test_flip_uses_net_r(tmp_path, monkeypatch):
    """毛R +5R 但成本后净R -3R + 零成本 +6R → 报告必须明示降级。"""
    eng = _load("svb_engine_r15a", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    t = {"trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
         "signal_ts_ms": BASE, "first_fill_ms": BASE,
         "entry": 100.0, "stop_init": 99.7, "sl_source": "trader",
         "status": "closed", "r": 5.0,            # 毛 +5R
         "pnl_usdt": 125.0, "fee_usdt": 200.0, "funding_usdt": 0.0,
         "net_usdt": -75.0,                        # 净 -3R
         "legs": [], "exit_parts": []}
    (tmp_path / "trades.json").write_text(json.dumps([t]), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30,
        "input_hashes": eng._audit_hashes()}), encoding="utf-8")
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 1, "sum_r": 6.0, "net_sum_r": 6.0, "rule": "tp_ladder",
        "input_hashes": eng._audit_hashes()}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "翻正" in text and "不可判定" in text, \
        "毛R为正但净R为负+零成本翻正 → 必须给出结论降级明示 (round-14 毛R判断漏报此场景)"


def test_flip_not_triggered_when_net_positive(tmp_path, monkeypatch):
    eng = _load("svb_engine_r15b", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    t = {"trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
         "signal_ts_ms": BASE, "first_fill_ms": BASE,
         "entry": 100.0, "stop_init": 99.7, "sl_source": "trader",
         "status": "closed", "r": 5.0, "pnl_usdt": 125.0,
         "fee_usdt": 5.0, "funding_usdt": 0.0, "net_usdt": 120.0,
         "legs": [], "exit_parts": []}
    (tmp_path / "trades.json").write_text(json.dumps([t]), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30}), encoding="utf-8")
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 1, "sum_r": 6.0, "net_sum_r": 6.0, "rule": "tp_ladder"}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "翻正" not in text


# ---------- 2. stop_init 字段契约 (端到端, 真实 Sim 产物) ----------

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


def test_measurability_e2e_real_sim_trade(tmp_path, monkeypatch):
    """端到端: 真实 Sim 产生的交易只有 stop_init 键 — 紧止损时报告必须警示。
    (round-14 读 sl_init/sl 键, 对真实产物从不触发 — 字段契约错误)"""
    eng = _load("svb_engine_r15c", tmp_path, monkeypatch)
    sim = eng.Sim.__new__(eng.Sim)
    sim.rule = "tp_ladder"
    sim.trades = []
    sim.funding_missing = set()
    # 0.15% 紧止损: entry 100, SL 99.85; 首bar即打损
    sim.mkt = FakeMarket([
        [BASE, 100, 100, 100, 100.0],
        [BASE + 60_000, 100, 100, 99.8, 99.82],   # SL 99.85 触发
        [BASE + 120_000, 99.8, 99.9, 99.7, 99.75],
    ])
    sim._fetch_funding = lambda *a, **k: []
    sig = {"ts_ms": BASE + 30_000, "symbol": "TEST", "side": "LONG",
           "order_type": "MARKET", "entry_prices": [], "entry_price": None,
           "sl": 99.85, "tp_levels": [], "id": "s1", "text": "long tight stop"}
    sim.run([sig], [])
    closed = [t for t in sim.trades if t.get("status") == "closed" and t.get("r") is not None]
    assert closed, "sim 应产生一笔平仓交易"
    assert "sl" not in closed[0] and closed[0].get("stop_init") == 99.85, \
        "字段契约前提: 真实交易含 stop_init 且无 sl 键"
    _mk_work(tmp_path)
    (tmp_path / "trades.json").write_text(json.dumps(sim.trades), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "可测性边界" in text and "不可判定" in text, \
        "真实引擎产物(仅 stop_init)在紧止损下必须触发可测性警示"


# ---------- 3. bound 运行绑定 ----------

def test_stale_bound_ignored_with_warning(tmp_path, monkeypatch):
    """bound.json 与本次 sim 输入哈希不匹配 → 忽略并告警, 不得用于结论。"""
    eng = _load("svb_engine_r15d", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    t = {"trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
         "signal_ts_ms": BASE, "first_fill_ms": BASE,
         "entry": 100.0, "stop_init": 99.7, "sl_source": "trader",
         "status": "closed", "r": -0.8, "pnl_usdt": -20.0,
         "fee_usdt": 2.0, "funding_usdt": 0.0, "net_usdt": -22.0,
         "legs": [], "exit_parts": []}
    (tmp_path / "trades.json").write_text(json.dumps([t]), encoding="utf-8")
    meta = {"rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
            "risk_usdt": 25.0, "position_cap_d": 30,
            "input_hashes": {"signals_final.json": "cur_ran_hash"}}
    (tmp_path / "sim_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    # 旧一轮的 bound: 哈希不同
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 5, "sum_r": 12.0, "net_sum_r": 12.0, "rule": "tp_ladder",
        "input_hashes": {"signals_final.json": "OLD_run_hash"}}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "已忽略" in text and "不匹配" in text, "过期 bound 必须被忽略并告警"
    assert "翻正" not in text, "过期 bound 不得参与结论"
    assert "零成本边界: ΣR +12.00" not in text


def test_matching_bound_still_shown(tmp_path, monkeypatch):
    """哈希匹配时 bound 正常展示并参与翻正判断。"""
    eng = _load("svb_engine_r15e", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    h = eng._audit_hashes()
    t = {"trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
         "signal_ts_ms": BASE, "first_fill_ms": BASE,
         "entry": 100.0, "stop_init": 99.7, "sl_source": "trader",
         "status": "closed", "r": -0.8, "pnl_usdt": -20.0,
         "fee_usdt": 2.0, "funding_usdt": 0.0, "net_usdt": -22.0,
         "legs": [], "exit_parts": []}
    (tmp_path / "trades.json").write_text(json.dumps([t]), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30, "input_hashes": h}), encoding="utf-8")
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 5, "sum_r": 12.0, "net_sum_r": 12.0, "rule": "tp_ladder",
        "input_hashes": h}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "零成本边界: ΣR +12.00" in text
    assert "翻正" in text and "不可判定" in text


# ---------- 4. phase_bound 语义: 真·零成本 ----------

def test_bound_zeroes_all_costs(tmp_path, monkeypatch):
    eng = _load("svb_engine_r15f", tmp_path, monkeypatch,
                {"SVB_SLIP": "0.002", "SVB_SPREAD": "0.001"})
    _mk_work(tmp_path)
    captured = {}

    class FakeSim:
        def __init__(self, rule="r_ladder", mkt=None):
            self.trades = []
            self.funding_missing = set()
            captured["slip"] = eng.SLIP
            captured["spread"] = eng.SPREAD
            captured["fee"] = eng.FEE_RATE

        def run(self, signals, manages):
            pass

    orig_sim = eng.Sim
    monkeypatch.setattr(eng, "Sim", FakeSim)
    eng.phase_bound()
    assert captured["slip"] == 0.0 and captured["spread"] == 0.0 and captured["fee"] == 0.0, \
        "bound 必须 SLIP=SPREAD=FEE_RATE=0 (真·零成本)"
    assert eng.SLIP == 0.002 and eng.SPREAD == 0.001, "bound 结束后必须恢复原值"
    b = json.loads((tmp_path / "bound.json").read_text(encoding="utf-8"))
    assert "input_hashes" in b and "net_sum_r" in b
