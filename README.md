# Signal Vision Backtest

[![CI](https://github.com/0xAlchemist-cs/signal-vision-backtest/actions/workflows/ci.yml/badge.svg)](https://github.com/0xAlchemist-cs/signal-vision-backtest/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-80%20passed-brightgreen)](https://github.com/0xAlchemist-cs/signal-vision-backtest/actions/workflows/ci.yml)

**English** | [简体中文](#简体中文)

---

## English

A research toolchain for backtesting trader-signal channels: feed Discord/Telegram **text + chart screenshots** through a vision LLM (any OpenAI-compatible API) to extract structured trade instructions, then replay them bar-by-bar on 1-minute OHLC with an explicit intra-bar price-path assumption, answering one question —

> **"What if you had followed this trader?"**

> ⚠️ **Disclaimer**: research tooling only, **not investment advice**. Automating Discord with a personal account (self-bot) violates their ToS. Backtests are not live results.

### Features

- **Vision-first parsing** — text and charts go to the LLM together; text numbers take priority, charts fill gaps. In one measured channel, **64% of stop-losses existed only as lines on chart images** — text-only parsing loses them.
- **Signal semantics**: `conditional` (trigger-only entries, 48h timeout), `stop_trigger_type=close_based` (candle-close stops — intrabar wicks do not trigger), `intent_family` (OPEN / RESULT / ANALYSIS / NOISE — PnL-brag posts never pollute the sample).
- **Two exit rules, same entries** (`SVB_RULE`): `r_ladder` (+1R take 40% & move stop to BE → +2R take 40% → rest follows trader calls) vs `tp_ladder` (follow the trader's own TPs, 1/N each). Same entries, different rules, very different conclusions — entry edge ≠ the trader's own TPs being the best exit.
- **Real cost model**: market/stop slippage (tunable for sensitivity matrices), taker fees, funding rates per holding period.
- **Auditable**: SHA-256 hashes of corpus/signals/manages are bound to each run (`sim_meta.json`); reports flag input files modified after the simulation.
- **Conservative matching**: each 1m bar expands to 4 synthetic ticks (open → adverse extreme → favorable extreme → close); messages insert at millisecond precision; same-bar SL-before-TP by default; gap-through stops fill at the worse executable price.

### Data sources

**Crypto perpetuals (built-in, public endpoints, no API key):**

| Purpose | Primary | Fallback |
|---|---|---|
| 1m OHLC klines | Bybit v5 `GET /v5/market/kline?category=linear&symbol=…&interval=1&start=…&end=…&limit=1000` | Binance Futures `GET https://fapi.binance.com/fapi/v1/klines?symbol=…&interval=1m&limit=1000` (only for symbols not listed on Bybit) |
| Instrument universe | Bybit `/v5/market/instruments-info` (paginated) | Binance `fapi/v1/exchangeInfo` (appended as fallback symbols) |
| Funding rates | Bybit `/v5/market/funding/history` | Binance `fapi` funding endpoint |

- Symbol resolution: `BTC` → `BTCUSDT` → `1000BTCUSDT`-style aliases; the resolution result (canonical symbol + exchange + multiplier) is used consistently for cache key, API request and price math.
- Requests go through an HTTPS host whitelist (`api.bybit.com`, `fapi.binance.com`), resolved-IP private-range blocking, no-redirect policy and 429 `Retry-After` backoff. Caches land in `SVB_WORK/klines1m/<SYMBOL>.json` and `SVB_WORK/instruments.json`.

**CFD (Gate TradFi — bring your own data, the engine's network layer is crypto-only by design):**

- CFD spot quotes come from the **Gate TradFi API**: `GET /api/v4/tradfi/symbols/XAUUSD/klines` (second timestamps → convert to milliseconds). Follow the instrument you actually trade — CFD spot quotes, not futures/contract klines; basis mismatch mis-flags TP hits.
- Delivery: write bars into `SVB_WORK/klines1m/<SYMBOL>.json` (`[ts_ms, open, high, low, close]`). **Before feeding the engine, flat-fill weekend/holiday closure gaps with carried minute bars and verify the gap count is zero** — otherwise the crypto engine sees the closure as missing data and pulls the same-named crypto token (e.g. XAU → XAUT), mixing two price systems at the seam.
- The single-symbol voice engine ([`backtest/voice_event_engine.py`](backtest/voice_event_engine.py)) takes any `--klines` JSON file, so Gate-exported CFD klines plug in directly.

### Requirements & install

- Python ≥ 3.10

```bash
pip install -r requirements.txt
```

### Configuration

| Variable | Purpose | Default |
|---|---|---|
| `SIGNAL_API_KEY` / `SIGNAL_API_BASE` / `SIGNAL_MODEL` | Vision LLM (HTTPS enforced) | GLM endpoint / `glm-4.5v` |
| `DISCORD_TOKEN` | Discord token (local only, never commit) | — |
| `SVB_WORK` | Working dir (signals / manages / kline cache / meta) | `./work` |
| `SVB_RULE` | Exit rule: `r_ladder` / `tp_ladder` | `r_ladder` |
| `SVB_RISK_USDT` | Fixed risk per signal (USDT) | `25` |
| `SVB_SLIP` | Market/stop slippage per side | `0.0005` |
| `SVB_PERCENT_BASIS` | Partial-TP basis: `original` / `remaining` | `original` |
| `SVB_PATH_MODE` | Intra-bar tick order: `synthetic` / `low_first` / `high_first` | `synthetic` |
| `SVB_POSITION_CAP_D` | Max holding days per position | `30` |
| `GR1_LVL` / `GR1_FRAC` / `GR2_LVL` / `GR2_FRAC` | R-ladder rungs | `1.0 / 0.40 / 2.0 / 0.40` |

### Quick start

```bash
# 1) pull channel history + chart images
export DISCORD_TOKEN=...
python -m fetch.discord_history <channel_id> data/channel.jsonl
python -m fetch.chart_images data/channel.jsonl data/images/

# 2) parse one signal (text + image) with a vision LLM
export SIGNAL_API_KEY=...
python -m signal_parser --text "long ETH at cmp, 4h close under 1865 stops, tp 1750" --image chart.jpg

# 3) backtest (signals.json / manages.json in SVB_WORK — see examples/)
export SVB_WORK=./work
python backtest/engine.py sim      # fills trades.json + sim_meta.json
python backtest/engine.py report   # renders report_v3.md
```

Signal JSON contract lives in [`examples/`](examples/) (`signal.json`, `conditional_signal.json`).

### Known limits (read before trusting any number)

1. Intra-bar path is an assumption, not recorded data — for tight-stop (<0.3%) scalpers run pessimistic/optimistic path modes; if the zero-slippage bound flips the sign, the honest conclusion is "undecidable at 1m granularity", not "profitable".
2. Survivorship bias: kline fetch is based on currently listed contracts; delisted symbols are absent.
3. Funding fetch failures count as zero; fee/slippage are constant assumptions.
4. Hedge-style traders (simultaneous long+short on one symbol) exceed the engine's one-position-per-symbol contract — a net-exposure convention must be declared up front.
5. Only successfully parsed trades are counted; pair the audit hashes with the candidate file for coverage review.

### Testing

```bash
python -m pytest tests/ -q
```

### License

MIT

---

## 简体中文

一套交易员频道信号研究工具链：把 Discord/Telegram 频道里的**文字 + 图表截图**交给视觉大模型（任意 OpenAI 兼容接口）解析成结构化交易指令，再以显式的分钟内价格路径假设在 1 分钟 OHLC 上逐条回放，回答一个问题——

> **"如果当时跟了这位交易员，会怎么样？"**

> ⚠️ **免责声明**：仅供技术研究，**不构成投资建议**。用个人账号自动化访问 Discord（self-bot）违反其服务条款。回测不代表实盘。

### 功能特性

- **视觉直解**：文字与图表一并交给大模型，文字数字优先、图只补缺纠错。实测某频道 **64% 的止损只存在于图上**——纯文本解析必然丢信号。
- **信号语义打标**：`conditional`（条件单，48h 未触发计未成交）、`stop_trigger_type=close_based`（收盘价止损，盘中插针不触发）、`intent_family`（OPEN/RESULT/ANALYSIS/NOISE 四族——战报帖不会污染回测样本）。
- **同批信号双出场规则对照**（`SVB_RULE`）：`r_ladder`（+1R 平40%并移保本 → +2R 再平40% → 剩余只听喊单）对照 `tp_ladder`（完全照抄交易员自设止盈，每档平 1/N）。同一批入场，两种规则结论可能完全相反——进场有优势 ≠ 自设止盈是最优出场。
- **真实成本建模**：市价/止损滑点（可调档做敏感性矩阵）、taker 手续费、逐持仓期真实资金费率。
- **可审计**：语料/信号/管理消息的 SHA-256 哈希随 `sim_meta.json` 与本次运行绑定；报告会标注 sim 之后被修改过的输入文件。
- **保守撮合**：每根 1m K 线展开为 4 个合成 tick（开盘 → 逆行极值 → 顺行极值 → 收盘），消息按毫秒精度插入 tick 之间；同 bar 默认先查止损；跳空穿越止损按更差可执行价成交。

### 数据源

**加密永续（内置，公共端点，无需 API Key）：**

| 用途 | 主源 | 兜底 |
|---|---|---|
| 1m OHLC K线 | Bybit v5 `GET /v5/market/kline?category=linear&symbol=…&interval=1&start=…&end=…&limit=1000` | Binance 合约 `GET https://fapi.binance.com/fapi/v1/klines?symbol=…&interval=1m&limit=1000`（仅用于 Bybit 未上架的标的） |
| 合约清单 | Bybit `/v5/market/instruments-info`（翻页） | Binance `fapi/v1/exchangeInfo`（作为兜底标的并入） |
| 资金费率 | Bybit `/v5/market/funding/history` | Binance `fapi` 资金费端点 |

- 标的解析：`BTC` → `BTCUSDT` → `1000BTCUSDT` 式别名；解析结果（规范合约 + 交易所 + 价格倍数）在缓存键、API 请求与价格计算三处保持一致。
- 全部请求经 HTTPS 域名白名单（`api.bybit.com`、`fapi.binance.com`）+ 解析 IP 私网拦截 + 禁重定向 + 429 `Retry-After` 退避。缓存写入 `SVB_WORK/klines1m/<SYMBOL>.json` 与 `SVB_WORK/instruments.json`。

**CFD（Gate TradFi——自带数据，引擎网络层刻意只接加密行情）：**

- CFD 现货报价来自 **Gate TradFi API**：`GET /api/v4/tradfi/symbols/XAUUSD/klines`（秒级时间戳 → 转毫秒）。跟单品种用什么报价就用什么回测——CFD 现货报价，不要拿期货/合约 K 线凑，基差会误判止盈触达。
- 接入方式：把 K 线写进 `SVB_WORK/klines1m/<SYMBOL>.json`（`[ts_ms, open, high, low, close]`）。**喂给引擎前必须用平盘 minute 填充周末/假日休市缺口，并校验缺口数为 0**——否则加密引擎会把休市判定为数据缺口，转而拉取同名加密代币（如 XAU → XAUT）补洞，两种价格体系在接缝处互相污染。
- 单标的喊话引擎（[`backtest/voice_event_engine.py`](backtest/voice_event_engine.py)）直接吃 `--klines` JSON 文件，Gate 导出的 CFD K线可以直接接入。

### 环境要求与安装

- Python ≥ 3.10

```bash
pip install -r requirements.txt
```

### 配置

| 变量 | 说明 | 默认 |
|---|---|---|
| `SIGNAL_API_KEY` / `SIGNAL_API_BASE` / `SIGNAL_MODEL` | 视觉大模型（强制 https） | GLM 官方端点 / `glm-4.5v` |
| `DISCORD_TOKEN` | Discord token（仅本地使用，勿提交） | — |
| `SVB_WORK` | 回测工作目录（signals/manages/K线缓存/meta） | `./work` |
| `SVB_RULE` | 出场规则：`r_ladder` / `tp_ladder` | `r_ladder` |
| `SVB_RISK_USDT` | 每单固定风险（U） | `25` |
| `SVB_SLIP` | 市价/止损单滑点（每边） | `0.0005` |
| `SVB_PERCENT_BASIS` | 部分止盈口径：`original` / `remaining` | `original` |
| `SVB_PATH_MODE` | 分钟内 tick 顺序：`synthetic` / `low_first` / `high_first` | `synthetic` |
| `SVB_POSITION_CAP_D` | 单仓最长持仓天数 | `30` |
| `GR1_LVL` / `GR1_FRAC` / `GR2_LVL` / `GR2_FRAC` | R 阶梯参数 | `1.0 / 0.40 / 2.0 / 0.40` |

### 快速开始

```bash
# 1) 拉取频道历史 + 图表
export DISCORD_TOKEN=...
python -m fetch.discord_history <channel_id> data/channel.jsonl
python -m fetch.chart_images data/channel.jsonl data/images/

# 2) 解析一条信号（文字 + 图）
export SIGNAL_API_KEY=...
python -m signal_parser --text "long ETH at cmp, 4h close under 1865 stops, tp 1750" --image chart.jpg

# 3) 回测（SVB_WORK 下准备 signals.json / manages.json，样例见 examples/）
export SVB_WORK=./work
python backtest/engine.py sim      # 产出 trades.json + sim_meta.json
python backtest/engine.py report   # 渲染 report_v3.md
```

信号 JSON 契约见 [`examples/`](examples/)（`signal.json`、`conditional_signal.json`）。

### 已知边界（信任任何数字前必读）

1. 分钟内路径是假设而非采集数据——紧止损（<0.3%）剥头皮频道必须配合悲观/乐观路径对照；若零滑点边界翻正，诚实结论是"1m 粒度不可判定"，而不是"盈利"。
2. 幸存者偏差：K线按当前在市合约拉取，退市标的不在数据集中。
3. 资金费拉取失败按 0 计；手续费/滑点为常量假设。
4. 对冲型交易员（同标的同时持多空）超出引擎单标的单仓契约——需要显式声明净敞口折算约定。
5. 报告只统计成功解析的交易；覆盖率核查请配合审计哈希与候选文件。

### 测试

```bash
python -m pytest tests/ -q
```

### 许可证

MIT
