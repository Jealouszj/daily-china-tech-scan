#!/usr/bin/env python3
"""
Daily China Tech Overseas News Scanner
=======================================
复现 agent-reach 多平台搜索流程：
WebSearch(英文源) → 关键文章抓取 → Anthropic API 合成 → GitHub Issue 发布

兼容两种模式：
- 深度模式 (DEEP_MODE=true): 使用 Anthropic API 做多轮智能合成
- 轻量模式 (DEEP_MODE=false): 纯搜索聚合，不消耗 API token
"""

import os
import sys
import json
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- 搜索轮次定义（短关键词 + 英文源限制，适合 DDG news 搜索）---
SEARCH_ROUNDS = [
    {
        "label": "Round 1: Broad financial news",
        "queries": [
            "China technology stocks",
            "China semiconductor chip export",
            "China AI artificial intelligence",
        ],
        "site_filter": "",  # 不做站点限制，广撒网
    },
    {
        "label": "Round 2: Specific angles",
        "queries": [
            "China EV battery electric vehicle",
            "Huawei SMIC chip manufacturing",
            "China data center Nvidia ban",
            "ByteDance TikTok US ban",
        ],
        "site_filter": "",
    },
    {
        "label": "Round 3: Premium sources (Bloomberg Reuters SCMP)",
        "queries": [
            "China tech",
            "China stock market",
            "China AI chip",
        ],
        "site_filter": "bloomberg.com OR reuters.com OR scmp.com OR ft.com OR wsj.com",
    },
]

SYNTHESIS_SYSTEM_PROMPT = """You are a senior financial and technology research analyst specializing in
cross-referencing Chinese and Western media coverage.

## Your Task
Given raw search results from multiple rounds of English-language web searches about Chinese
technology and financial news, produce a comprehensive markdown report.

## Report Structure (follow exactly):

### Section 1: Explosive / Underreported Stories
Ranked by significance. For each story include:
- **Source**: Which foreign media outlet (Bloomberg, Reuters, SCMP, etc.)
- **Domestic Coverage Level**: one of: 几乎不报 / 极低 / 有些但轻描淡写
- **Key Facts**: 3-5 bullet points with specific data and numbers
- **Why Underreported**: 1-2 sentences analyzing the reason

### Section 2: Notable But Less Explosive
Same format, shorter entries.

### Section 3: Summary Table
| Topic | Domestic Coverage | Significance | Source |

### Section 4: Analyst Commentary
2-3 paragraphs on cross-cutting themes and what to watch next.

## Critical Rules
1. Prioritize stories with specific numbers, dates, and named sources
2. Cross-reference: if multiple foreign outlets report the same thing, flag it as more credible
3. Clearly separate confirmed facts from analyst opinions
4. Filter out Chinese state media (Xinhua, CGTN, China Daily, Global Times, ECNS, People's Daily, CRI) — these are domestic sources, not foreign
5. Include URLs for key articles
6. Date the report with today's date
7. Write in Chinese (the user is Chinese-speaking) but preserve key English terms and company names
8. Add a disclaimer: "以上信息整理自公开外媒报道，不代表本人观点，仅供参考。"
"""


def search_duckduckgo(query: str, site_filter: str = "",
                     max_results: int = 8, time_limit: str = "w") -> list[dict]:
    """Use DuckDuckGo news search (free, no API key).
    Falls back to text search if news returns nothing.
    time_limit: 'd' (day), 'w' (week), 'm' (month)."""
    try:
        from duckduckgo_search import DDGS
        results = []

        # Build query with optional site filter
        full_query = f"{query} {site_filter}".strip()

        with DDGS() as ddgs:
            # Primary: news search with time filter
            for r in ddgs.news(full_query, max_results=max_results, timelimit=time_limit):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("body", ""),
                    "source": r.get("source", ""),
                })

            # Fallback: text search if news returned nothing
            if not results:
                print(f"    News search empty, falling back to text search...")
                for r in ddgs.text(full_query, max_results=max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", ""),
                    })

        return results
    except ImportError:
        print("[WARN] duckduckgo-search not installed, skipping DDG")
        return []


def search_newsapi(query: str, api_key: str | None) -> list[dict]:
    """Use NewsAPI for structured news (free tier: 100 req/day)."""
    if not api_key:
        return []
    try:
        import requests
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = "https://newsapi.org/v2/everything"
        resp = requests.get(url, params={
            "q": query,
            "from": today,
            "language": "en",
            "sortBy": "relevancy",
            "pageSize": 5,
            "apiKey": api_key,
        }, timeout=10)
        if resp.status_code != 200:
            print(f"[WARN] NewsAPI error: {resp.status_code} {resp.text[:200]}")
            return []
        data = resp.json()
        return [
            {
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "snippet": a.get("description", ""),
                "source": a.get("source", {}).get("name", ""),
            }
            for a in data.get("articles", [])
        ]
    except Exception as e:
        print(f"[WARN] NewsAPI failed: {e}")
        return []


