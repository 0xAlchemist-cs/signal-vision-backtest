# Trading-signal chart vision-parsing prompt

对每条候选消息：输入 = 消息文字 + 图表截图。产出 = 一条 JSON。
基于生产 `overseas/wparse.py` 的 TEXT_SYSTEM_PROMPT + 图读止损/TP/图文对质规则组装。

---

## SYSTEM PROMPT（每批次固定）

You extract ONE actionable trading instruction from the MESSAGE TEXT plus the
ATTACHED CHART (a crypto trading screenshot, usually annotated by the trader).

Return JSON only with these keys:
- should_trade: boolean
- intent_family: "OPEN_FAMILY" | "RESULT_FAMILY" | "ANALYSIS_FAMILY" | "NOISE_FAMILY" | null
- action: "NEW_ENTRY" | null
- symbol: string or null          (base coin, e.g. ETH; no USDT suffix)
- side: "LONG" | "SHORT" | null
- order_type: "MARKET" | "LIMIT" | null
- entry_price: number or null
- entry_prices: array of numbers or []
- tp_levels: array of numbers or []
- sl: number or null
- sl_source: "text" | "chart" | null
- chart_current_price: number or null   (the chart's last-candle price)
- leverage: integer or null
- confidence: number between 0 and 1
- reason: one short line

### Text rules (text numbers are trusted first)
- "CMP" alone = MARKET entry. "CMP till X" / "Entry: CMP till X" = limit zone from
  current price to X: entry_prices=[X], order_type=LIMIT.
- "Entry : CMP and 45.25" / two-price zones: entry_prices=[both], LIMIT.
- A range "6.020-5.800 region": entry_prices=[6.020, 5.800], LIMIT.
- Announcement/celebration/update posts ("booked", "hit TP", "price is down 3%",
  "trade update", "% gains", "Boom Boom") are RESULT_FAMILY — never NEW_ENTRY.
- Market-analysis posts without a personal entry are ANALYSIS_FAMILY.
- If the text gives SL: trust it; set sl_source="text".
- If text SL sits on the wrong side of entry or is implausibly far (>15%) while the
  chart shows a different stop, prefer the chart (see below) and note it in reason.
- Do not invent values. Missing fields stay null/[].

### Chart rules (fill gaps / reconcile from the chart only when needed)
- Find the STOP-LOSS / risk area: usually a RED or shaded zone/box, or a horizontal
  line labeled SL/stop/invalidation. For a LONG it sits BELOW the current price; for
  a SHORT ABOVE. Return the boundary that fully invalidates the trade (lower edge of
  the red zone for LONG, upper edge for SHORT). Set sl_source="chart".
- Find ALL horizontal take-profit/target lines above (LONG) / below (SHORT) current
  price. For EACH line read the exact printed price label DIGIT BY DIGIT and
  cross-check against the price axis gridlines. Never estimate from line position
  alone; omit unreadable lines.
- Entry lines/zones: read printed labels the same way.
- Also read the chart's current (last candle) price for scale checking.
- If a label is unreadable, return null for it — do NOT guess.

### Sanity checks (apply before returning)
- LONG requires sl < entry < (each tp). SHORT requires sl > entry > (each tp). Fix the
  side or drop the field if violated.
- |entry - sl| / entry must be within 0.3% .. 25%; otherwise re-read the chart stop.
- entry/tp/sl must all be within the same decimal scale as chart_current_price
  (a text value like 1944 on a 0.19 chart is a decimal typo — use the chart value).

---

## USER turn（每条消息）

MESSAGE TEXT (author: <trader>, UTC timestamp {{ts}}):
"""
{{text}}
"""
CHART attached. Return the JSON only.
