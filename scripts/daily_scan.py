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
import re
import sys
import json
import time
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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


def search_google_news(query: str, site_filter: str = "",
                     max_results: int = 10) -> list[dict]:
    """Use Google News RSS feed — free, no API key, no rate limits.
    Reliable in GitHub Actions unlike DDG which rate-limits shared IPs."""
    results = []

    # If site_filter provided, append as search terms (RSS doesn't support site: syntax)
    full_query = f"{query} {site_filter}".strip()

    url = ("https://news.google.com/rss/search?"
           f"q={urllib.parse.quote(full_query)}"
           "&hl=en-US&gl=US&ceid=US:en")

    try:
        import requests as reqs
        resp = reqs.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; daily-china-scan/1.0)"}, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

        for item in root.findall('.//item')[:max_results]:
            title = item.findtext('title', '')
            link = item.findtext('link', '')
            desc = item.findtext('description', '')
            source_elem = item.find('source')
            source = source_elem.text if source_elem is not None else ''
            # Clean description (remove HTML)
            if desc:
                desc = desc.replace('<p>', '').replace('</p>', '')
                desc = ' '.join(desc.split())[:300]
            results.append({
                "title": title,
                "url": link,
                "snippet": desc,
                "source": source,
            })
        return results
    except Exception as e:
        print(f"    Google News error: {type(e).__name__}: {e}")
        return []


def search_duckduckgo(query: str, site_filter: str = "",
                     max_results: int = 5, **kwargs) -> list[dict]:
    """DDG fallback — kept for local testing, not used in CI by default."""
    # Skip DDG entirely in CI (GitHub Actions) to avoid rate limits
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return []
    try:
        from duckduckgo_search import DDGS
        full_query = f"{query} {site_filter}".strip()
        results = []
        with DDGS() as ddgs:
            for r in ddgs.news(full_query, max_results=max_results, timelimit="d"):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("body", ""),
                    "source": r.get("source", ""),
                })
        return results
    except Exception:
        return []


def search_newsapi(query: str, api_key: str | None) -> list[dict]:
    """Use NewsAPI for structured news (free tier: 100 req/day)."""
    if not api_key:
        return []
    try:
        import requests
        from_24h_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        url = "https://newsapi.org/v2/everything"
        resp = requests.get(url, params={
            "q": query,
            "from": from_24h_ago,
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
        # Extract text from all content blocks (skip ThinkingBlock from DeepSeek)
        text_parts = []
        for block in response.content:
            if hasattr(block, 'text'):
                text_parts.append(block.text)
        return "\n".join(text_parts) if text_parts else "(empty response)"
    except Exception as e:
        print(f"[ERROR] API call failed: {e}")
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
        from github import Auth, Github
        g = Github(auth=Auth.Token(token))
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


def send_email(report: str, title: str) -> bool:
    """Send report via SMTP email."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = os.environ.get("SMTP_PORT", "587")
    smtp_username = os.environ.get("SMTP_USERNAME", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    email_from = os.environ.get("EMAIL_FROM", "")
    email_to = os.environ.get("EMAIL_TO", "")

    if not all([smtp_host, smtp_username, smtp_password, email_from, email_to]):
        print("[INFO] Email skipped: SMTP config not complete")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = title
        msg["From"] = email_from
        msg["To"] = email_to

        # Plain text fallback (strip markdown for readability)
        plain = report.replace("#", "").replace("*", "").replace("`", "")[:5000]
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(report, "html", "utf-8"))

        port = int(smtp_port)
        if port == 465:
            with smtplib.SMTP_SSL(smtp_host, port, timeout=30) as server:
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=30) as server:
                server.starttls()
                server.login(smtp_username, smtp_password)
                server.send_message(msg)

        print(f"[OK] Email sent to {email_to}")
        return True
    except Exception as e:
        print(f"[ERROR] Email failed: {e}")
        return False


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
            # Primary: Google News RSS (free, reliable in CI)
            gn_results = search_google_news(query, site_filter=site_filter)
            all_results.extend(gn_results)
            print(f"    Google News: {len(gn_results)} results")
            # Secondary: DDG (local only, skipped in CI)
            ddg_results = search_duckduckgo(query, site_filter=site_filter)
            all_results.extend(ddg_results)
            if ddg_results:
                print(f"    DDG: {len(ddg_results)} results")
            # Tertiary: NewsAPI (if key configured)
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

    # --- Merge YouTube report if available ---
    youtube_report_path = Path("output/youtube-report.md")
    if youtube_report_path.exists():
        youtube_content = youtube_report_path.read_text(encoding="utf-8")
        # Strip the top-level heading from YouTube report (already has ##)
        youtube_content = re.sub(r'^# 📺.*\n', '', youtube_content)
        report += f"\n\n---\n\n{youtube_content.strip()}\n"
        print("[INFO] Merged YouTube report into daily report")

    # --- Merge Holdings report if available ---
    holdings_report_path = Path("output/holdings-report.md")
    if holdings_report_path.exists():
        holdings_content = holdings_report_path.read_text(encoding="utf-8")
        # Strip the top-level heading from holdings report
        holdings_content = re.sub(r'^# 📊.*\n', '', holdings_content)
        report += f"\n\n---\n\n{holdings_content.strip()}\n"
        print("[INFO] Merged holdings report into daily report")

    # --- Output ---
    output_dir = Path("output")
    report_path = save_report(report, output_dir)

    # --- Email ---
    send_email(report, title)

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
