# Signal Vision Backtest

**AI 视觉信号解析 + 秒级事件驱动回测** —— 面向 Discord/Telegram 交易员频道的跟单研究与回测工具链。

**EN**: AI vision signal parsing + second-level event-driven backtesting for
Discord/Telegram trading-signal channels. Parse trader messages (text + chart
screenshots) with a vision LLM, then replay them against 1-minute candles with
message timestamps inserted at millisecond precision.

> ⚠️ 教育与研究用途。不构成投资建议。自动化个人账号违反 Discord ToS，请自行评估。

---

## 为什么是"视觉"解析

真实交易员频道的信号有三个特征，纯文本解析必然失败：

1. **价位在图上**：止损/止盈常只以红线/绿线画在 TradingView 截图里（我们实测一位交易员 64% 的止损只存在于图上）；
2. **口语化表达**："现价 1038 短多，止盈 1135 平一半，成本防守，980 补，945 止损"——市价入场+分批止盈+保本+补仓腿+硬止损，一个句子里五种指令；
3. **收盘价止损**："4H 收盘跌破 1865 才空"——盘中插针不算触发，只有周期收盘才算。这一条建模与否，回测结果相差数十个 R。

本工具链的做法：文字数字优先、图只补缺/纠错（逐位读标签并与坐标轴交叉核对）、
条件单打标、战报/教学帖过滤（naive 正则管线会把" booked +6R"当新开仓交易）。

## 目录结构

```
signal_parser/   # 文字+图表 → 结构化 JSON(OpenAI 兼容视觉模型: GLM/Qwen-VL/GPT-4o)
prompts/         # 视觉解析 prompt(含: 文字优先/图补缺纠错/条件单/战报过滤规则)
fetch/           # Discord 频道历史 + 图表图下载(域名白名单+私网 IP 拦截)
backtest/        # 回测引擎: 1m K线四tick展开 + 消息毫秒插入 + 事件驱动持仓管理
examples/        # 信号 JSON 样例
reports/         # 报告生成
```

## 快速开始

```bash
pip install -r requirements.txt

# 1) 配置
export SIGNAL_API_KEY=...        # OpenAI 兼容 API key
export SIGNAL_API_BASE=https://open.bigmodel.cn/api/paas/v4
export SIGNAL_MODEL=glm-4.5v     # 任意支持视觉的模型
export DISCORD_TOKEN=...         # 频道所在账号 token

# 2) 拉取频道历史 + 图
python -m fetch.discord_history <channel_id> data/channel.jsonl
python -m fetch.chart_images data/channel.jsonl data/images/

# 3) 解析一条信号
python -m signal_parser --text "Going long XMR at cmp, TPs above, 4h close for stops" --image chart.jpg

# 4) 回放
python backtest/engine.py sim    # 需要 work/signals.json + work/manages.json
```

## 回测引擎要点（backtest/engine.py）

- **四 tick 展开**：每根 1m K 线展开为 开盘(0s) → 逆行极值(20s) → 顺行极值(40s) → 收盘(60s)，
  即同根 K 线内先试探对持仓不利的方向——避免"TP/SL 同根 K 线永远先成 T P"的乐观偏差
  （设计借鉴 [VeloTradeX](https://github.com/VeloTradeX/velotradex) 的保守重放思想，MIT）。
- **消息毫秒插入**：交易员的喊单按原始时间戳插入 tick 之间，成交=消息后首个 tick。
- **两种出场规则**（`SVB_RULE`）：
  - `r_ladder`（默认）：+1R 平 40% 并移保本 → +2R 平 40% → 剩余 20% 只听喊单；
  - `tp_ladder`：完全按交易员公布的止盈位执行，N 档各平 1/N。
  我们对多位真实交易员的对照回测显示：**同一批入场，两种出场可以相差 20–70R**——
  进场有优势的交易员，其自设止盈位未必是最优出场。
- **收盘价止损**：`stop_trigger_type=close_based` 的止损只在对应周期收盘 tick 判定。
- 资金费率按持仓逐期计入；同参信号去重；同标的换新撤旧；入场 48h 超时；持仓 30 天上限。
- 敏感性：滑点/止损距离均可通过环境变量调档（`SVB_SLIP` 等）。

## 实测教训（为什么这些细节重要）

- 一位交易员 64% 的止损只存在于图上——纯文本解析直接丢失 2/3 信号；
- 把"盘中插针"误判为收盘止损触发，会让一套正期望策略在回测里变负；
- 图文币种错配（发错图/引用旧图）必须显式处理，否则图读价格污染文字信号；
- 已退市币、仅现货对、黄金/外汇 CFD 标的需要单独计数。

## License

MIT
