#!/usr/bin/env python3
"""
13F Institutional Holdings Tracker
===================================
Tracks quarterly 13F filings from 12 major institutions via SEC EDGAR API (free).
Detects QoQ portfolio changes and generates a daily tracking report.

Data source: SEC EDGAR submissions API + primary document XML parsing.
No API key required — SEC EDGAR is fully public.

Daily behavior:
  - Normal day: latest snapshot + filing countdown
  - Filing day (new 13F detected): detailed change analysis

Integration: saves report to output/holdings-report.md, merged by daily_scan.py
"""

import os
import re
import sys
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Any

import requests

# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════

# SEC requires a descriptive User-Agent with org name and email
SEC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; daily-china-tech-scan/1.0; research@example.com)",
    "Accept": "application/json",
}

# Major institutions tracked (CIK must be 10-digit zero-padded string)
INSTITUTIONS: list[dict[str, str]] = [
    {"name": "Berkshire Hathaway",     "cik": "0001067983", "slug": "berkshire"},
    {"name": "Bridgewater Associates",  "cik": "0001350694", "slug": "bridgewater"},
    {"name": "Renaissance Technologies","cik": "0001037389", "slug": "renaissance"},
    {"name": "Goldman Sachs Group",     "cik": "0000886982", "slug": "goldman"},
    {"name": "Morgan Stanley",          "cik": "0000895421", "slug": "morgan-stanley"},
    {"name": "UBS Group AG",            "cik": "0001610520", "slug": "ubs"},
    {"name": "BlackRock Inc",           "cik": "0001364742", "slug": "blackrock"},
    {"name": "Point72 Asset Management","cik": "0001603466", "slug": "point72"},
    {"name": "Citadel Advisors",        "cik": "0001423053", "slug": "citadel"},
    {"name": "D.E. Shaw & Co",          "cik": "0001009207", "slug": "deshaw"},
    {"name": "Baupost Group",           "cik": "0001061768", "slug": "baupost"},
    {"name": "Appaloosa LP",            "cik": "0001656456", "slug": "appaloosa"},
]

# ── A-Share (A股) institutional data ──────────────────────────

# For each tracked institution: their known A-share / China market presence.
# This is manually researched data — updated as new information becomes available.
# Fields:
#   - a_share_access: how they access A-shares
#   - known_a_holdings: publicly known A-share positions (from company filings)
#   - notes: additional context
#   - data_confidence: 'confirmed' | 'likely' | 'unknown'
INSTITUTION_A_SHARE_INFO: dict[str, dict] = {
    "berkshire": {
        "a_share_access": "None (主要通过港股持有比亚迪 01211.HK，无A股直接持仓)",
        "known_a_holdings": [],
        "notes": "巴菲特明确表示不直接投A股。BYD仓位通过港股持有，自2022年8月起持续减持，至2024年持股比例已大幅下降。",
        "data_confidence": "confirmed",
    },
    "bridgewater": {
        "a_share_access": "QFII + 债券通 (2018年获QFII资格，积极配置A股)",
        "known_a_holdings": [
            "公开信息：桥水中国基金通过QFII+RQFII配置A股，偏好消费、医药、科技龙头",
            "2024年桥水中国基金规模约300亿元人民币，主要投资于A股和债券",
        ],
        "notes": "桥水全天候策略在中国落地，A股配置偏大盘蓝筹。具体个股持仓需查上市公司季报前十大股东。",
        "data_confidence": "confirmed",
    },
    "renaissance": {
        "a_share_access": "Limited (主要通过量化模型，可能通过swap/衍生产品间接参与)",
        "known_a_holdings": [],
        "notes": "文艺复兴以量化策略为主，A股参与度较低，13F中几乎不露A股标的。",
        "data_confidence": "likely",
    },
    "goldman": {
        "a_share_access": "QFII + 合资券商(高盛高华) + 北向",
        "known_a_holdings": [
            "高盛通过QFII和北向通道积极参与A股",
            "2024年高盛多次发布A股看多报告，重点推荐消费和新能源板块",
        ],
        "notes": "高盛是中国市场最活跃的外资投行之一，具体持仓分散在多个实体。",
        "data_confidence": "confirmed",
    },
    "morgan-stanley": {
        "a_share_access": "QFII + 合资券商(摩根士丹利证券) + 北向",
        "known_a_holdings": [],
        "notes": "摩根士丹利A股研究覆盖广泛，通过北向通道活跃交易。",
        "data_confidence": "confirmed",
    },
    "ubs": {
        "a_share_access": "QFII + 北向 (持有最大QFII额度之一)",
        "known_a_holdings": [
            "UBS AG在A股多家公司季报中出现在前十大股东",
            "可通过上市公司季报查询UBS AG持仓",
        ],
        "notes": "UBS是QFII额度最大的外资机构之一，在A股市场非常活跃。",
        "data_confidence": "confirmed",
    },
    "blackrock": {
        "a_share_access": "QFII + 外资全资公募(贝莱德基金) + 北向",
        "known_a_holdings": [
            "贝莱德基金2021年获准在华设立全资公募基金公司",
            "已发行多只A股基金产品，可通过基金季报查询持仓",
        ],
        "notes": "贝莱德是全球最大资管公司，积极布局中国A股市场。",
        "data_confidence": "confirmed",
    },
    "point72": {
        "a_share_access": "Limited (主要通过QFII/北向进行相对低调的配置)",
        "known_a_holdings": [],
        "notes": "Point72以多策略对冲基金为主，在A股市场相对低调。",
        "data_confidence": "likely",
    },
    "citadel": {
        "a_share_access": "QFII (2020年获QFII资格)",
        "known_a_holdings": [],
        "notes": "Citadel于2020年获得QFII资格，A股参与度逐步提升。",
        "data_confidence": "confirmed",
    },
    "deshaw": {
        "a_share_access": "Limited (量化策略为主，主要通过衍生产品/北向)",
        "known_a_holdings": [],
        "notes": "D.E. Shaw以量化策略为主，A股参与方式较不透明。",
        "data_confidence": "likely",
    },
    "baupost": {
        "a_share_access": "None (以美国困境资产/价值投资为主，未见明确A股参与)",
        "known_a_holdings": [],
        "notes": "Baupost以深度价值投资为主，主要投资美国市场。",
        "data_confidence": "likely",
    },
    "appaloosa": {
        "a_share_access": "Limited (David Tepper曾表示看好中国，但主要通过港股/中概股参与)",
        "known_a_holdings": [],
        "notes": "Appaloosa关注中国市场，但主要集中在港股和美股中概股。",
        "data_confidence": "likely",
    },
}