def deduplicate_results(all_results: list[dict]) -> list[dict]:
    """Deduplicate by URL hash."""
    seen = set()
    unique = []
    for r in all_results:
        key = hashlib.md5(r["url"].encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def format_search_results(results: list[dict], max_items: int = 50) -> str:
    """Format search results as text for Anthropic API synthesis."""
    lines = []
    for i, r in enumerate(results[:max_items], 1):
        lines.append(f"{i}. **{r['title']}**")
        lines.append(f"   URL: {r['url']}")
        lines.append(f"   Snippet: {r['snippet'][:300]}")
        source = r.get("source", "")
        if source:
            lines.append(f"   Source: {source}")
        lines.append("")
    return "\n".join(lines)


def synthesize_with_claude(search_text: str, api_key: str,
                          base_url: str = "https://api.anthropic.com",
                          model: str = "claude-sonnet-4-6") -> str:
    """Use Anthropic-compatible API (Anthropic / DeepSeek / OpenRouter)."""
    import anthropic

    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
    )
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")

    prompt = f"""Today is {today_str}.

Below are raw search results from multiple rounds of English-language web searches
about Chinese technology and financial news. Synthesize them into a comprehensive
report following the structure and rules in your system prompt.

=== RAW SEARCH RESULTS ===
{search_text}
=== END RAW RESULTS ===

Produce the full report now. Focus on stories that Chinese domestic media
would NOT cover, or cover very differently. Be specific with numbers and sources."""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYNTHESIS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"[ERROR] Anthropic API failed: {e}")
        return f"API 合成失败: {e}\n\n以下为原始搜索结果：\n\n{search_text}"


def lightweight_synthesis(search_text: str, all_results: list[dict]) -> str:
    """Lightweight mode: template-based aggregation without AI."""
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")

    report = f"""# 🌍 中国科技财经海外资讯扫描

> 生成时间：{today_str}（北京时间）
> 模式：轻量聚合（未使用 AI 合成）

---

## ⚠️ 轻量模式

本报告为多源搜索的自动聚合，未经 AI 深度分析和去重。
**开启深度模式**：在仓库 Settings → Secrets → 添加 `ANTHROPIC_API_KEY`。

---

## 📰 搜索结果（按来源分组）

"""
    by_source: dict[str, list] = {}
    for r in all_results:
        src = r.get("source", "未标注来源")
        by_source.setdefault(src, []).append(r)

    for src, items in sorted(by_source.items(), key=lambda x: -len(x[1])):
        report += f"### {src} ({len(items)} 条)\n\n"
        for item in items[:5]:
            report += f"- **{item['title']}**\n"
            report += f"  {item['snippet'][:200]}\n"
            report += f"  {item['url']}\n\n"

    report += """
---

> ⚠️ 以上为自动搜索聚合，未经人工核实。在 Settings → Secrets → 添加 `ANTHROPIC_API_KEY` 即可开启 AI 深度分析。
"""
    return report


def create_github_issue(repo: str, title: str, body: str, token: str) -> str | None:
    """Create a GitHub issue with the report."""
    try:
        from github import Github
        g = Github(token)
        repo_obj = g.get_repo(repo)
        issue = repo_obj.create_issue(
            title=title,
            body=body,
            labels=["daily-scan", "china-tech"],
        )
        return issue.html_url
    except Exception as e:
        print(f"[ERROR] Failed to create GitHub issue: {e}")
        return None


def save_report(report: str, output_dir: Path) -> Path:
    """Save report to output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = output_dir / f"report-{today}.md"
    filepath.write_text(report, encoding="utf-8")
    latest = output_dir / "report.md"
    latest.write_text(report, encoding="utf-8")
    print(f"[OK] Report saved to {filepath}")
    return filepath


def main():
    print("=" * 60)
    print("Daily China Tech Overseas News Scanner")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    newsapi_key = os.environ.get("NEWSAPI_KEY", "")
    deep_mode = os.environ.get("DEEP_MODE", "true").lower() == "true"
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    use_ai = deep_mode and bool(anthropic_key)
    print(f"Mode: {'AI Deep' if use_ai else 'Lightweight (no AI)'}")

    # --- Multi-round search ---
    all_results: list[dict] = []
    for round_def in SEARCH_ROUNDS:
        print(f"\n--- {round_def['label']} ---")
        site_filter = round_def.get("site_filter", "")
        for query in round_def["queries"]:
            filter_info = f" [site: {site_filter[:50]}...]" if site_filter else ""
            print(f"  Searching: {query}{filter_info}")
            ddg_results = search_duckduckgo(query, site_filter=site_filter)
            all_results.extend(ddg_results)
            print(f"    DDG news: {len(ddg_results)} results")
            news_results = search_newsapi(query, newsapi_key)
            all_results.extend(news_results)
            if news_results:
                print(f"    NewsAPI: {len(news_results)} results")

    unique_results = deduplicate_results(all_results)
    print(f"\n[INFO] Total raw: {len(all_results)}, unique: {len(unique_results)}")

    search_text = format_search_results(unique_results)

    # --- Synthesize ---
    today_cn = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    title = f"每日中国科技海外资讯扫描 — {today_cn}"

    if use_ai:
        print("\n[INFO] Synthesizing with Anthropic API...")
        synthesis = synthesize_with_claude(search_text, anthropic_key,
                                            base_url=anthropic_base_url,
                                            model=anthropic_model)
        report = f"""# 🌍 {title}

> 🤖 由 GitHub Actions + Anthropic API 自动生成 | {today_cn} 21:03 北京时间
> 搜索策略：3 轮 10 个英文查询 → 去重 → Claude Sonnet 合成

{synthesis}
"""
    else:
        print("\n[INFO] Using lightweight aggregation...")
        inner = lightweight_synthesis(search_text, unique_results)
        report = f"# 🌍 {title}\n\n{inner}"

    # --- Output ---
    output_dir = Path("output")
    report_path = save_report(report, output_dir)

    # --- GitHub Issue ---
    issue_url = None
    if github_token and repo:
        print(f"\n[INFO] Creating GitHub issue in {repo}...")
        issue_url = create_github_issue(repo, title, report, github_token)
        if issue_url:
            print(f"[OK] Issue: {issue_url}")

    print("\n" + "=" * 60)
    print("SCAN COMPLETE")
    print(f"Unique results: {len(unique_results)}")
    print(f"Report: {report_path}")
    if issue_url:
        print(f"Issue: {issue_url}")
    print(f"Mode: {'AI' if use_ai else 'Lightweight'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
