# -*- coding: utf-8 -*-
"""第十三轮审计回归测试 (输入绑定与数据质量)。

1. phase_prefetch 必须使用 resolve() 返回的**规范合约代码**(含 1000 前缀别名) —
   此前透传窗口原始名: 交易所只有 1000PEPEUSDT 时仍按 PEPEUSDT 预取,
   API 失败或拉到与回测实际合约不一致的行情。
2. 审计哈希必须绑定 sim 当时的输入快照 — 此前 report 阶段现算当前文件哈希:
   sim 之后改信号文件, 报告会把 A 的逐单收益与 B 的哈希并排展示成同一次研究。
   修改输入后报告必须明示"输入已变更", 不得静默生成看似一致的审计证明。
3. 正常报告(有已平仓交易)必须展示资金费缺失标的; 有缺失时净额标注"部分成本缺失"。
4. 规则文案的持仓上限天数必须来自 sim_meta(position_cap_d), 不得硬编码 30 天。
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

_TRADE = {
    "trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
    "signal_ts_ms": 1_775_808_000_000, "first_fill_ms": 1_775_808_000_000,
    "entry": 100.0, "sl_source": "trader", "status": "closed", "r": 0.5,
    "net_usdt": 12.5, "pnl_usdt": 12.5, "fee_usdt": 0.1, "funding_usdt": 0.0,
    "exit_parts": [{"reason": "TP1", "price": 105.0, "ts_ms": 1_775_810_000_000}],
}


def _load(name: str, work: Path):
    os.environ["SVB_WORK"] = str(work)
    spec = importlib.util.spec_from_file_location(name, ROOT / "backtest/engine.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_work(tmp_path):
    (tmp_path / "signals_final.json").write_text("[]", encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")


def test_prefetch_uses_canonical_symbol(tmp_path, monkeypatch):
    eng = _load("svb_engine_r13a", tmp_path)
    (tmp_path / "instruments.json").write_text(json.dumps({
        "1000PEPEUSDT": "bybit", "BTCUSDT": "bybit"}), encoding="utf-8")
    (tmp_path / "kline_windows.json").write_text(json.dumps([
        {"symbol": "PEPEUSDT", "t0": 1, "t1": 2},
        {"symbol": "BTCUSDT", "t0": 1, "t1": 2},
    ]), encoding="utf-8")
    calls = []

    class FakeM:
        FETCH_WORKERS = 0
        INSTR = {"1000PEPEUSDT": "bybit", "BTCUSDT": "bybit"}

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
    assert ("1000PEPEUSDT", "bybit") in calls, "预取必须用解析后的规范合约(1000前缀)"
    assert ("PEPEUSDT", "bybit") not in calls, "不得按未解析的窗口原始名预取"
    assert ("BTCUSDT", "bybit") in calls


def test_report_warns_when_inputs_changed_after_sim(tmp_path, monkeypatch):
    eng = _load("svb_engine_r13b", tmp_path)
    _mk_work(tmp_path)
    (tmp_path / "trades.json").write_text(json.dumps([_TRADE]), encoding="utf-8")

    class FakeSim:
        def __init__(self, rule="r_ladder", mkt=None):
            self.trades = []
            self.funding_missing = set()

        def run(self, signals, manages):
            pass

    monkeypatch.setattr(eng, "Sim", FakeSim)
    eng.phase_sim()
    meta = json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))
    assert meta.get("input_hashes"), "sim_meta 必须包含输入文件哈希快照"
    # sim 之后改输入
    (tmp_path / "signals_final.json").write_text('["changed"]', encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "输入变更" in text or "已变更" in text, "输入被改后报告必须提示, 不得静默"
    assert meta["input_hashes"].get("signals_final.json") in text, \
        "审计哈希必须是 sim 时的快照, 而非 report 时现算"


def test_report_no_warning_when_inputs_unchanged(tmp_path, monkeypatch):
    eng = _load("svb_engine_r13c", tmp_path)
    _mk_work(tmp_path)
    (tmp_path / "trades.json").write_text(json.dumps([_TRADE]), encoding="utf-8")

    class FakeSim:
        def __init__(self, rule="r_ladder", mkt=None):
            self.trades = []
            self.funding_missing = set()

        def run(self, signals, manages):
            pass

    monkeypatch.setattr(eng, "Sim", FakeSim)
    eng.phase_sim()
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "输入变更" not in text


def test_report_shows_funding_quality_in_normal_branch(tmp_path, monkeypatch):
    eng = _load("svb_engine_r13d", tmp_path)
    (tmp_path / "signals_final.json").write_text("[]", encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")
    (tmp_path / "trades.json").write_text(json.dumps([_TRADE]), encoding="utf-8")
    (tmp_path / "funding_quality.json").write_text(
        json.dumps({"missing_symbols": ["PUMPUSDT"]}), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "PUMPUSDT" in text, "正常报告也必须展示资金费缺失标的"
    assert "部分成本缺失" in text, "存在资金费缺失时净额必须标注部分成本缺失"


def test_report_cap_days_from_meta(tmp_path, monkeypatch):
    eng = _load("svb_engine_r13e", tmp_path)
    (tmp_path / "signals_final.json").write_text("[]", encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")
    (tmp_path / "trades.json").write_text(json.dumps([_TRADE]), encoding="utf-8")
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 120}), encoding="utf-8")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "120天上限" in text, "规则文案的持仓上限必须来自 sim_meta"
    assert "30天上限" not in text
