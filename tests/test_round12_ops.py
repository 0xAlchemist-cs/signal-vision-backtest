# -*- coding: utf-8 -*-
"""第十二轮运维审计回归测试。

1. phase_prefetch 按标的解析数据源 — 此前硬编码 "bybit"，
   仅 Binance 有合约的标的(如 PUMPUSDT)在预取阶段被静默按 bybit 入队，
   拉取失败进 _failed 后由 sim 时 ensure 兜底(慢路径, 且预取等待提前放行)。
2. phase_sim 落盘 sim_meta.json / phase_report 回读 — 此前报告的规则文案、
   滑点与 25U 折算在 report 阶段重新读环境变量, 报告与 sim 不在同一次调用时
   会标错规则(实测: sim 跑 tp_ladder, 新 shell 里 report 输出 r_ladder 文案)。
3. POSITION_CAP_D 可由 SVB_POSITION_CAP_D 覆盖(此前硬编码 30)。
"""
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name: str, env=None):
    if env:
        for k, v in env.items():
            os.environ[k] = v
    spec = importlib.util.spec_from_file_location(name, ROOT / "backtest/engine.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_prefetch_resolves_symbol_source(tmp_path, monkeypatch):
    """仅 Binance 上架的标的, 预取必须按 binance 源入队而不是 bybit。"""
    eng = _load("svb_engine_r12a", {"SVB_WORK": str(tmp_path)})
    (tmp_path / "instruments.json").write_text(json.dumps({
        "BTCUSDT": "bybit", "PUMPUSDT": "binance"}), encoding="utf-8")
    (tmp_path / "kline_windows.json").write_text(json.dumps([
        {"symbol": "BTCUSDT", "t0": 1, "t1": 2},
        {"symbol": "PUMPUSDT", "t0": 1, "t1": 2},
        {"symbol": "GHOSTUSDT", "t0": 1, "t1": 2},   # 双边都没有 → 保持 bybit 原样入队
    ]), encoding="utf-8")
    calls = []

    class FakeM:
        FETCH_WORKERS = 0
        INSTR = {"PUMPUSDT": "binance", "BTCUSDT": "bybit"}

        def __init__(self):
            self.klines = {}
            self._cond = threading.Condition()
            self._queue = []
            self._pending = set()

        def resolve(self, base):
            s = base.upper()
            for cand in (s + "USDT", "1000" + s + "USDT"):
                if cand in self.INSTR:
                    return cand, self.INSTR[cand], 1.0
            return None

        def prefetch(self, sym, source, t0, t1):
            calls.append((sym, source))

    monkeypatch.setattr(eng, "Market", FakeM)
    eng.phase_prefetch()
    assert ("BTCUSDT", "bybit") in calls
    assert ("PUMPUSDT", "binance") in calls, "Binance 兜底标的被硬编码成 bybit 预取"
    assert ("GHOSTUSDT", "bybit") in calls


def test_report_reads_sim_meta_not_stale_env(tmp_path, monkeypatch, capsys):
    """sim 在 SVB_RULE=tp_ladder 下落盘, report 在未设该变量的新 shell 里
    也必须输出 tp_ladder 的规则文案与 meta 内的滑点。"""
    monkeypatch.delenv("SVB_RULE", raising=False)
    eng = _load("svb_engine_r12b", {"SVB_WORK": str(tmp_path)})
    (tmp_path / "trades.json").write_text(json.dumps([{
        "trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
        "signal_ts_ms": 1_775_808_000_000, "first_fill_ms": 1_775_808_000_000,
        "entry": 100.0, "sl_source": "trader", "status": "closed", "r": 0.5,
        "net_usdt": 12.5, "pnl_usdt": 12.5, "fee_usdt": 0.1, "funding_usdt": 0.0,
        "exit_parts": [{"reason": "TP1", "price": 105.0, "ts_ms": 1_775_810_000_000}],
    }]), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "信号止盈位阶梯" in text, "sim_meta 存在时报告应回读 tp_ladder 文案"
    assert "+1R 平40%" not in text.split("\n")[3], "不得再按 report 时的环境变量标错规则"


def test_report_without_meta_falls_back_to_env(tmp_path, monkeypatch):
    """老运行没有 sim_meta.json → 维持环境变量回退, 兼容不破坏。"""
    monkeypatch.setenv("SVB_RULE", "tp_ladder")
    eng = _load("svb_engine_r12c", {"SVB_WORK": str(tmp_path)})
    (tmp_path / "trades.json").write_text(json.dumps([{
        "trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
        "signal_ts_ms": 1_775_808_000_000, "first_fill_ms": 1_775_808_000_000,
        "entry": 100.0, "sl_source": "trader", "status": "closed", "r": 0.5,
        "net_usdt": 12.5, "pnl_usdt": 12.5, "fee_usdt": 0.1, "funding_usdt": 0.0,
        "exit_parts": [{"reason": "TP1", "price": 105.0, "ts_ms": 1_775_810_000_000}],
    }]), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "信号止盈位阶梯" in text


def test_position_cap_env_tunable(monkeypatch):
    monkeypatch.setenv("SVB_POSITION_CAP_D", "45")
    eng = _load("svb_engine_r12d")
    assert eng.POSITION_CAP_D == 45
    monkeypatch.delenv("SVB_POSITION_CAP_D", raising=False)
    eng2 = _load("svb_engine_r12e")
    assert eng2.POSITION_CAP_D == 30


def test_phase_sim_writes_meta(tmp_path, monkeypatch):
    """phase_sim 必须把当时口径落盘 sim_meta.json(供 report 在新 shell 回读)。"""
    eng = _load("svb_engine_r12f", {"SVB_WORK": str(tmp_path)})
    (tmp_path / "signals_final.json").write_text("[]", encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")

    class FakeSim:
        def __init__(self, rule="r_ladder", mkt=None):
            self.trades = []
            self.funding_missing = set()

        def run(self, signals, manages):
            pass

    monkeypatch.setattr(eng, "Sim", FakeSim)
    eng.phase_sim()
    meta = json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))
    assert meta["rule"] == "r_ladder"
    assert "slip" in meta and "risk_usdt" in meta and "percent_basis" in meta
