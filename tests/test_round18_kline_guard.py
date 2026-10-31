# -*- coding: utf-8 -*-
"""第十八轮回归测试 (审查十八: bound 行情指纹的执行时校验)。

场景: sim A → 修改 klines1m → bound A → report。
修复前: bound 用**新行情**计算, 却复制 meta 的**旧行情指纹** — 产物元数据
        与实际计算条件不一致。
修复后: phase_bound 在运行前校验磁盘行情指纹与 sim_meta 记录一致;
        不一致则拒绝执行 bound(不落盘任何产物), 提示重跑 sim+bound。
        (旧 bound 本身仍然有效 — 它属于数据一致时期的 sim A, 报告继续采信。)
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
    [BASE + 60_000, 100, 100.6, 99.8, 99.82],
]


def test_bound_refuses_when_klines_changed_since_sim(tmp_path, monkeypatch):
    eng = _load("svb_engine_r18a", tmp_path, monkeypatch)
    # klines1m 预置一个行情文件(被 sim 的指纹记录)
    (tmp_path / "klines1m").mkdir()
    kf = tmp_path / "klines1m" / "TESTUSDT.json"
    kf.write_text("[[1,2,3,4,5]]", encoding="utf-8")
    sig = {"ts_ms": BASE + 30_000, "symbol": "TEST", "side": "LONG",
           "order_type": "MARKET", "entry_prices": [], "entry_price": None,
           "sl": 99.85, "tp_levels": [], "id": "s1", "text": "long"}
    (tmp_path / "signals_final.json").write_text(json.dumps([sig]), encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")
    (tmp_path / "funding").mkdir(exist_ok=True)
    (tmp_path / "funding" / "TESTUSDT.json").write_text("[]", encoding="utf-8")

    eng.Market = lambda: FakeMarket(BARS)
    eng.phase_sim()
    meta = json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))
    assert meta.get("kline_fp"), "sim 必须记录行情指纹"
    assert meta["kline_fp"].get("TESTUSDT.json")

    # ---- sim 之后修改行情文件 ----
    kf.write_text("[[1,2,3,4,5],[6,7,8,9,10]]", encoding="utf-8")

    eng.phase_bound()
    # 修复后: 拒绝执行 → 不得落盘 bound.json(否则就是"新数据+旧指纹"的谎言产物)
    assert not (tmp_path / "bound.json").exists(), \
        "行情与 sim 时不一致, bound 必须拒绝执行而非产出'新数据+旧指纹'的产物"


def test_bound_proceeds_when_klines_unchanged(tmp_path, monkeypatch):
    eng = _load("svb_engine_r18b", tmp_path, monkeypatch)
    (tmp_path / "klines1m").mkdir()
    (tmp_path / "klines1m" / "TESTUSDT.json").write_text("[[1,2,3,4,5]]", encoding="utf-8")
    sig = {"ts_ms": BASE + 30_000, "symbol": "TEST", "side": "LONG",
           "order_type": "MARKET", "entry_prices": [], "entry_price": None,
           "sl": 99.85, "tp_levels": [], "id": "s1", "text": "long"}
    (tmp_path / "signals_final.json").write_text(json.dumps([sig]), encoding="utf-8")
    (tmp_path / "manages.json").write_text("[]", encoding="utf-8")
    (tmp_path / "funding").mkdir(exist_ok=True)
    (tmp_path / "funding" / "TESTUSDT.json").write_text("[]", encoding="utf-8")

    eng.Market = lambda: FakeMarket(BARS)
    eng.phase_sim()
    eng.phase_bound()
    assert (tmp_path / "bound.json").exists(), "行情未变时 bound 正常执行"
    meta = json.loads((tmp_path / "sim_meta.json").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "bound.json").read_text(encoding="utf-8"))
    assert b["kline_fp"] == meta["kline_fp"]
