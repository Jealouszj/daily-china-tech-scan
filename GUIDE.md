# 每日中国科技海外资讯扫描 — GitHub Actions 配置指南

## 概述

每天自动搜索英文媒体源，找出**国内媒体未报道或少量报道**的中国科技财经资讯，
合成报告后自动发布到 GitHub Issue。

### 工作流程

```
GitHub Actions 定时触发 (每天 21:07 北京时间)
  → 3 轮 10 个英文搜索 (DuckDuckGo 免费 + NewsAPI 可选)
  → 去重、格式化
  → [可选] Anthropic API (Claude Sonnet) 智能合成
  → 报告存入 output/report.md
  → 自动创建 GitHub Issue
```

### 两种运行模式

| 模式 | 前提 | 报告质量 | 单次费用 |
|------|------|---------|---------|
| **深度模式** (推荐) | 配置 Anthropic API Key |  AI 分析+去伪存真 | ~$0.12 |
| **轻量模式** (默认回退) | 无 |  原始搜索聚合 | $0.00 |

---

## 第一步：创建 GitHub 仓库

```bash
# 在 github.com 创建新仓库，例如: daily-china-tech-scan
git clone git@github.com:你的用户名/daily-china-tech-scan.git
cd daily-china-tech-scan

# 从本项目复制所有文件（替换 /home/zuka 为实际路径）
cp -r /home/zuka/github-actions-daily-scan/.github .
cp -r /home/zuka/github-actions-daily-scan/scripts .
cp /home/zuka/github-actions-daily-scan/GUIDE.md .
```

最终仓库结构：
```
daily-china-tech-scan/
├── .github/
│   └── workflows/
│       └── daily-scan.yml          # Actions 工作流
├── scripts/
│   ├── daily_scan.py               # 主脚本
│   └── requirements.txt            # Python 依赖
├── output/                         # 报告输出 (自动创建)
│   ├── report.md                   # 最新报告
│   └── report-2026-07-01.md        # 历史报告
└── GUIDE.md
```

---

## 第二步：配置 GitHub Secrets

仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**

###  必选（GitHub 自动提供，无需手动设置）

| Secret | 说明 |
|--------|------|
| `GITHUB_TOKEN` | Actions 自动注入，用于创建 Issue |

###  推荐：Anthropic API Key（开启深度模式）

| Secret | 获取方式 |
|--------|---------|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys → Create Key |

有了这个 Key，报告由 Claude Sonnet 进行智能分析：
- 自动识别国内报道不足的内容
- 对比国内外叙事差异
- 信源可信度判断 + 去重
- 结构化输出 + 分析师评论

###  可选：NewsAPI Key（增强搜索覆盖）

| Secret | 获取方式 |
|--------|---------|
| `NEWSAPI_KEY` | https://newsapi.org/register（免费 100次/天）|

添加后可搜索 Bloomberg、Reuters 等正规新闻源。

---

## 第三步：推送并激活

```bash
git add .
git commit -m "feat: daily China tech overseas news scanner"
git push origin main
```

推送后 GitHub Actions 自动激活。每天 21:07 北京时间自动运行。

---

## 第四步：手动测试（推荐首次运行）

1. 仓库页面 → **Actions** tab
2. 左侧选择 **"Daily China Tech Overseas News Scan"**
3. 点击 **"Run workflow"** → 保持深度模式勾选 → **Run workflow**

等待 2-3 分钟后查看结果：

| 查看位置 | 路径 |
|----------|------|
| **GitHub Issue** | Issues 页面，带 `daily-scan` + `china-tech` 标签 |
| **Actions Artifact** | 运行详情 → Artifacts → `daily-china-tech-report` |
| **输出日志** | 运行详情 → `run-daily-scan` → 查看 stdout |

---

## 第五步：可选增强

### A. 自动提交报告到仓库

在 `daily-scan.yml` 的 steps 末尾添加：

```yaml
      - name: Commit report
        if: success()
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "actions@users.noreply.github.com"
          git add output/
          git diff --staged --quiet || git commit -m "docs: daily scan $(date +%Y-%m-%d)"
          git push
```

### B. 推送到企业微信/Slack

在脚本中加 webhook 即可：

```python
def notify_slack(url: str, issue_url: str):
    import requests
    requests.post(url, json={"text": f" 每日扫描已发布\n{issue_url}"})
```

### C. 修改执行频率

修改 cron 表达式（UTC 时间）：

```yaml
# 北京时间 21:07 = UTC 13:07
- cron: "7 13 * * *"       # 每天
# - cron: "7 13 * * 1-5"   # 仅工作日
# - cron: "7 13 * * 1,3,5" # 每周一三五
```

---

## 搜索策略（复现 agent-reach 多平台搜索）

```
第一轮（宽泛搜索）
  ├── China tech stocks Bloomberg Reuters exclusive underreported
  ├── Chinese semiconductor AI chip export controls sanctions
  └── China technology foreign media exclusive coverage

第二轮（深挖具体维度）
  ├── China data centers remove Nvidia chips mandate domestic AI
  ├── Chinese AI capability vs claims analyst report short seller
  ├── China EV battery overseas factory pushback Europe US BYD CATL
  └── Huawei SMIC advanced chip yield problems 5nm process

第三轮（补漏 & 最新）
  ├── China tech overseas expansion backlash Southeast Asia
  ├── TikTok ByteDance US ban latest
  └── China AI regulation policy data security
```

所有查询均为英文，系统自动过滤中国官媒（新华社/CGTN/China Daily/环球时报等）。

---

## 费用明细

### 深度模式（推荐）

| 项目 | 用量 | 单次费用 |
|------|------|---------|
| DuckDuckGo 搜索 | 30 次 | $0.00 |
| NewsAPI | ~30 次 | 免费 tier |
| Anthropic API (Sonnet) | ~6000 tokens | ~$0.12 |
| GitHub Actions | ~3 分钟 | 免费额度内 |
| **合计** | | **~$0.12/次 ~$3.60/月** |

### 轻量模式

全部 $0.00，但报告为原始搜索结果的简单聚合，无 AI 分析。

---

## 常见问题

### 没有 Anthropic API Key 能用吗？
**可以。** 脚本自动回退到轻量模式，用免费搜索聚合出报告。但报告不会区分"哪些是国内少报的"——这需要 AI 判断。

### API Key 安全吗？
**安全。** 存在 GitHub Secrets 中，加密存储，不会出现在日志或代码中。

### 会超 GitHub Actions 免费额度吗？
**不会。** 每天 ~3 分钟，月均 ~90 分钟。免费额度 2000 分钟/月。

### 能改成搜索其他主题吗？
**可以。** 编辑 `scripts/daily_scan.py` 中 `SEARCH_ROUNDS` 的查询词即可。

### 可以不用 GitHub 吗？
可以用 GitLab CI、Cron + 服务器、Vercel Cron Jobs 等替代。核心脚本 `daily_scan.py` 是独立的。

---

## 获取 API Keys

### Anthropic API Key
1. 访问 https://console.anthropic.com
2. 注册 → API Keys → Create Key
3. 格式：`sk-ant-api03-...`
4. 存入 GitHub Secrets → `ANTHROPIC_API_KEY`

### NewsAPI Key（可选）
1. 访问 https://newsapi.org/register
2. 注册免费账号
3. 存入 GitHub Secrets → `NEWSAPI_KEY`
