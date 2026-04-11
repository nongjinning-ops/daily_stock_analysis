# Automation-2 执行记录

## 2026-04-08 18:00 执行摘要

- **执行结果**: ⚠️ 部分成功（大盘复盘正常，个股 AI 分析失效）
- **执行方式**: `ENABLE_CHIP_DISTRIBUTION=false ./venv/bin/python main.py --force-run`（需用 venv python，系统无 `python` 命令只有 `python3`）
- **分析股票数**: 10 只
- **报告文件**:
  - `reports/report_20260408.md`（2.0KB，个股仪表盘 - 内容为空，AI 失效）
  - `reports/market_review_20260408.md`（5.8KB，大盘复盘 - 正常）
- **推送状态**: ✅ 企业微信推送成功（分2批发送）

### 关键注意事项
- **AI Agent 全部失效**: Gemini 报 `thought_signature` 错误；OpenAI(Aihubmix) 余额不足(403)，导致所有个股评分默认 50，无实质分析内容
- **筹码分布功能崩溃**: 持续问题，必须设置 `ENABLE_CHIP_DISTRIBUTION=false`
- **fake_useragent 缺失**: 本次首次发现，已在 venv 中安装
- **Tushare Token 问题**: 持续失效，自动降级 EfinanceFetcher
- **港股 API 断连**: EfinanceFetcher/AkshareFetcher 均失败，降级到 PytdxFetcher
- **GOOGL YF 限流**: Yahoo Finance Rate Limit，降级到其他数据源

### 大盘复盘摘要（2026-04-08）
- 港股：恒生指数 +3.09%（25893），国企指数 +2.61%，南向资金净卖 277亿却指数大涨，机构接力
- 美股：纳指 +0.10%（22017），道指 -0.18%，VIX 暴跌 21% 至 20.35，恐慌情绪大退潮

## 2026-04-07 18:00 执行摘要

- **执行结果**: ✅ 成功
- **执行方式**: `ENABLE_CHIP_DISTRIBUTION=false python main.py --force-run`（后台运行）
- **分析股票数**: 12 只（含港股/美股/A股/指数）
- **报告文件**:
  - `reports/report_20260407.md`（个股决策仪表盘，26KB）
  - `reports/market_review_20260407.md`（大盘复盘，4.9KB）
- **推送状态**: 企业微信推送成功（分2批发送）

### 关键注意事项
- **筹码分布功能崩溃**: `libmini_racer.dylib` FATAL 错误导致筹码分析崩溃，须设置 `ENABLE_CHIP_DISTRIBUTION=false` 规避
- **Tushare Token 问题**: Token 限流（历史数据接口报错"您的token不对"），自动降级到 EfinanceFetcher 正常工作
- **港股今日休市**: HK00700 / HK01810 今日港股非交易日，仍被 `--force-run` 强制纳入分析
- **HSTECH 数据源问题**: Yahoo Finance 无法获取 ^HSTECH 数据

### 分析结果摘要（2026-04-07）
| 股票 | 建议 | 评分 | 趋势 |
|------|------|------|------|
| 贵州茅台(600519) | 🟢 买入 | 65 | 看多 |
| Apple Inc.(AAPL) | 🟢 买入 | 65 | 震荡 |
| NVIDIA(NVDA) | ⚪ 观望 | 65 | 震荡 |
| Alphabet(GOOGL) | 🟡 轻仓持有 | 63 | 震荡向上 |
| 纳斯达克(IXIC) | ⚪ 观望 | 60 | 震荡 |
| HSTECH | ⚪ 观望 | 55 | 震荡 |
| 上证指数(000001) | ⚪ 观望 | 50 | 震荡 |
| 宁德时代(300750) | 🟠 减仓 | 35 | 看空 |
| 腾讯控股(HK00700) | ⚪ 观望 | 35 | 看空 |
| 比亚迪(002594) | ⚪ 观望 | 25 | 看空 |
| 小米集团-W(HK01810) | ⚪ 观望 | 25 | 看空 |
| Tesla(TSLA) | 🔴 观望/卖出 | 20 | 强烈看空 |
