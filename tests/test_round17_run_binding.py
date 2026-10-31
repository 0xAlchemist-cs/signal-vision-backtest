# -*- coding: utf-8 -*-
"""第十七轮回归测试 (round-16 增补的外部审计修复)。

1. P1 bound 执行条件一致性 — bound 必须按 sim_meta 记录的**全部执行参数**
   (risk_usdt/path_mode/percent_basis/R阶梯等)实际运行, 而不是指纹写旧的、
   执行用当前的。用两个不同配置的引擎实例(模拟跨进程)做真实成交验证。
2. P1 run_id 绑定 — 每次 sim 生成唯一 run_id; bound 绑定该 ID;
   报告校验 run_id 一致, 修完 K 线缓存重跑 sim 后旧 bound 必须失效。
3. kline 数据版本指纹 — klines1m 文件(大小+mtime)摘要进 meta/bound,
   行情数据变了旧 bound 同样失效。
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


BARS = [
    [BASE, 100, 100, 100, 100.0],
    [BASE + 60_000, 100, 100.6, 99.8, 99.82],    # 入场后打损 99.85
    [BASE + 120_000, 99.8, 99.9, 99.7, 99.75],
]


def _mk_work(tmp_path):
    (tmp_path / "funding").mkdir(exist_ok=True)
    (tmp_path / "funding" / "TESTUSDT.json").write_text("[]", encoding="utf-8")
    sig = {"ts_ms": BASE + 30_000, "symbol": "TEST", "side": "LONG",
           "order_type": "MARKET", "entry_prices": [], "entry_price": None,
           "sl": 99.85, "tp_levels": [], "id": "s1", "text": "long"}
    (tmp_path / "signals_final.json").write_text(json.dumps([sig]), encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")


def _run_sim(eng, tmp_path):
    """驱动真实 phase_sim (Market 替换为 FakeMarket), 落盘 trades+meta。"""
    eng.Market = lambda: FakeMarket(BARS)
    eng.phase_sim()
    return json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))


# ---------- 1. bound 按 meta 参数实际执行 ----------

def test_bound_executes_with_meta_params(tmp_path, monkeypatch):
    """进程A(风险25/GR1_FRAC 0.4)跑 sim → 进程B(风险100/GR1_FRAC 0.9)跑 bound:
    bound 必须按 A 的参数执行 — bound.json 指纹与逐单 USDT 都必须是 25 口径。"""
    # 进程 A: 正常口径
    eng_a = _load("svb_engine_r17a", tmp_path, monkeypatch,
                  {"SVB_RISK_USDT": "25", "GR1_FRAC": "0.4"})
    _mk_work(tmp_path)
    _run_sim(eng_a, tmp_path)
    meta = json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))
    assert meta["params"]["risk_usdt"] == 25.0
    assert meta["run_id"], "sim 必须生成 run_id"
    # 进程 B: 不同配置(风险100, R1平仓90%)
    eng_b = _load("svb_engine_r17b", tmp_path, monkeypatch,
                  {"SVB_RISK_USDT": "100", "GR1_FRAC": "0.9"})
    eng_b.Market = lambda: FakeMarket(BARS)
    eng_b.phase_bound()
    b = json.loads((tmp_path / "bound.json").read_text(encoding="utf-8"))
    assert b["params"]["risk_usdt"] == 25.0, \
        "bound 指纹必须来自 meta(原sim口径), 不是当前环境"
    assert b["params"]["r1_frac"] == 0.4
    assert b["run_id"] == meta["run_id"], "bound 必须绑定产生它的那次 sim"
    # 真实成交验证: 零成本重跑的逐单 USDT 必须是 25 风险口径
    zt = [t for t in json.loads((tmp_path / "trades_zero_cost.json").read_text(encoding="utf-8"))
          if t.get("status") == "closed" and t.get("r") is not None]
    assert zt, "bound 应产生真实成交"
    for t in zt:
        assert t["pnl_usdt"] == pytest.approx(t["r"] * 25.0, abs=0.11), \
            f"bound 逐单 pnl 按 {t['r'] * 25.0:.1f}U(25口径) 结算, 实得 {t['pnl_usdt']}"


# ---------- 2. run_id 绑定 ----------

def _pair_files(eng, tmp_path, run_meta="A", run_bound="A", kline_meta=None,
                kline_bound=None):
    _mk_work(tmp_path)
    t = {"trader": "t", "symbol": "TESTUSDT", "base": "TEST", "side": "LONG",
         "signal_ts_ms": BASE, "first_fill_ms": BASE,
         "entry": 100.0, "stop_init": 97.0, "sl_source": "trader",
         "status": "closed", "r": -0.8, "pnl_usdt": -20.0,
         "fee_usdt": 2.0, "funding_usdt": 0.0, "net_usdt": -22.0,
         "legs": [], "exit_parts": []}
    (tmp_path / "trades.json").write_text(json.dumps([t]), encoding="utf-8")
    h = eng._audit_hashes()
    params = eng._sim_params()
    kf_m = {"TESTUSDT.json": "100-111"} if kline_meta is None else kline_meta
    kf_b = {"TESTUSDT.json": "100-111"} if kline_bound is None else kline_bound
    (tmp_path / "sim_meta.json").write_text(json.dumps({
        "rule": "tp_ladder", "slip": 0.0005, "percent_basis": "original",
        "risk_usdt": 25.0, "position_cap_d": 30, "params": params,
        "input_hashes": h, "run_id": run_meta, "kline_fp": kf_m}), encoding="utf-8")
    (tmp_path / "bound.json").write_text(json.dumps({
        "n": 1, "sum_r": 6.0, "net_sum_r": 6.0, "rule": "tp_ladder",
        "params": params, "input_hashes": h,
        "run_id": run_bound, "kline_fp": kf_b}), encoding="utf-8")


def test_run_id_mismatch_invalidates_bound(tmp_path, monkeypatch):
    eng = _load("svb_engine_r17c", tmp_path, monkeypatch)
    _pair_files(eng, tmp_path, run_meta="runA", run_bound="runOLD")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "已忽略" in text, "run_id 不一致的 bound 必须失效"
    assert "零成本边界: ΣR +6.00" not in text


def test_run_id_match_accepted(tmp_path, monkeypatch):
    eng = _load("svb_engine_r17d", tmp_path, monkeypatch)
    _pair_files(eng, tmp_path, run_meta="runA", run_bound="runA")
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "零成本边界: ΣR +6.00" in text


def test_resim_generates_new_run_id(tmp_path, monkeypatch):
    """修完 K 线重跑 sim → run_id 必须变化(旧 bound 因此失效)。"""
    eng = _load("svb_engine_r17e", tmp_path, monkeypatch)
    _mk_work(tmp_path)
    eng.Market = lambda: FakeMarket(BARS)
    eng.phase_sim()
    m1 = json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))
    eng.phase_sim()
    m2 = json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))
    assert m1["run_id"] != m2["run_id"], "每次 sim 必须有独立 run_id"


# ---------- 3. kline 数据版本指纹 ----------

def test_kline_fp_mismatch_invalidates_bound(tmp_path, monkeypatch):
    """行情缓存被修复/重拉后, 旧 bound 不得为新结论背书。"""
    eng = _load("svb_engine_r17f", tmp_path, monkeypatch)
    _pair_files(eng, tmp_path, run_meta="runA", run_bound="runA",
                kline_meta={"TESTUSDT.json": "100-111"},
                kline_bound={"TESTUSDT.json": "100-999"})
    eng.phase_report()
    text = (tmp_path / "report_v3.md").read_text(encoding="utf-8")
    assert "已忽略" in text, "行情数据指纹不一致的 bound 必须失效"
    assert "零成本边界: ΣR +6.00" not in text