# 13F filing schedule
Q_SCHEDULE = {
    1: {"quarter_end": "03-31", "deadline": "05-15", "label": "Q1"},
    2: {"quarter_end": "06-30", "deadline": "08-14", "label": "Q2"},
    3: {"quarter_end": "09-30", "deadline": "11-14", "label": "Q3"},
    4: {"quarter_end": "12-31", "deadline": "02-14", "label": "Q4"},
}

# Top holdings to display per institution
TOP_N = 10

# Request delay to respect SEC rate limit (10 req/sec)
REQUEST_DELAY = 0.25


# ═══════════════════════════════════════════════════════════════
# SEC EDGAR API helpers
# ═══════════════════════════════════════════════════════════════

def sec_get(url: str, timeout: int = 20) -> requests.Response | None:
    """GET request with SEC-required headers and rate-limit delay."""
    time.sleep(REQUEST_DELAY)
    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp
        elif resp.status_code == 404:
            print(f"    [404] Not found: {url[:100]}...")
            return None
        else:
            print(f"    [HTTP {resp.status_code}] {url[:100]}...")
            return None
    except requests.RequestException as e:
        print(f"    [ERR] Request failed: {e}")
        return None


def get_latest_13f_filing(cik: str) -> dict[str, Any] | None:
    """Query SEC submissions API for the latest 13F-HR filing of a CIK.

    Returns dict with: quarter_end, filing_date, accession_number, doc_url
    or None if not found / error.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = sec_get(url)
    if resp is None:
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"    [ERR] Invalid JSON from submissions API for {cik}")
        return None

    filings = data.get("filings", {}).get("recent", {})
    if not filings:
        return None

    forms = filings.get("form", [])
    report_dates = filings.get("reportDate", [])
    filing_dates = filings.get("filingDate", [])
    accessions = filings.get("accessionNumber", [])
    primary_docs = filings.get("primaryDocument", [])

    # Find the latest 13F-HR
    for i in range(len(forms)):
        if forms[i] == "13F-HR":
            acc_num = accessions[i] if i < len(accessions) else ""
            if not acc_num:
                continue

            # Build document URL — use the full submission text file (.txt)
            # which contains the SGML-wrapped XML, not the XSL-rendered HTML
            cik_no_zeros = str(int(cik))  # strip leading zeros
            acc_no_dashes = acc_num.replace("-", "")

            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_no_zeros}/{acc_no_dashes}/{acc_num}.txt"
            )

            return {
                "quarter_end": report_dates[i] if i < len(report_dates) else "",
                "filing_date": filing_dates[i] if i < len(filing_dates) else "",
                "accession_number": acc_num,
                "doc_url": doc_url,
            }

    return None


def download_and_parse_13f(doc_url: str) -> list[dict[str, Any]]:
    """Download a 13F primary document and parse holdings from the informationTable.

    SEC 13F filings contain multiple <XML> blocks. The first has cover page data,
    the second (or later) has the <informationTable> with the actual holdings.
    """
    resp = sec_get(doc_url, timeout=30)
    if resp is None:
        return []

    raw_text = resp.text

    # Find ALL <XML>...</XML> blocks (there are typically 2: cover + informationTable)
    xml_blocks = re.findall(r"<XML>\s*(.*?)\s*</XML>", raw_text, re.DOTALL | re.IGNORECASE)

    # Try each XML block — look for one containing infoTable elements
    for xml_text in xml_blocks:
        try:
            root = ET.fromstring(xml_text)
            holdings = _extract_holdings_from_tree(root)
            if holdings:
                return holdings
        except ET.ParseError:
            continue

    # Fallback: try parsing individual <infoTable> elements from raw text
    tables = re.findall(
        r"<infoTable>.*?</infoTable>", raw_text, re.DOTALL | re.IGNORECASE,
    )
    if tables:
        holdings = []
        for table_xml in tables:
            try:
                entry = ET.fromstring(table_xml)
                h = _parse_info_table(entry)
                if h:
                    holdings.append(h)
            except ET.ParseError:
                continue
        if holdings:
            return holdings

    return []


# Known SEC 13F XML namespaces (tried in order)
_NS_13F = [
    "http://www.sec.gov/edgar/thirteenffiler",
    "http://www.sec.gov/edgar/document/thirteenf/informationtable",
]


def _findall_ns(root: ET.Element, tag: str) -> list[ET.Element]:
    """Find all elements matching tag across known 13F namespaces + no-ns fallback."""
    results = root.findall(f".//{tag}")  # no namespace
    if results:
        return results
    for ns in _NS_13F:
        results = root.findall(f".//{{{ns}}}{tag}")
        if results:
            return results
    return []


def _find_ns(parent: ET.Element, tag: str) -> ET.Element | None:
    """Find first child element matching tag across known 13F namespaces + no-ns fallback."""
    el = parent.find(tag)  # no namespace
    if el is not None:
        return el
    for ns in _NS_13F:
        el = parent.find(f"{{{ns}}}{tag}")
        if el is not None:
            return el
    return None


def _extract_holdings_from_tree(root: ET.Element) -> list[dict[str, Any]]:
    """Extract holdings from a parsed XML tree."""
    holdings = []

    # Find all infoTable elements
    info_tables = _findall_ns(root, "infoTable")

    for entry in info_tables:
        h = _parse_info_table(entry)
        if h:
            holdings.append(h)

    return holdings


def _parse_info_table(entry: ET.Element) -> dict[str, Any] | None:
    """Parse a single <infoTable> element into a holding dict."""

    def _text(tag: str) -> str:
        el = _find_ns(entry, tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    issuer = _text("nameOfIssuer")
    title = _text("titleOfClass")
    cusip = _text("cusip")
    value_str = _text("value")

    if not issuer or not value_str:
        return None

    try:
        value_k = int(value_str)
    except (ValueError, TypeError):
        return None

    # Shares
    shrs_el = _find_ns(entry, "shrsOrPrnAmt")
    shares = 0
    share_type = ""
    if shrs_el is not None:
        amt_el = _find_ns(shrs_el, "sshPrnamt")
        type_el = _find_ns(shrs_el, "sshPrnamtType")
        if amt_el is not None and amt_el.text:
            try:
                shares = int(float(amt_el.text.strip()))
            except (ValueError, TypeError):
                shares = 0
        if type_el is not None and type_el.text:
            share_type = type_el.text.strip()

    discretion = _text("investmentDiscretion")

    return {
        "issuer": issuer,
        "class": title,
        "cusip": cusip,
        "value_k": value_k,
        "shares": shares,
        "share_type": share_type,
        "discretion": discretion,
    }


def normalize_holdings(raw_holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect value unit and normalize value_k to actual dollars.

    SEC spec says 13F values are in thousands, but many filers
    (Berkshire, Goldman, Morgan Stanley) report actual dollars.
    Heuristic: if raw total > $50B, the filer is using actual dollars.
    """
    if not raw_holdings:
        return raw_holdings

    total_raw = sum(h["value_k"] for h in raw_holdings)
    # If raw total exceeds $1B, values are likely actual dollars (not thousands)
    if total_raw > 1_000_000_000:
        # Already in actual dollars — value_k is correct as-is
        return raw_holdings
    else:
        # In thousands — multiply by 1000 to get actual dollars
        normalized = []
        for h in raw_holdings:
            normalized.append({**h, "value_k": h["value_k"] * 1000})
        return normalized


