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
    re.compile(r, re.I | re.DOTALL)
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
    # Chinese news-style: "Channel[Date]：key1；key2；key3" with dense info
    # Must be long enough AND have substantive content after separator
    if len(title) < 40:
        return None
    # Check for news-style format with substantial content after ：/:
    if "：" in title or ":" in title:
        parts = re.split(r'[：:]', title, maxsplit=1)
        if len(parts) == 2 and len(parts[1].strip()) >= 30:
            return title  # Entire title IS the summary
    # Semicolon-separated key points (explicit Chinese news format)
    if title.count("；") >= 2 or title.count(";") >= 2:
        return title
    # Long dense title with substantive punctuation
    if len(title) >= 65 and (title.count("，") >= 2 or title.count(",") >= 2):
        return title
    return None


def extract_description_summary(desc: str, effective_len: int, is_structured: bool) -> str | None:
    """Tier 1: Extract key points from a rich description.
    Returns structured summary or None if description is too thin."""
    if effective_len < 50:
        return None  # Too short to be useful
    if effective_len < 200 and not is_structured:
        return None  # Short + unstructured = likely noise

    if is_structured:
        # Return first ~400 chars of structured content
        lines = desc.strip().split('\n')
        key_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if any(pat.search(line) for pat in _STRUCTURED_PATTERNS):
                key_lines.append(line)
            elif len(line) > 20:
                key_lines.append(line)
            if len('\n'.join(key_lines)) > 400:
                break
        return '\n'.join(key_lines)
    else:
        # Unstructured but long: return first 300 chars
        return desc[:300]

    return None


def extract_content(video: dict) -> dict:
    """Multi-tier content extraction for a single video.
    Returns enriched video dict with:
      - extracted_summary: best free summary (title-based or description-based)
      - summary_source: 'title' | 'description' | None
      - needs_ai: bool — True if Tier 0+1 insufficient, AI should summarize
      - ai_input: compact text for AI summarization (only if needs_ai)
    """
    title = video.get("title", "")
    desc = video.get("description", "")
    duration_sec = video.get("duration_sec", 0)

    cleaned_desc, eff_len, is_structured = clean_description(desc)

    result = {**video, "cleaned_description": cleaned_desc}

    # Priority: description first (more detailed), title as fallback
    # "有些博主会把核心观点全部写在简介里" — prefer description when rich

    # Tier 1: Rich description? (check FIRST — more detail than even best title)
    desc_summary = extract_description_summary(cleaned_desc, eff_len, is_structured)
    if desc_summary:
        result["extracted_summary"] = desc_summary
        result["summary_source"] = "description"
        result["needs_ai"] = False
        result["ai_input"] = ""
        return result

    # Tier 0: Title self-summarizing? (fallback when description is thin)
    title_summary = extract_title_summary(title)
    if title_summary:
        result["extracted_summary"] = title_summary
        result["summary_source"] = "title"
        result["needs_ai"] = False
        result["ai_input"] = ""
        return result

    # Tier 2: Need AI help (thin description, uninformative title)
    # Compact input: title + whatever description we have (max 500 chars)
    ai_input = f"标题：{title}\n时长：{video.get('duration', '?')}\n"
    if cleaned_desc:
        ai_input += f"简介：{cleaned_desc[:500]}\n"
    result["extracted_summary"] = ""
    result["summary_source"] = None
    result["needs_ai"] = True
    result["ai_input"] = ai_input[:800]  # Hard cap on input
    return result


# ═══════════════════════════════════════════════════════════════
# AI: Individual video summary (Tier 2 — only when needed)
# ═══════════════════════════════════════════════════════════════

VIDEO_SUMMARY_PROMPT = """You summarize YouTube videos in 80-150 Chinese characters.
Rules:
1. Extract the single most important insight or argument
2. Include specific data/numbers if present
3. No fluff, no "这个视频讲了..."
4. If the input is mostly sponsor/promo text, say "内容以推广为主，无实质观点"
Output format: just the summary text, nothing else."""


def ai_summarize_video(ai_input: str, client, model: str) -> str:
    """Call AI to summarize a single video (compact)."""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=300,
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
        return "".join(text_parts).strip()[:200]
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
- **[title](url)**（channel | duration）— use the extracted summary directly (80-150 chars each)

### 📊 今日概览
- X channels, Y videos total
- Trending themes (if any)
- Most active channel today

## Rules
1. **Keep the extracted summaries** — don't re-summarize, just trim if too long
2. If a video's summary is its own title, that's fine — format it cleanly
3. Opinionated prioritization: genuine insights > news roundups > sponsored/fluff
4. Add at end: "以上摘要由 AI 自动整理，视频核心观点来自标题/简介提取，以实际观看为准。"
5. No more than 1500 chars total output
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
                print(f"    AI summarizing: {v['title'][:50]}...")
                v["extracted_summary"] = ai_summarize_video(
                    v["ai_input"], client, model
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
            max_tokens=2500,
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
        report += f"\n\n> 📊 摘要来源：标题自包含 {tier0} | 简介提取 {tier1} | AI 辅助 {tier2}"
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
                report += f"  > {summary[:200]}\n"
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
    print("\n── Step 2: Extracting content (Tier 0–1, 0 tokens) ──")
    ai_needed = 0
    extracted = {"title": 0, "description": 0}
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
                print(f"  ✓ [{channel}] description extracted ({len(enriched['extracted_summary'])} chars)")
            else:
                ai_needed += 1
                print(f"  ⟳ [{channel}] needs AI: {v['title'][:50]}...")

    print(f"  Tier 0 (title): {extracted['title']} | Tier 1 (desc): {extracted['description']} | Tier 2 (AI): {ai_needed}")

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
    print(f"  Summary sources: title={extracted['title']} desc={extracted['description']} ai={ai_needed}")
    print(f"  Report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
