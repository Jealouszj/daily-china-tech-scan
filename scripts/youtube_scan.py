#!/usr/bin/env python3
"""
YouTube Channel Update Scanner (v2 — token-efficient)
======================================================
多层级内容提取策略，大幅节省 AI token：
  Tier 0: 标题自摘要检测（免费，0 token）
  Tier 1: 简介清洗+结构化提取（免费，0 token）
  Tier 2: AI 轻量摘要（仅在 Tier 0+1 不足时触发，~800 token/视频）
  Tier 3: 跨频道 AI 合成日报（一次性，~2000 token）

核心原则：能用标题/简介解决的问题，绝不用 AI。
"""

import os
import sys
import re
import json
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# Channel config
# ═══════════════════════════════════════════════════════════════

def parse_channels(raw: str) -> list[tuple[str, str]]:
    """Parse channel list, return [(label, url), ...].
    Accepts: @handle, UC...id, or full https:// URL."""
    from urllib.parse import unquote
    channels = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("https://"):
            url = token.rstrip("/")
            if "/videos" not in url:
                url += "/videos"
            if "/@" in url:
                label = unquote(url.split("/@")[1].split("/")[0])
            elif "/channel/" in url:
                label = unquote(url.split("/channel/")[1].split("/")[0][:12])
            else:
                label = unquote(url)
        elif token.startswith("@"):
            label = unquote(token[1:])
            url = f"https://www.youtube.com/@{label}/videos"
        elif token.startswith("UC"):
            label = unquote(token[:16])
            url = f"https://www.youtube.com/channel/{token}/videos"
        else:
            print(f"[WARN] Unknown channel format: {token}, skipping")
            continue
        channels.append((label, url))
    return channels


# ═══════════════════════════════════════════════════════════════
# YouTube fetching
# ═══════════════════════════════════════════════════════════════

# ── Channel ID resolution (for RSS fallback) ─────────────────

def _resolve_channel_id(channel_url: str) -> str | None:
    """Try to get YouTube channel ID from a channel URL.
    Uses yt-dlp flat playlist first, falls back to page HTML scraping."""
    # Method 1: yt-dlp flat playlist (fast, no player responses needed)
    try:
        cmd = ["yt-dlp", "--flat-playlist", "--playlist-end", "1",
               "--print", "%(channel_id)s", "--ignore-errors", channel_url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        cid = result.stdout.strip().split("\n")[0].strip()
        if cid and cid != "NA" and cid.startswith("UC"):
            return cid
    except Exception:
        pass

    # Method 2: scrape channel page HTML for canonical URL
    try:
        import requests as reqs
        # Use @handle URL (not /videos) for the main channel page
        page_url = channel_url.replace("/videos", "") if channel_url.endswith("/videos") else channel_url
        resp = reqs.get(page_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; youtube-scan/1.0)"
        }, timeout=10)
        match = re.search(r'https://www\.youtube\.com/channel/(UC[\w-]+)', resp.text)
        if match:
            return match.group(1)
    except Exception:
        pass

    return None


def _fetch_via_rss(channel_id: str, days: int = 1) -> list[dict]:
    """Fetch recent videos via YouTube RSS feed (no yt-dlp needed).
    Reliable in GitHub Actions where yt-dlp is blocked by YouTube."""
    import requests as reqs
    videos = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    try:
        resp = reqs.get(rss_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; youtube-scan/1.0)"
        }, timeout=15)
        resp.raise_for_status()
        # YouTube RSS uses default namespace — strip it for easier parsing
        xml_text = re.sub(r'\sxmlns="[^"]+"', '', resp.text, count=1)
        root = ET.fromstring(xml_text)

        for entry in root.findall("entry"):
            title = (entry.findtext("title", "") or "").strip()
            link_el = entry.find("link")
            link = link_el.get("href", "") if link_el is not None else ""
            published = (entry.findtext("published", "") or "").strip()
            video_id = ""
            if "/watch?v=" in link:
                video_id = link.split("/watch?v=")[1].split("&")[0]

            pub_date = published[:10]
            if pub_date < cutoff:
                continue

            videos.append({
                "title": title,
                "url": link,
                "video_id": video_id,
                "upload_date": pub_date.replace("-", ""),
                "duration": "",
                "duration_sec": 0,
                "description": "",
                "channel": "",
                "channel_url": "",
                "view_count": None,
            })
    except Exception as e:
        print(f"    [!] RSS fetch error: {e}")

    return videos