def aggregate_holdings(raw_holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate holdings by issuer, sum values across share classes.
    Returns sorted by total value descending."""
    by_issuer: dict[str, dict] = {}
    for h in raw_holdings:
        key = h["issuer"].upper().strip()
        if key not in by_issuer:
            by_issuer[key] = {
                "issuer": h["issuer"],
                "classes": [],
                "total_value_k": 0,
                "total_shares": 0,
            }
        by_issuer[key]["total_value_k"] += h["value_k"]
        by_issuer[key]["total_shares"] += h["shares"]
        by_issuer[key]["classes"].append({
            "class": h["class"],
            "value_k": h["value_k"],
            "shares": h["shares"],
            "cusip": h["cusip"],
        })

    aggregated = list(by_issuer.values())
    total = sum(a["total_value_k"] for a in aggregated)

    for a in aggregated:
        a["pct"] = round(a["total_value_k"] / total * 100, 2) if total > 0 else 0

    aggregated.sort(key=lambda x: x["total_value_k"], reverse=True)
    return aggregated


# ═══════════════════════════════════════════════════════════════
# A-Share (A股) institutional data fetching
# ═══════════════════════════════════════════════════════════════

# Custom headers for eastmoney API (required to avoid 403/ proxy issues)
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; daily-china-tech-scan/1.0; research@example.com)",
    "Referer": "https://data.eastmoney.com/",
}

_EM_REQUEST_DELAY = 0.3  # Respect eastmoney rate limits


def _em_get(url: str, params: dict | None = None, timeout: int = 15) -> dict | None:
    """GET request to eastmoney API with appropriate headers and rate limiting.
    Returns parsed JSON dict or None on failure.
    Explicitly disables proxy — eastmoney APIs reject proxy connections."""
    import time as _time
    _time.sleep(_EM_REQUEST_DELAY)
    try:
        resp = requests.get(
            url, params=params, headers=_EM_HEADERS, timeout=timeout,
            proxies={"http": None, "https": None},  # Disable proxy for eastmoney
        )
        if resp.status_code == 200:
            return resp.json()
        print(f"    [EM HTTP {resp.status_code}] {url[:80]}...")
        return None
    except requests.RequestException as e:
        print(f"    [EM ERR] {e}")
        return None


def fetch_qfii_sector_stocks(top_n: int = 20) -> dict | None:
    """Fetch QFII heavy holdings sector constituent stocks from eastmoney.
    Returns dict with: stocks (list), total_count, fetch_time, source.
    Data source: 东方财富 QFII重仓板块 (BK0535)
    NOTE: This is the QFII sector board — stocks that are held by ANY QFII,
    NOT broken down by individual institution."""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": str(top_n),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f20",  # sort by total market cap
        "fs": "b:BK0535",  # QFII重仓板块
        "fields": "f2,f3,f4,f12,f14,f15,f16,f17,f18,f20,f21",
    }
    data = _em_get(url, params)
    if data is None or not data.get("data"):
        return None

    diff_data = data["data"]
    stocks = []
    for row in diff_data.get("diff", []):
        try:
            stocks.append({
                "code": row.get("f12", ""),
                "name": row.get("f14", ""),
                "price": float(row.get("f2", 0) or 0),
                "change_pct": float(row.get("f3", 0) or 0),
                "high": float(row.get("f15", 0) or 0),
                "low": float(row.get("f16", 0) or 0),
                "volume": int(row.get("f17", 0) or 0),
                "turnover": float(row.get("f18", 0) or 0),
                "market_cap": float(row.get("f20", 0) or 0),
            })
        except (ValueError, TypeError):
            continue

    return {
        "stocks": stocks,
        "total_count": diff_data.get("total", 0),
        "fetch_time": datetime.now(timezone.utc).isoformat(),
        "source": "东方财富 QFII重仓板块 (BK0535)",
    }


def fetch_northbound_flow_summary(days: int = 5) -> dict | None:
    """Fetch recent northbound capital flow summary.
    Uses AKShare stock_hsgt_hist_em for both Shanghai and Shenzhen connect.
    Returns dict with: shanghai_flow, shenzhen_flow, total_flow, recent_days, source.
    NOTE: Data may be delayed by 1-2 trading days."""
    try:
        import akshare as ak
    except ImportError:
        print("    [WARN] AKShare not installed — skipping northbound flow data")
        return None

    try:
        df_sh = ak.stock_hsgt_hist_em(symbol="沪股通")
        df_sz = ak.stock_hsgt_hist_em(symbol="深股通")
    except Exception as e:
        print(f"    [WARN] Northbound flow fetch failed: {e}")
        return None

    if df_sh is None or df_sz is None or df_sh.empty or df_sz.empty:
        return None

    # Get the last N valid (non-NaN) trading days
    valid_sh = df_sh.dropna(subset=["当日成交净买额"]).tail(days)
    valid_sz = df_sz.dropna(subset=["当日成交净买额"]).tail(days)

    if valid_sh.empty and valid_sz.empty:
        return None

    # Align by date
    recent_days = []
    for _, row_sh in valid_sh.iterrows():
        date_str = str(row_sh["日期"])[:10]
        row_sz = valid_sz[valid_sz["日期"].astype(str).str[:10] == date_str]
        sz_net = float(row_sz.iloc[0]["当日成交净买额"]) if len(row_sz) > 0 else 0
        sh_net = float(row_sh["当日成交净买额"])
        recent_days.append({
            "date": date_str,
            "shanghai_net_buy": round(sh_net, 2),
            "shenzhen_net_buy": round(sz_net, 2),
            "total_net_buy": round(sh_net + sz_net, 2),
        })

    if not recent_days:
        return None

    total_net = sum(d["total_net_buy"] for d in recent_days)
    sh_total = sum(d["shanghai_net_buy"] for d in recent_days)
    sz_total = sum(d["shenzhen_net_buy"] for d in recent_days)

    return {
        "recent_days": recent_days,
        "shanghai_total_net": round(sh_total, 2),
        "shenzhen_total_net": round(sz_total, 2),
        "total_net_flow": round(total_net, 2),
        "trend": "inflow" if total_net > 0 else "outflow",
        "fetch_time": datetime.now(timezone.utc).isoformat(),
        "source": "AKShare / 东方财富 沪深港通数据",
        "data_lag_note": "数据可能存在1-2个交易日延迟，最新数据为上一交易日",
    }


def fetch_institutional_aggregate_stats() -> dict | None:
    """Fetch aggregate A-share institutional holding statistics.
    Uses AKShare stock_institute_hold for overall institutional participation.
    Returns dict with: total_stocks, avg_inst_count, high_inst_stocks, source."""
    try:
        import akshare as ak
    except ImportError:
        return None

    try:
        df = ak.stock_institute_hold()
    except Exception as e:
        print(f"    [WARN] Institutional aggregate stats failed: {e}")
        return None

    if df is None or df.empty:
        return None

    # Top stocks by institutional count
    if "机构数" in df.columns:
        df_sorted = df.sort_values("机构数", ascending=False)
        top_inst = []
        for _, row in df_sorted.head(10).iterrows():
            top_inst.append({
                "code": str(row.get("证券代码", "")),
                "name": str(row.get("证券简称", "")),
                "inst_count": int(row.get("机构数", 0) or 0),
                "inst_change": int(row.get("机构数变化", 0) or 0),
                "hold_ratio": float(row.get("持股比例", 0) or 0),
                "float_ratio": float(row.get("占流通股比例", 0) or 0),
            })

        return {
            "total_stocks": len(df),
            "avg_inst_count": round(float(df["机构数"].mean()), 1) if "机构数" in df.columns else 0,
            "top_by_inst_count": top_inst,
            "fetch_time": datetime.now(timezone.utc).isoformat(),
            "source": "AKShare / 东方财富 机构持股统计",
            "note": "此数据为全市场机构合计，不区分机构类型（含公募、QFII、保险等）",
        }

    return None


# ═══════════════════════════════════════════════════════════════
# Cache for change detection
# ═══════════════════════════════════════════════════════════════

def load_cache(output_dir: Path) -> dict:
    """Load previous holdings cache from JSON file."""
    cache_path = output_dir / "holdings_cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            print("[WARN] Cache corrupted, rebuilding...")
    return {}


def save_cache(output_dir: Path, cache: dict) -> None:
    """Save holdings cache to JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "holdings_cache.json"
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_changes(
    current: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare current vs previous quarter holdings. Returns change summary."""
    prev_map = {h["issuer"].upper().strip(): h for h in previous}
    curr_map = {h["issuer"].upper().strip(): h for h in current}

    curr_keys = set(curr_map.keys())
    prev_keys = set(prev_map.keys())

    new_positions = []
    exited_positions = []
    increased = []
    decreased = []

    for key in curr_keys - prev_keys:
        h = curr_map[key]
        new_positions.append({"issuer": h["issuer"], "value_k": h["total_value_k"],
                              "pct": h["pct"]})

    for key in prev_keys - curr_keys:
        h = prev_map[key]
        exited_positions.append({"issuer": h["issuer"], "value_k": h["total_value_k"],
                                 "pct": h["pct"]})

    for key in curr_keys & prev_keys:
        prev_val = prev_map[key]["total_value_k"]
        curr_val = curr_map[key]["total_value_k"]
        if prev_val == 0:
            continue
        change_pct = round((curr_val - prev_val) / prev_val * 100, 1)
        entry = {
            "issuer": curr_map[key]["issuer"],
            "prev_value_k": prev_val,
            "curr_value_k": curr_val,
            "change_pct": change_pct,
        }
        if change_pct >= 20:
            increased.append(entry)
        elif change_pct <= -20:
            decreased.append(entry)

    new_positions.sort(key=lambda x: x["value_k"], reverse=True)
    exited_positions.sort(key=lambda x: x["value_k"], reverse=True)
    increased.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    decreased.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

    prev_total = sum(h["total_value_k"] for h in previous)
    curr_total = sum(h["total_value_k"] for h in current)
    total_change_pct = (
        round((curr_total - prev_total) / prev_total * 100, 1) if prev_total > 0 else 0
    )

    return {
        "new_positions": new_positions,
        "exited_positions": exited_positions,
        "increased": increased[:10],
        "decreased": decreased[:10],
        "prev_total_value_k": prev_total,
        "curr_total_value_k": curr_total,
        "total_change_pct": total_change_pct,
        "has_changes": bool(new_positions or exited_positions or increased or decreased),
    }


# ═══════════════════════════════════════════════════════════════
# Filing calendar
# ═══════════════════════════════════════════════════════════════

def get_filing_schedule_info() -> dict[str, Any]:
    """Determine current quarter status and next filing deadline."""
    today = date.today()
    current_year = today.year

    schedules = []
    for q in [1, 2, 3, 4]:
        q_end_parts = Q_SCHEDULE[q]["quarter_end"].split("-")
        deadline_parts = Q_SCHEDULE[q]["deadline"].split("-")

        q_end = date(current_year, int(q_end_parts[0]), int(q_end_parts[1]))
        deadline = date(current_year, int(deadline_parts[0]), int(deadline_parts[1]))

        # Q4 deadline is Feb of next year
        if q == 4:
            deadline = date(current_year + 1, int(deadline_parts[0]), int(deadline_parts[1]))

        # Q1 deadline is May
        # All deadlines are in the same year as quarter_end except Q4

        status = "past"
        if today <= deadline + timedelta(days=45):
            status = "filing_window"
        if today <= deadline:
            status = "before_deadline"
        if today <= q_end:
            status = "current_quarter"

        days_until_deadline = (deadline - today).days

        schedules.append({
            "quarter": Q_SCHEDULE[q]["label"],
            "year": deadline.year,
            "quarter_end": q_end.isoformat(),
            "deadline": deadline.isoformat(),
            "days_until_deadline": days_until_deadline,
            "status": status,
        })

    # Find the most relevant schedule
    current = None
    latest_reported = None
    for s in schedules:
        if s["status"] in ("current_quarter", "before_deadline", "filing_window"):
            if current is None:
                current = s
        if latest_reported is None and s["status"] == "past":
            latest_reported = s
    if current is None:
        current = schedules[-1]
    if latest_reported is None:
        latest_reported = schedules[0]

    return {
        "current": current,
        "latest_reported": latest_reported,
        "all": schedules,
    }


# ═══════════════════════════════════════════════════════════════
# Report formatting
# ═══════════════════════════════════════════════════════════════

def format_value(val: int) -> str:
    """Format a dollar value to human-readable string.
    13F XML values are in actual dollars (despite SEC spec saying thousands)."""
    if val >= 1_000_000_000_000:
        return f"${val / 1_000_000_000_000:.2f}T"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    return f"${val:,.0f}"


def format_change_pct(pct: float) -> str:
    """Format change percentage with arrow."""
    if pct > 0.5:
        return f"\U0001f4c8 +{pct:.0f}%"
    elif pct < -0.5:
        return f"\U0001f4c9 {pct:.0f}%"
    return "➡ 持平"


def generate_report(
    all_data: list[dict[str, Any]],
    changes_by_cik: dict[str, dict],
    schedule: dict[str, Any],
    errors: list[str],
    a_share_data: dict[str, Any] | None = None,
) -> str:
    """Generate the daily markdown report."""
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    tz = datetime.now(timezone.utc) + timedelta(hours=8)
    beijing_str = tz.strftime("%Y-%m-%d %H:%M")

    report = f"""# \U0001f4ca 机构13F持仓追踪日报 — {today_str}

> \U0001f916 自动生成 | 北京时间 {beijing_str}
> 数据来源：SEC EDGAR（免费公开数据）
> 13F为季度申报（延后45天），非实时持仓

---

"""

    # ── Section 1: New disclosures today ──────────────────────
    today_filed = []
    check_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for d in all_data:
        if d.get("filing_date") == check_date and d.get("holdings"):
            today_filed.append(d)

    if today_filed:
        report += "## \U0001f514 今日新披露\n\n"
        for d in today_filed:
            report += (f"- **{d['name']}** 提交了截至 {d['quarter_end']} 的13F，"
                       f"共披露 {len(d['aggregated'])} 只标的\n")
        report += "\n"
    else:
        report += "## \U0001f514 今日新披露\n\n> 今日无新13F披露。以下为最新已知持仓快照。\n\n"

    # ── Section 2: Latest holdings snapshot ───────────────────
    report += "---\n\n## \U0001f4c8 最新持仓快照\n\n"

    # Sort by total portfolio value
    report_data = sorted(
        [d for d in all_data if d.get("aggregated")],
        key=lambda x: x.get("total_value_k", 0), reverse=True,
    )

    for d in report_data:
        name = d["name"]
        quarter = d.get("quarter_end", "?")
        filing_d = d.get("filing_date", "?")
        total_val = format_value(d.get("total_value_k", 0))
        n_holdings = len(d.get("aggregated", []))
        cache_key = d.get("cik", "")

        report += f"### {name}\n"
        report += f"> \U0001f4c5 截至 {quarter} | 申报日 {filing_d} | 披露 {n_holdings} 只标的 | 总持仓 {total_val}\n\n"

        # Top N holdings table
        report += "| # | 标的 | 市值 | 占比 |\n"
        report += "|---|------|------|------|\n"
        for i, h in enumerate(d["aggregated"][:TOP_N], 1):
            report += f"| {i} | **{h['issuer']}** | {format_value(h['total_value_k'])} | {h['pct']:.1f}% |\n"
        report += "\n"

        # Change summary (only if we have previous data)
        changes = changes_by_cik.get(cache_key, {})
        if changes.get("has_changes"):
            report += "<details>\n<summary>\U0001f4ca QoQ变动详情</summary>\n\n"
            new_pos = changes.get("new_positions", [])
            exited = changes.get("exited_positions", [])
            inc = changes.get("increased", [])
            dec = changes.get("decreased", [])

            total_chg = changes.get("total_change_pct", 0)
            report += f"**总仓位变化：{format_change_pct(total_chg)}**\n\n"

            if new_pos:
                report += "**\U0001f195 新增仓位：**\n"
                for p in new_pos[:5]:
                    report += f"- {p['issuer']}（{format_value(p['value_k'])}，占比 {p['pct']:.1f}%）\n"
                report += "\n"

            if exited:
                report += "**\U0001f6ab 清仓：**\n"
                for p in exited[:5]:
                    report += f"- {p['issuer']}（上季 {format_value(p['value_k'])}，占比 {p['pct']:.1f}%）\n"
                report += "\n"

            if inc:
                report += "**\U0001f4c8 大幅增持（+20%以上）：**\n"
                for p in inc[:5]:
                    report += f"- {p['issuer']}：{format_value(p['prev_value_k'])} → {format_value(p['curr_value_k'])}（+{p['change_pct']:.0f}%）\n"
                report += "\n"

            if dec:
                report += "**\U0001f4c9 大幅减持（-20%以上）：**\n"
                for p in dec[:5]:
                    report += f"- {p['issuer']}：{format_value(p['prev_value_k'])} → {format_value(p['curr_value_k'])}（{p['change_pct']:.0f}%）\n"
                report += "\n"

            report += "</details>\n\n"

    # ── Section 3: A-Share institutional data ──────────────────
    if a_share_data:
        report += "---\n\n## \U0001f1e8\U0001f1f3 A股机构持仓观察\n\n"

        qfii_data = a_share_data.get("qfii_sector")
        northbound = a_share_data.get("northbound_flow")
        inst_stats = a_share_data.get("institutional_stats")
        a_errors = a_share_data.get("errors", [])

        if a_errors:
            report += "> ⚠️ 部分A股数据获取失败：\n"
            for e in a_errors:
                report += f"> - {e}\n"
            report += "> 以下为可用数据，缺失项已标注。\n\n"

        # ── Sub-section 3a: Institution A-share status ──────────
        report += "### \U0001f3e6 主要机构A股参与情况\n\n"
        report += "> 以下为各机构在中国A股市场的已知参与情况。与美股13F不同，"
        report += "A股没有统一的机构持仓披露数据库，\n"
        report += "> 机构通过QFII、北向沪深港通、合资公募等多种渠道参与，"
        report += "具体持仓需查阅上市公司季报前十大股东。\n\n"

        report += "| 机构 | A股参与方式 | 数据确定性 |\n"
        report += "|------|------------|----------|\n"
        for inst in INSTITUTIONS:
            slug = inst["slug"]
            info = INSTITUTION_A_SHARE_INFO.get(slug, {})
            access = info.get("a_share_access", "未知")
            confidence = info.get("data_confidence", "unknown")
            conf_icon = {"confirmed": "✅ 已确认", "likely": "\U0001f50d 推断", "unknown": "❓ 未知"}.get(confidence, "❓")
            # Truncate long access descriptions
            display_access = access[:80] + "..." if len(access) > 80 else access
            report += f"| **{inst['name']}** | {display_access} | {conf_icon} |\n"

        report += "\n"

        # ── Sub-section 3b: Northbound flow ─────────────────────
        if northbound:
            report += "### \U0001f4b0 北向资金近期动向\n\n"
            trend = northbound.get("trend", "")
            total = northbound.get("total_net_flow", 0)
            trend_icon = "\U0001f7e2" if total > 0 else "\U0001f534"
            report += f"> {trend_icon} 近{len(northbound.get('recent_days', []))}个交易日"
            report += f"北向资金累计净{'流入' if total > 0 else '流出'} **{abs(total):.1f}亿元**\n"
            report += f"> 沪股通: {northbound.get('shanghai_total_net', 0):.1f}亿 | "
            report += f"深股通: {northbound.get('shenzhen_total_net', 0):.1f}亿\n\n"

            recent = northbound.get("recent_days", [])
            if recent:
                report += "| 日期 | 沪股通净买额(亿) | 深股通净买额(亿) | 合计(亿) |\n"
                report += "|------|-----------------|-----------------|--------|\n"
                for d in recent[-5:]:
                    dir_icon = "\U0001f7e2" if d["total_net_buy"] > 0 else "\U0001f534"
                    report += (f"| {d['date']} | {d['shanghai_net_buy']:.1f} "
                              f"| {d['shenzhen_net_buy']:.1f} | {dir_icon} {d['total_net_buy']:.1f} |\n")
                report += "\n"

            if northbound.get("data_lag_note"):
                report += f"> {northbound['data_lag_note']}\n\n"
        else:
            report += "### \U0001f4b0 北向资金近期动向\n\n"
            report += "> ⚠️ 北向资金数据暂不可用（数据源可能延迟或API变更）\n\n"

        # ── Sub-section 3c: QFII sector stocks ──────────────────
        if qfii_data and qfii_data.get("stocks"):
            report += "### \U0001f4ca QFII重仓板块 Top 20\n\n"
            report += f"> 数据来源：{qfii_data.get('source', '东方财富')}\n"
            report += f"> 注意：此列表为QFII板块成分股（按总市值排序），不区分具体QFII机构名称。\n\n"
            report += "| # | 代码 | 名称 | 最新价 | 涨跌幅 | 总市值(亿) |\n"
            report += "|---|------|------|--------|--------|----------|\n"
            for i, s in enumerate(qfii_data["stocks"][:20], 1):
                mcap = s.get("market_cap", 0) / 1e8 if s.get("market_cap") else 0
                chg = s.get("change_pct", 0)
                chg_str = f"\U0001f7e2 +{chg:.2f}%" if chg > 0 else f"\U0001f534 {chg:.2f}%"
                report += (f"| {i} | {s['code']} | **{s['name']}** | {s['price']:.2f} "
                          f"| {chg_str} | {mcap:.1f} |\n")
            report += "\n"
        else:
            report += "### \U0001f4ca QFII重仓板块\n\n"
            report += "> ⚠️ QFII板块数据暂不可用（数据源可能延迟或API变更）\n\n"

        # ── Sub-section 3d: Institutional aggregate stats ───────
        if inst_stats:
            report += "### \U0001f4ca 全市场机构持股统计\n\n"
            report += f"> {inst_stats.get('source', '')}\n"
            report += f"> {inst_stats.get('note', '')}\n\n"
            report += f"- **覆盖A股数量**：{inst_stats['total_stocks']} 只\n"
            report += f"- **平均机构持股数**：{inst_stats['avg_inst_count']} 家/只\n\n"

            top = inst_stats.get("top_by_inst_count", [])
            if top:
                report += "**机构持股数量最多的股票：**\n\n"
                report += "| # | 代码 | 名称 | 持股机构数 | 机构数变化 | 持股比例 |\n"
                report += "|---|------|------|----------|----------|--------|\n"
                for i, s in enumerate(top[:10], 1):
                    chg = s.get("inst_change", 0)
                    chg_str = f"+{chg}" if chg > 0 else str(chg)
                    report += (f"| {i} | {s['code']} | **{s['name']}** "
                              f"| {s['inst_count']} | {chg_str} | {s['hold_ratio']:.1f}% |\n")
                report += "\n"

        # ── Institution-specific known holdings ─────────────────
        report += "<details>\n<summary>\U0001f4cb 各机构A股参与详情</summary>\n\n"
        for inst in INSTITUTIONS:
            slug = inst["slug"]
            info = INSTITUTION_A_SHARE_INFO.get(slug, {})
            report += f"**{inst['name']}**\n"
            report += f"- 参与方式：{info.get('a_share_access', '未知')}\n"
            known = info.get("known_a_holdings", [])
            if known:
                for h in known:
                    report += f"- {h}\n"
            notes = info.get("notes", "")
            if notes:
                report += f"- {notes}\n"
            report += "\n"
        report += "</details>\n\n"

    # ── Section 4: Error log ──────────────────────────────────
    if errors:
        report += "---\n\n## ⚠️ 数据获取问题\n\n"
        for e in errors:
            report += f"- {e}\n"
        report += "\n"

    # ── Section 4: Filing calendar ────────────────────────────
    report += "---\n\n## \U0001f4c5 13F申报日历\n\n"

    curr = schedule.get("current", {})
    latest = schedule.get("latest_reported", {})

    report += f"- **当前周期**：{curr.get('quarter', '?')} {curr.get('year', '?')}\n"
    report += f"- **持仓截止日**：{curr.get('quarter_end', '?')}\n"
    report += f"- **申报截止日**：{curr.get('deadline', '?')}\n"

    days = curr.get("days_until_deadline", 0)
    if days > 0:
        report += f"- **距截止还有**：{days} 天\n"
    elif days < 0:
        report += f"- **截止日已过**：{-days} 天（申报可能仍在陆续提交中）\n"

    report += f"- **最新完整季度**：{latest.get('quarter', '?')} {latest.get('year', '?')}\n"

    report += "\n| 季度 | 截止日 | 申报截止 | 状态 |\n"
    report += "|------|--------|----------|------|\n"
    for s in schedule.get("all", []):
        status_map = {
            "current_quarter": "\U0001f504 当前季度",
            "before_deadline": "⏳ 待申报",
            "filing_window": "\U0001f4ec 申报中",
            "past": "✅ 已完成",
        }
        status = status_map.get(s["status"], "?")
        report += (f"| {s['quarter']} {s['year']} | {s['quarter_end']} "
                   f"| {s['deadline']} | {status} |\n")

    report += f"""
---

> ⚠️ **免责声明**：以上数据来自SEC EDGAR公开13F申报文件。13F仅披露多头权益仓位，不包含空头、期权、债券等。
> 申报延后45天，不代表当前实时持仓。数据仅供参考，不构成投资建议。
> 中国时间 {beijing_str} 自动生成。
"""
    return report


# ═══════════════════════════════════════════════════════════════
# Save report (matches YouTube module pattern)
# ═══════════════════════════════════════════════════════════════

def save_report(report: str, output_dir: Path) -> Path:
    """Save report to output directory (matches youtube_scan.py pattern)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = output_dir / f"holdings-report-{today}.md"
    filepath.write_text(report, encoding="utf-8")
    latest = output_dir / "holdings-report.md"
    latest.write_text(report, encoding="utf-8")
    print(f"[OK] Report saved to {filepath}")
    return filepath


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("13F Institutional Holdings Tracker")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    output_dir = Path("output")
    cache = load_cache(output_dir)

    all_data: list[dict[str, Any]] = []
    changes_by_cik: dict[str, dict] = {}
    errors: list[str] = []

    # ── Fetch 13F data for each institution ──
    success_count = 0
    for inst in INSTITUTIONS:
        name = inst["name"]
        cik = inst["cik"]
        slug = inst["slug"]
        print(f"\n── {name} (CIK: {cik}) ──")

        # Get latest 13F filing
        filing = get_latest_13f_filing(cik)
        if filing is None:
            msg = f"{name}: 无法获取13F申报记录"
            print(f"  [SKIP] {msg}")
            errors.append(msg)
            continue

        quarter_end = filing["quarter_end"]
        filing_date = filing["filing_date"]
        doc_url = filing["doc_url"]
        print(f"  Quarter: {quarter_end} | Filed: {filing_date}")
        print(f"  URL: {doc_url[:80]}...")

        # Parse holdings
        raw_holdings = download_and_parse_13f(doc_url)
        if not raw_holdings:
            msg = f"{name}: 13F解析失败（可能为新格式或XML结构变化）"
            print(f"  [WARN] {msg}")
            errors.append(msg)
            # Continue with cached data if available
            prev_data = cache.get(cik, {})
            prev_holdings = prev_data.get("holdings", [])
            if prev_holdings:
                aggregated = prev_holdings
                total_k = prev_data.get("total_value_k", 0)
            else:
                continue
        else:
            print(f"  Raw entries: {len(raw_holdings)}")
            normalized = normalize_holdings(raw_holdings)
            aggregated = aggregate_holdings(normalized)
            total_k = sum(h["total_value_k"] for h in aggregated)
            print(f"  Aggregated: {len(aggregated)} issuers, total {format_value(total_k)}")

        success_count += 1

        # Detect changes vs cache
        prev_data = cache.get(cik, {})
        prev_holdings = prev_data.get("holdings", [])
        prev_quarter = prev_data.get("quarter_end", "")

        if prev_holdings and quarter_end != prev_quarter:
            print(f"  New quarter detected! Comparing {prev_quarter} -> {quarter_end}")
            changes = detect_changes(aggregated, prev_holdings)
            changes_by_cik[cik] = changes
            if changes["has_changes"]:
                print(f"    New: {len(changes['new_positions'])} | "
                      f"Exited: {len(changes['exited_positions'])} | "
                      f"Increased: {len(changes['increased'])} | "
                      f"Decreased: {len(changes['decreased'])}")
        elif not prev_holdings:
            print(f"  First time scan — no baseline for change detection")

        all_data.append({
            "name": name,
            "cik": cik,
            "slug": slug,
            "quarter_end": quarter_end,
            "filing_date": filing_date,
            "accession_number": filing["accession_number"],
            "aggregated": aggregated,
            "total_value_k": total_k,
            "holdings": aggregated,  # for cache
        })

        # Update cache (store top 50 only to keep cache small)
        cache[cik] = {
            "name": name,
            "quarter_end": quarter_end,
            "filing_date": filing_date,
            "total_value_k": total_k,
            "holdings": aggregated[:50],
        }

    # ── Fetch A-share data ──
    print("\n── A-Share Institutional Data ──")
    a_share_data: dict[str, Any] = {}
    a_share_errors: list[str] = []

    # Disable proxy for eastmoney API calls (handle both lower/upper case variants)
    _orig_proxy_vars = {}
    for _key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        _val = os.environ.pop(_key, None)
        if _val is not None:
            _orig_proxy_vars[_key] = _val

    try:
        print("  Fetching QFII sector stocks... ", end="", flush=True)
        qfii_data = fetch_qfii_sector_stocks(top_n=20)
        if qfii_data:
            print(f"{len(qfii_data['stocks'])} stocks (total {qfii_data['total_count']})")
            a_share_data["qfii_sector"] = qfii_data
        else:
            print("unavailable")
            a_share_errors.append("QFII板块数据获取失败（东方财富API可能变更）")
    except Exception as e:
        print(f"error: {e}")
        a_share_errors.append(f"QFII板块数据获取异常: {e}")

    try:
        print("  Fetching northbound flow... ", end="", flush=True)
        northbound = fetch_northbound_flow_summary(days=5)
        if northbound and northbound.get("recent_days"):
            total = northbound["total_net_flow"]
            print(f"{len(northbound['recent_days'])} days, net {total:+.1f}亿")
            a_share_data["northbound_flow"] = northbound
        else:
            print("unavailable")
            a_share_errors.append("北向资金数据获取失败（AKShare数据源可能延迟）")
    except Exception as e:
        print(f"error: {e}")
        a_share_errors.append(f"北向资金数据异常: {e}")

    try:
        print("  Fetching institutional aggregate stats... ", end="", flush=True)
        inst_stats = fetch_institutional_aggregate_stats()
        if inst_stats:
            print(f"{inst_stats['total_stocks']} stocks, avg {inst_stats['avg_inst_count']} insts/stock")
            a_share_data["institutional_stats"] = inst_stats
        else:
            print("unavailable")
            a_share_errors.append("机构持股统计数据获取失败")
    except Exception as e:
        print(f"error: {e}")
        a_share_errors.append(f"机构持股统计异常: {e}")

    a_share_data["errors"] = a_share_errors

    # Restore proxy settings
    for _key, _val in _orig_proxy_vars.items():
        os.environ[_key] = _val

    # ── Generate report ──
    if success_count == 0:
        print("\n[ERROR] All institutions failed — generating placeholder report")
        today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
        report = f"""# 📊 机构13F持仓追踪日报 — {today_str}

> ⚠️ 今日所有机构数据获取失败。请检查SEC EDGAR API连接或查看日志。
>
> 13F数据为SEC公开信息，免费且无需API Key。如持续失败，可能是网络问题或SEC API变更。

{chr(10).join(f'- {e}' for e in errors) if errors else ''}

> 13F数据来自 SEC EDGAR，仅供参考，不构成投资建议。
"""
        # Even on failure, include A-share data if available
        if a_share_data.get("qfii_sector") or a_share_data.get("northbound_flow"):
            report += "\n---\n\n## 🇨🇳 A股机构持仓观察\n\n"
            report += "> ⚠️ 美股13F数据获取失败，但以下A股数据可用：\n\n"
            # Add simplified A-share section
            if a_share_data.get("northbound_flow"):
                nb = a_share_data["northbound_flow"]
                total = nb["total_net_flow"]
                report += f"- 北向资金近期累计净{'流入' if total > 0 else '流出'} {abs(total):.1f}亿元\n"
            if a_share_data.get("qfii_sector") and a_share_data["qfii_sector"].get("stocks"):
                report += f"- QFII重仓板块共 {a_share_data['qfii_sector']['total_count']} 只标的\n"
    else:
        schedule = get_filing_schedule_info()
        report = generate_report(all_data, changes_by_cik, schedule, errors, a_share_data)

    # ── Save ──
    report_path = save_report(report, output_dir)
    save_cache(output_dir, cache)

    print("\n" + "=" * 60)
    print("SCAN COMPLETE")
    print(f"  Institutions tracked: {len(INSTITUTIONS)}")
    print(f"  Successful: {success_count}")
    print(f"  Failed: {len(INSTITUTIONS) - success_count}")
    print(f"  Report: {report_path}")
    print(f"  Cache: {output_dir / 'holdings_cache.json'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