def fetch_channel_videos(channel_url: str, days: int = 1) -> list[dict]:
    """Fetch videos with yt-dlp primary, RSS fallback for CI environments."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")

    # ── Primary: yt-dlp (full metadata: title + description + duration) ──
    cmd = [
        "yt-dlp",
        "--playlist-end", "10",
        "--dump-json",
        "--ignore-errors",
        "--extractor-args", "youtube:player_client=android",
        channel_url,
    ]
    ytdlp_videos = []
    ytdlp_warns = 0
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.stderr:
            warns = [l for l in result.stderr.split("\n") if l.strip() and "WARNING" in l]
            ytdlp_warns = len(warns)
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                d = json.loads(line)
                upload_date = d.get("upload_date", "")
                if upload_date and upload_date < cutoff:
                    continue
                ytdlp_videos.append({
                    "title": d.get("title", ""),
                    "url": d.get("webpage_url", ""),
                    "video_id": d.get("id", ""),
                    "upload_date": upload_date,
                    "duration": d.get("duration_string", ""),
                    "duration_sec": d.get("duration") or 0,
                    "description": d.get("description") or "",
                    "channel": d.get("uploader", ""),
                    "channel_url": d.get("uploader_url", ""),
                    "view_count": d.get("view_count"),
                })
            except json.JSONDecodeError:
                pass
    except subprocess.TimeoutExpired:
        print(f"    [!] yt-dlp timeout")
        ytdlp_warns = 999  # force RSS fallback
    except Exception as e:
        print(f"    [!] yt-dlp error: {e}")
        ytdlp_warns = 999

    # ── Fallback: RSS if yt-dlp had issues and got nothing useful ──
    if not ytdlp_videos and ytdlp_warns > 0:
        print(f"    [!] yt-dlp failed ({ytdlp_warns} warnings), switching to RSS")
        channel_id = _resolve_channel_id(channel_url)
        if channel_id:
            return _fetch_via_rss(channel_id, days)
        print(f"    [!] RSS fallback unavailable (no channel ID)")

    return ytdlp_videos


def fetch_all_channels(channels: list[tuple[str, str]]) -> dict[str, list[dict]]:
    """Fetch videos from all channels, return {channel_label: [videos]}."""
    results: dict[str, list[dict]] = {}
    for label, url in channels:
        print(f"  Fetching @{label} ... ", end="", flush=True)
        videos = fetch_channel_videos(url)
        print(f"{len(videos)} new video(s)")
        if videos:
            results[label] = videos
    return results


# ═══════════════════════════════════════════════════════════════
# Multi-Tier Content Extraction (TOKEN-FREE)
# ═══════════════════════════════════════════════════════════════

# Patterns for sponsor/promo boilerplate to strip from descriptions
_SPONSOR_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r'\b(?:Thanks|Thank you|Thanks to|Sponsored by|Sponsor|This video is sponsored|Paid promotion|AD|#ad|#sponsored)\b[^\n]*',
        r'\b(?:感谢|赞助|广告|推广)\b[^\n]*',
        r'https?://(?:geni\.us|bit\.ly|amzn\.to|shop\.|store\.|merch\.|patreon\.com|buymeacoffee\.com)[^\s]*',
        r'\b(?:Subscribe|Like|Comment|Share|Follow|Bell|Notification)\b[^\n]*',
        r'\b(?:订阅|点赞|评论|分享|关注|一键三连)\b[^\n]*',
        r'http://twitter\.com[^\s]*',
        r'http://instagram\.com[^\s]*',
        r'http://facebook\.com[^\s]*',
        r'^Playlist of[^\n]*',
        r'^~\s*$[\s\S]*',  # MKBHD-style separator + everything after
        r'Check out my[^\n]*',
        r'Get \d+% off[^\n]*',
        r'Save \d+%[^\n]*',
        r'Limited time[^\n]*',
        r"Let me know[^\n]*",
        r"What do you think[^\n]*",
    ]
]

# Structured content indicators in descriptions
_STRUCTURED_PATTERNS = [
    re.compile(r'\d+[\.\、\)）]'),   # Numbered list: "1. " or "1、"
    re.compile(r'^[-•\*]\s', re.M),  # Bullet points
    re.compile(r'[一二三四五六七八九十]+[\.\、]'),  # Chinese numbered
]

# Thresholds for adaptive summarization (in characters, after cleaning)
# Note: effective threshold is lower than user-facing "300 chars" because
# sponsor/promo stripping typically removes 20-35% of raw description text.
_RICH_DESC_THRESHOLD = 250   # >250 cleaned chars = rich, AI-summarize to ~300 chars
_MEDIUM_DESC_THRESHOLD = 50  # 50-250 chars = medium, check info density


def _detect_information_density(text: str) -> int:
    """Score the information density of a description (0-10+).
    Higher scores = more substantive financial/trading content.
    Used to decide whether a medium-length description is worth keeping."""
    if len(text) < 20:
        return 0
    signals = [
        # Stock codes: A-shares (6-digit), tickers
        (r'\b\d{5,6}\b', 2),                          # Numeric codes (likely A-shares)
        (r'\b[A-Z]{2,5}\b', 1, False),                # US tickers (uppercase only, no re.I)
        # Financial metrics (high weight)
        (r'\d+[\.\d]*%', 1),                          # Percentages
        (r'[¥$]\s*\d+', 1),                       # Currency amounts
        (r'\d+[\.\d]*(?:亿|万|k|K|w|W|B|M)\b', 1),   # Quantities with units
        # Directional views (high weight — key for investment content)
        (r'(?:看[多空涨跌]|持仓|加仓|减仓|清仓|建仓|止盈|止损|做[多空])', 2),
        (r'(?:bullish|bearish|long|short|hold|buy|sell|target)', 1),
        # Financial terms
        (r'(?:估值|业绩|营收|利润|增速|PE|PB|ROE|EPS|毛利|净利|现金流)', 1),
        (r'(?:板块|赛道|龙头|题材|概念|热点|主线|趋势|震荡|突破|回调)', 1),
        (r'(?:建议|推荐|策略|仓位|配置|组合|风险|收益)', 2),
        # Data points
        (r'(?:涨幅|跌幅|涨了|跌了|上涨|下跌|新高|新低|反弹)', 1),
        (r'(?:预期|预测|目标价|评级|上调|下调)', 1),
        # Time horizons
        (r'(?:短线|中线|长线|短期|中期|长期|本周|本月|本季)', 1),
    ]
    score = 0
    for entry in signals:
        if len(entry) == 3:
            pat, weight, _use_ignorecase = entry
            flags = 0
        else:
            pat, weight = entry
            flags = re.I
        if re.search(pat, text, flags):
            score += weight
    return score


def clean_description(desc: str) -> tuple[str, int, bool]:
    """Strip sponsor/promo boilerplate from description.
    Returns (cleaned_desc, effective_length, is_structured)."""
    cleaned = desc
    for pat in _SPONSOR_PATTERNS:
        cleaned = pat.sub('', cleaned)
    # Collapse whitespace
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = cleaned.strip()

    effective_len = len(cleaned)
    is_structured = any(pat.search(cleaned) for pat in _STRUCTURED_PATTERNS)
    return cleaned, effective_len, is_structured


def extract_title_summary(title: str) -> str | None:
    """Tier 0: Detect if title is self-summarizing.
    Chinese news-style titles often contain the full summary with separators.
    English titles like "Antigravity A1: A New Take on Drones!" are NOT
    self-summarizing — they're catchy hooks, not content summaries.
    Returns summary string or None."""
    # Quick check: title with specific financial data is always useful
    has_fin_data = bool(re.search(
        r'\d+[\.\d]*%|'
        r'(?:涨|跌)\d+[\.\d]*%|'
        r'\d{5,6}\b|'          # A-share codes
        r'[¥$]\d+',            # Currency amounts
        title
    ))
    # Chinese news-style: "Channel[Date]：key1；key2；key3" with dense info
    if len(title) < 22 and not has_fin_data:
        return None
    # Check for news-style format with substantial content after ：/:
    if "：" in title or ":" in title:
        parts = re.split(r'[：:]', title, maxsplit=1)
        if len(parts) == 2 and len(parts[1].strip()) >= 18:
            return title  # Entire title IS the summary
    # Semicolon-separated key points (explicit Chinese news format)
    if title.count("；") >= 2 or title.count(";") >= 2:
        return title
    # Long dense title with substantive punctuation
    if len(title) >= 50 and (title.count("，") >= 2 or title.count(",") >= 2):
        return title
    # Short but contains financial data → use directly
    if has_fin_data and len(title) >= 15:
        return title
    return None


def extract_description_summary(desc: str, effective_len: int, is_structured: bool) -> tuple[str | None, int, bool]:
    """Tier 1: Classify and extract from description.
    Returns (summary_or_none, info_density_score, is_rich_for_ai).

    Strategy:
      - > 300 chars: "rich" — pass to AI for ~300 char structured summary
        (don't naive-truncate; AI preserves key data better)
      - 50–300 chars: check information density
        - High density (>=3): use directly (info-rich enough as-is)
        - Low density (<3): might need AI if there's some substance
      - < 50 chars: too thin, skip to title/AI tier
    """
    density = _detect_information_density(desc)

    if effective_len < _MEDIUM_DESC_THRESHOLD:
        # Short but highly dense? Use directly instead of wasting AI tokens
        if density >= 5:
            return desc.strip(), density, False
        return None, density, False  # Too short

    if effective_len > _RICH_DESC_THRESHOLD:
        # Rich description — AI should summarize to ~300 chars
        # Don't truncate here; pass full text to AI for proper summarization
        return None, density, True  # is_rich_for_ai=True

    # Medium-length (50-300 chars): check information density
    if density >= 3:
        # High info density — use directly, no AI needed
        if is_structured:
            lines = [l.strip() for l in desc.strip().split('\n') if l.strip()]
            return '\n'.join(lines), density, False
        return desc.strip(), density, False

    # Medium length but low density — check if worth AI summarization
    if density >= 1 and effective_len >= 100:
        # Some substance, let AI clean it up
        return None, density, True

    # Too vague — skip to title tier
    return None, density, False


def extract_content(video: dict) -> dict:
    """Multi-tier content extraction for a single video.
    Returns enriched video dict with:
      - extracted_summary: best free summary (title-based or description-based)
      - summary_source: 'title' | 'description' | None
      - needs_ai: bool — True if Tier 0+1 insufficient, AI should summarize
      - ai_input: compact text for AI summarization (only if needs_ai)
      - info_density: int — information density score (for adaptive AI prompting)
      - desc_is_rich: bool — True if description > 300 chars (for adaptive AI prompt)
    """
    title = video.get("title", "")
    desc = video.get("description", "")
    duration_sec = video.get("duration_sec", 0)

    cleaned_desc, eff_len, is_structured = clean_description(desc)

    result = {**video, "cleaned_description": cleaned_desc,
              "info_density": 0, "desc_is_rich": False}

    # ── Tier 1: Description-based extraction ──
    desc_result, density, is_rich = extract_description_summary(
        cleaned_desc, eff_len, is_structured
    )
    result["info_density"] = density
    result["desc_is_rich"] = is_rich

    if desc_result is not None:
        # Medium-length, high-density description — use directly
        result["extracted_summary"] = desc_result
        result["summary_source"] = "description"
        result["needs_ai"] = False
        result["ai_input"] = ""
        return result

    if is_rich:
        # Rich description (> 300 chars) — needs AI for ~300 char structured summary
        # Provide full description so AI has all context
        ai_input = (
            f"标题：{title}\n"
            f"时长：{video.get('duration', '?')}\n"
            f"简介（{eff_len}字，信息密度{density}分，内容详尽，请保留关键数据）：\n"
            f"{cleaned_desc}\n"
        )
        result["extracted_summary"] = ""
        result["summary_source"] = None
        result["needs_ai"] = True
        result["ai_input"] = ai_input[:2000]  # Allow up to 2000 chars for rich descriptions
        result["effective_desc_len"] = eff_len  # Pass through for adaptive token budget
        return result

    # ── Tier 0: Title self-summarizing? ──
    title_summary = extract_title_summary(title)
    if title_summary:
        result["extracted_summary"] = title_summary
        result["summary_source"] = "title"
        result["needs_ai"] = False
        result["ai_input"] = ""
        return result

    # ── Tier 2: Need AI help ──
    ai_input = f"标题：{title}\n时长：{video.get('duration', '?')}\n"
    if cleaned_desc:
        ai_input += f"简介（{eff_len}字，信息密度{density}分）：\n{cleaned_desc}\n"
    else:
        ai_input += "（无简介，仅根据标题判断内容）\n"
    result["extracted_summary"] = ""
    result["summary_source"] = None
    result["needs_ai"] = True
    result["ai_input"] = ai_input[:1500]
    return result


# ═══════════════════════════════════════════════════════════════
# AI: Individual video summary (Tier 2 — only when needed)
# ═══════════════════════════════════════════════════════════════

VIDEO_SUMMARY_PROMPT = """You summarize financial/trading YouTube videos in Chinese.
Your goal: preserve enough detail to be useful, but stay concise enough to scan quickly.

## Length rules (ADAPTIVE)
- If input has RICH description (>300 chars with stock picks, positions, forecasts):
  → Summarize to **200-350 Chinese characters**. Preserve: tickers/stock codes, price ranges,
    position recommendations (增持/减持/持有), bullish/bearish views, short/medium/long-term
    outlooks, key support/resistance levels, specific % targets.
- If input has MEDIUM description (100-300 chars with some data):
  → Summarize to **120-200 Chinese characters**. Extract the core thesis with key numbers.
- If input has THIN description (title only or vague text):
  → Summarize to **60-120 Chinese characters**. Extract the single most important takeaway.

## What to KEEP (high priority)
1. Specific stock codes, tickers, sectors mentioned
2. Position changes: 增持/减持/建仓/减仓/清仓
3. Time-horizon views: 短线/中线/长线, short-term/mid-term/long-term
4. Price targets or ranges, key levels
5. Concrete data: % changes, valuations, volume signals
6. Risk warnings or catalyst events

## What to DROP (low priority)
1. Sponsor/promo text, "订阅点赞" boilerplate
2. Generic market commentary without specifics
3. Repetitive phrases, filler words
4. Personal anecdotes not related to investment decisions

## Output format
Just the summary text. No prefix like "这个视频讲了..." or "总结：".
If input contains NO substantive financial information (just promo/ads/vague talk), say: "内容以推广为主，无实质投资观点"
"""


def ai_summarize_video(ai_input: str, client, model: str,
                       desc_is_rich: bool = False,
                       info_density: int = 0,
                       effective_desc_len: int = 0) -> str:
    """Call AI to summarize a single video with adaptive output length.
    - Truly rich (>300 chars) → max_tokens=1000, ~300 char summary
    - Medium-with-substance (100-300 chars) → max_tokens=500, ~150 char summary
    - Thin (<100 chars or density 0) → max_tokens=300, ~80 char summary
    """
    # Adaptive token budget: prioritize truly rich over medium-with-substance
    if desc_is_rich and effective_desc_len > _RICH_DESC_THRESHOLD:
        max_tokens = 1000
    elif desc_is_rich:  # Medium with substance (100-300 chars, density 1-2)
        max_tokens = 500
    elif info_density >= 3:
        max_tokens = 600
    elif info_density >= 1:
        max_tokens = 400
    else:
        max_tokens = 300

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0.3,
            system=VIDEO_SUMMARY_PROMPT,
            messages=[{"role": "user", "content": ai_input}],
        )
        text_parts = []
        for block in response.content:
            if hasattr(block, 'text') and block.text:
                text_parts.append(block.text)
        if not text_parts:
            btypes = [getattr(b, 'type', type(b).__name__) for b in response.content]
            print(f"    [WARN] No text in response blocks: {btypes}")
            return "(AI 摘要失败)"
        summary = "".join(text_parts).strip()
        # Soft truncation — only if wildly over target
        if desc_is_rich and len(summary) > 500:
            # Find last sentence boundary within 500 chars
            trunc_point = summary[:500].rfind("。")
            if trunc_point > 200:
                summary = summary[:trunc_point + 1]
        elif not desc_is_rich and len(summary) > 300:
            trunc_point = summary[:300].rfind("。")
            if trunc_point > 80:
                summary = summary[:trunc_point + 1]
        return summary
    except Exception as e:
        print(f"    [WARN] AI video summary failed: {e}")
        return "(AI 摘要失败)"


# ═══════════════════════════════════════════════════════════════
# AI: Daily report synthesis (cross-channel formatting)
# ═══════════════════════════════════════════════════════════════

DAILY_REPORT_PROMPT = """You format a daily YouTube subscription digest in Chinese.

## Input
You'll receive a list of videos with pre-extracted summaries (from titles, descriptions, or AI).
Your job is to FORMAT and PRIORITIZE, not to re-summarize.

## Output Format

### 🔥 今日必看
1-2 most important videos. For each: title (as markdown link), channel, duration, and why it matters.

### 📺 其他更新
Remaining videos in a compact list:
- **[title](url)**（channel | duration）— use the extracted summary directly.
  Keep summaries as provided; only trim if they exceed 350 characters.
  Preserve: stock codes, position recommendations, price targets, % data.

### 📊 今日概览
- X channels, Y videos total
- Trending themes (if any): e.g. "多数频道关注AI/半导体", "A股情绪偏谨慎"
- Most active channel today

## Rules
1. **Preserve the extracted summaries** — don't re-compress them; they're already tailored to input richness
2. Format URLs as markdown links: `[title](url)`
3. Opinionated prioritization: genuine insights > news roundups > sponsored/fluff
4. If a video's summary is its own title, format it cleanly without repeating
5. Add at end: "以上摘要由 AI 自动整理，视频核心观点来自标题/简介提取，以实际观看为准。"
6. Total output: max 3000 chars
"""


def synthesize_daily_report(videos_by_channel: dict[str, list[dict]],
                            api_key: str,
                            base_url: str = "https://api.anthropic.com",
                            model: str = "claude-sonnet-4-6") -> str:
    """Two-phase AI pipeline:
    Phase 1: Summarize individual videos that need AI (Tier 2 only)
    Phase 2: Format daily report from all summaries (cheap — one call)
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    total_videos = sum(len(v) for v in videos_by_channel.values())

    # Phase 1: AI summary only for videos that need it (Tier 2)
    ai_calls = 0
    for channel, videos in videos_by_channel.items():
        for v in videos:
            if v.get("needs_ai"):
                ai_calls += 1
                print(f"    AI summarizing: {v['title'][:50]}... "
                      f"(rich={v.get('desc_is_rich', False)}, density={v.get('info_density', 0)})")
                v["extracted_summary"] = ai_summarize_video(
                    v["ai_input"], client, model,
                    desc_is_rich=v.get("desc_is_rich", False),
                    info_density=v.get("info_density", 0),
                    effective_desc_len=v.get("effective_desc_len", 0),
                )
                v["summary_source"] = "ai"

    # Phase 2: Format daily report
    lines = []
    for channel, videos in videos_by_channel.items():
        for v in videos:
            lines.append(f"CHANNEL: {channel}")
            lines.append(f"TITLE: {v['title']}")
            lines.append(f"URL: {v['url']}")
            lines.append(f"DURATION: {v['duration']}")
            lines.append(f"SUMMARY: {v.get('extracted_summary', 'N/A')}")
            lines.append(f"SOURCE: {v.get('summary_source', 'unknown')}")
            lines.append("---")

    video_text = "\n".join(lines)
    total_channels = len(videos_by_channel)

    prompt = f"""Today: {today_str}
{total_channels} channels, {total_videos} videos. AI individual summaries: {ai_calls}.

{video_text}

Produce the daily report now."""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=DAILY_REPORT_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text_parts = []
        for block in response.content:
            if hasattr(block, 'text') and block.text:
                text_parts.append(block.text)
        if not text_parts:
            btypes = [getattr(b, 'type', type(b).__name__) for b in response.content]
            print(f"    [WARN] No text in daily report response blocks: {btypes}")
            return format_lightweight_report(videos_by_channel)
        report = "\n".join(text_parts)

        # Append token stats
        tier0 = sum(1 for vv in videos_by_channel.values() for v in vv if v.get("summary_source") == "title")
        tier1 = sum(1 for vv in videos_by_channel.values() for v in vv if v.get("summary_source") == "description")
        tier2 = ai_calls
        tier2_rich = sum(1 for vv in videos_by_channel.values() for v in vv
                         if v.get("summary_source") == "ai" and v.get("desc_is_rich"))
        tier2_thin = tier2 - tier2_rich
        report += f"\n\n> 📊 摘要来源：标题自包含 {tier0} | 简介直接使用 {tier1}"
        if tier2 > 0:
            report += f" | AI 辅助 {tier2}（其中详情摘要 {tier2_rich}，简明摘要 {tier2_thin}）"
        return report
    except Exception as e:
        print(f"[ERROR] Report synthesis failed: {e}")
        # Fallback to lightweight mode
        return format_lightweight_report(videos_by_channel)


# ═══════════════════════════════════════════════════════════════
# Lightweight fallback (no AI)
# ═══════════════════════════════════════════════════════════════

def format_lightweight_report(videos_by_channel: dict[str, list[dict]]) -> str:
    """Template-based report without AI."""
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    total_videos = sum(len(v) for v in videos_by_channel.values())

    report = f"## 📺 YouTube 订阅更新\n\n> {today_str} | {total_videos} 条更新 | 轻量模式\n\n"
    for channel, videos in videos_by_channel.items():
        report += f"### {channel}\n\n"
        for v in videos:
            upload = v.get("upload_date", "")
            if len(upload) == 8:
                upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
            report += f"- **[{v['title']}]({v['url']})** ({v['duration']})\n"
            summary = v.get("extracted_summary", "")
            if summary:
                report += f"  > {summary[:400]}\n"
            report += "\n"
    report += "\n> ⚠️ 轻量模式：配置 ANTHROPIC_API_KEY 可开启 AI 智能摘要。\n"
    return report


# ═══════════════════════════════════════════════════════════════
# Email
# ═══════════════════════════════════════════════════════════════

def create_github_issue(repo: str, title: str, body: str, token: str) -> str | None:
    """Create a GitHub Issue with the YouTube report."""
    try:
        from github import Auth, Github
        g = Github(auth=Auth.Token(token))
        repo_obj = g.get_repo(repo)
        issue = repo_obj.create_issue(
            title=title,
            body=body,
            labels=["daily-scan", "youtube"],
        )
        return issue.html_url
    except Exception as e:
        print(f"[ERROR] Failed to create GitHub issue: {e}")
        return None


def save_report(report: str, output_dir: Path) -> Path:
    """Save report to output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = output_dir / f"youtube-report-{today}.md"
    filepath.write_text(report, encoding="utf-8")
    latest = output_dir / "youtube-report.md"
    latest.write_text(report, encoding="utf-8")
    print(f"[OK] Report saved to {filepath}")
    return filepath


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("YouTube Channel Update Scanner (v2 — token-efficient)")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    channels_raw = os.environ.get("YOUTUBE_CHANNELS", "")
    if not channels_raw:
        print("[SKIP] No YOUTUBE_CHANNELS configured.")
        print("  Set GitHub Secret: YOUTUBE_CHANNELS=@channel1,@channel2,...")
        # Still create report file so daily_scan merge doesn't silently miss it
        report = "## 📺 YouTube 订阅更新\n\n> ⚠️ 未配置频道列表。在 GitHub Secrets 中添加 `YOUTUBE_CHANNELS` 即可开启。\n"
        save_report(report, Path("output"))
        return

    channels = parse_channels(channels_raw)
    print(f"Channels: {len(channels)}")
    for label, url in channels:
        print(f"  - @{label}")

    # ── Step 1: Fetch ──
    print("\n── Step 1: Fetching recent videos ──")
    videos_by_channel = fetch_all_channels(channels)
    total = sum(len(v) for v in videos_by_channel.values())
    print(f"Total new videos: {total}")
    if total == 0:
        print("[INFO] No new videos today.")
        report = f"""## 📺 YouTube 订阅更新

> {len(channels)} 个频道今日无更新。
"""
        output_dir = Path("output")
        save_report(report, output_dir)
        print("[OK] Empty report saved.")
        return

    # ── Step 2: Multi-tier content extraction (token-free) ──
    print("\n── Step 2: Extracting content (Tier 0–1, adaptive) ──")
    ai_needed = 0
    extracted = {"title": 0, "description": 0, "description_rich": 0}
    for channel, videos in videos_by_channel.items():
        for i, v in enumerate(videos):
            enriched = extract_content(v)
            videos[i] = enriched
            src = enriched.get("summary_source")
            if src == "title":
                extracted["title"] += 1
                print(f"  ✓ [{channel}] title self-summarizing ({len(v['title'])} chars)")
            elif src == "description":
                extracted["description"] += 1
                density = enriched.get("info_density", 0)
                print(f"  ✓ [{channel}] description direct-use (density={density}, {len(enriched['extracted_summary'])} chars)")
            else:
                ai_needed += 1
                is_rich = enriched.get("desc_is_rich", False)
                density = enriched.get("info_density", 0)
                if is_rich:
                    extracted["description_rich"] += 1
                    print(f"  ⟳ [{channel}] RICH desc→AI ({len(enriched.get('cleaned_description', ''))} chars, density={density}): {v['title'][:50]}...")
                else:
                    print(f"  ⟳ [{channel}] needs AI (density={density}): {v['title'][:50]}...")

    print(f"  Tier 0 (title): {extracted['title']} | "
          f"Tier 1 (desc-direct): {extracted['description']} | "
          f"Tier 1 (desc-rich→AI): {extracted['description_rich']} | "
          f"Tier 2 (AI-thin): {ai_needed - extracted['description_rich']}")

    # ── Step 3: Synthesize ──
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    use_ai = bool(anthropic_key)

    today_cn = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    title = f"YouTube 订阅更新摘要 — {today_cn}"

    if use_ai:
        print("\n── Step 3: AI daily report synthesis ──")
        print(f"  AI individual summaries needed: {ai_needed}/{total}")
        synthesis = synthesize_daily_report(
            videos_by_channel, anthropic_key,
            base_url=anthropic_base_url, model=anthropic_model,
        )
        report = f"""# 📺 {title}

> 🤖 自动生成 | {today_cn} 北京时间 | {len(channels)} 个频道 {total} 条更新

{synthesis}
"""
    else:
        print("\n── Step 3: Lightweight report ──")
        inner = format_lightweight_report(videos_by_channel)
        report = f"# 📺 {title}\n\n{inner}"

    # ── Step 4: Output (report merged by daily_scan.py into one Issue) ──
    output_dir = Path("output")
    report_path = save_report(report, output_dir)

    print("\n" + "=" * 60)
    print("SCAN COMPLETE")
    print(f"  Channels: {len(channels)} | Videos: {total}")
    rich = extracted.get("description_rich", 0)
    thin_ai = ai_needed - rich
    print(f"  Summary sources: title={extracted['title']} desc-direct={extracted['description']} desc-rich→AI={rich} thin→AI={thin_ai}")
    if total > 0:
        pct_free = (extracted['title'] + extracted['description']) / total * 100
        print(f"  Token-free coverage: {pct_free:.0f}% ({extracted['title'] + extracted['description']}/{total})")
        if ai_needed > 0:
            rich_pct = rich / ai_needed * 100 if ai_needed > 0 else 0
            print(f"  AI-assisted videos: {ai_needed} ({rich} rich/structured, {thin_ai} thin/simple)")
    print(f"  Report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
