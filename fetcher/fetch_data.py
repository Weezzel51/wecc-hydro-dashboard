"""
WECC Hydro Market Brief — Daily Data Fetcher
─────────────────────────────────────────────
Runs daily at 06:00 PT via GitHub Actions. Pulls from primary public
sources, writes a single JSON file consumed by the static dashboard.

Design rules
────────────
1. Graceful degradation: any source can fail without killing the run.
2. Primary sources only. No paywalled feeds. No scraping behind logins.
3. One JSON output. Frontend reads /data/dashboard.json — that's it.
4. Idempotent. Safe to re-run any time; output always reflects "now".

Environment
───────────
    EIA_API_KEY    Required for BPA mix.
                   Free at https://www.eia.gov/opendata/register.php
                   Set as GitHub Actions secret.
"""

import os
import sys
import json
import re
import datetime
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, List, Any
from xml.etree import ElementTree as ET

import requests

# ── Config ────────────────────────────────────────────────────────────
EIA_API_KEY  = os.environ.get("EIA_API_KEY", "")
OUT_PATH     = Path(__file__).resolve().parent.parent / "data" / "dashboard.json"
ARCHIVE_PATH = Path(__file__).resolve().parent.parent / "data" / "archive.json"
PT           = datetime.timezone(datetime.timedelta(hours=-7))
HTTP_TIMEOUT = 30
UA           = "WECC-Hydro-Brief/1.0 (https://wecchydrobrief.com)"

# Basin roster — 17 basins, north-to-south. Names MUST match the
# `name` property in data/geo/basins.geojson so the map renders.
BASINS: List[Dict[str, str]] = [
    # BC Columbia headwaters
    {"name": "Mica / Upper Columbia (BC)", "source": "bc",     "id": "UCOL"},
    {"name": "Kootenay (BC)",              "source": "bc",     "id": "KOOT"},
    # PNW (Columbia + Snake)
    {"name": "Upper Columbia (US)",        "source": "snotel", "id": "170200"},
    {"name": "Pend Oreille",               "source": "snotel", "id": "170102"},
    {"name": "Yakima",                     "source": "snotel", "id": "170300"},
    {"name": "Clearwater",                 "source": "snotel", "id": "170603"},
    {"name": "Salmon",                     "source": "snotel", "id": "170602"},
    {"name": "Upper Snake",                "source": "snotel", "id": "170401"},
    {"name": "Lower Snake",                "source": "snotel", "id": "170601"},
    {"name": "Owyhee",                     "source": "snotel", "id": "170501"},
    {"name": "Lower Columbia",             "source": "snotel", "id": "170800"},
    # CAISO Sierra
    {"name": "Northern Sierra (Feather)",  "source": "cdec",   "id": "NSF"},
    {"name": "Central Sierra (American)",  "source": "cdec",   "id": "CSF"},
    {"name": "Southern Sierra (San Joaq.)","source": "cdec",   "id": "SSF"},
    {"name": "Tulare (Kings/Kern)",        "source": "cdec",   "id": "TLR"},
    # Colorado (no SWE source feed; basin shows on map but stays "no data")
    {"name": "Upper Colorado",             "source": "none",   "id": "UCOLO"},
    {"name": "Lower Colorado",             "source": "none",   "id": "LCOLO"},
]

# USACE NWD projects to fetch discharge for.
USACE_PROJECTS: List[Dict[str, str]] = [
    {"name": "Grand Coulee", "code": "GCL"},
    {"name": "Chief Joseph", "code": "CHJ"},
    {"name": "Lower Granite","code": "LWG"},
    {"name": "The Dalles",   "code": "TDA"},
    {"name": "Bonneville",   "code": "BON"},
]


# ── Utility ───────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    print(msg, flush=True)


def _safe(fn, label: str):
    try:
        return fn()
    except Exception as e:
        _log(f"  ✗ {label}: {type(e).__name__}: {e}")
        return None


def _http_get(url: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


# ══════════════════════════════════════════════════════════════════════
#  TIER 1 — EIA-930 (working) + EIA ICE bulk XLS (new)
# ══════════════════════════════════════════════════════════════════════

def fetch_eia_bpa_mix() -> Optional[Dict[str, int]]:
    """
    EIA-930 BPA balancing-authority hourly generation by fuel type.
    Averages the last 24h into hydro / wind / thermal-and-other.
    """
    if not EIA_API_KEY:
        _log("  ⚠ EIA_API_KEY unset — skipping BPA mix")
        return None

    end   = datetime.datetime.utcnow()
    start = end - datetime.timedelta(days=2)
    url   = "https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
    params = {
        "api_key": EIA_API_KEY,
        "frequency": "hourly",
        "data[0]": "value",
        "facets[respondent][]": "BPAT",
        "start": start.strftime("%Y-%m-%dT%H"),
        "end":   end.strftime("%Y-%m-%dT%H"),
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 5000,
    }
    rows = _http_get(url, params=params).json().get("response", {}).get("data", [])
    if not rows:
        return None

    by_hour: Dict[str, Dict[str, float]] = {}
    for row in rows:
        h, ft, v = row.get("period"), row.get("fueltype"), row.get("value")
        if h is None or ft is None or v is None:
            continue
        by_hour.setdefault(h, {})[ft] = float(v)
    last_24 = sorted(by_hour.keys(), reverse=True)[:24]

    totals: Dict[str, float] = {}
    for h in last_24:
        for ft, v in by_hour[h].items():
            totals[ft] = totals.get(ft, 0.0) + v

    grand = sum(totals.values()) or 1.0
    hydro = totals.get("WAT", 0.0) / grand * 100
    wind  = totals.get("WND", 0.0) / grand * 100
    therm = 100.0 - hydro - wind
    _log(f"  ✓ BPA mix: hydro {hydro:.0f}% / wind {wind:.0f}% / other {therm:.0f}%")
    return {
        "hydro_pct":   round(hydro),
        "wind_pct":    round(wind),
        "thermal_pct": round(therm),
    }


def fetch_eia_midc_price() -> Optional[Dict[str, Any]]:
    """
    Download EIA's bulk ICE wholesale electricity XLS, parse Mid-C peak.

    Source: https://www.eia.gov/electricity/wholesale/
            The page links to ice_electric-YYYY-YYYY.xlsx (current year file).
    Why this and not Today in Energy: bulk XLS is an authoritative republication
    of ICE's daily indices under EIA's licensing agreement, includes peak +
    off-peak + weighted-avg + volume, history back to 2001. Today in Energy
    only shows the previous day's headline.
    """
    import openpyxl

    year = datetime.date.today().year
    # The file is named for the year range it covers. Current convention:
    # "ice_electric-2010-{year}.xlsx" — EIA expands the range each year.
    candidates = [
        f"https://www.eia.gov/electricity/wholesale/xls/ice_electric-2010-{year}.xlsx",
        f"https://www.eia.gov/electricity/wholesale/xls/ice_electric-2010-{year - 1}.xlsx",
    ]
    xls_bytes = None
    for url in candidates:
        try:
            r = _http_get(url)
            xls_bytes = r.content
            _log(f"  · fetched {url.rsplit('/', 1)[-1]} ({len(xls_bytes)//1024} KB)")
            break
        except requests.HTTPError:
            continue
    if xls_bytes is None:
        raise RuntimeError("EIA ICE bulk XLS not found at expected URLs")

    import io
    wb = openpyxl.load_workbook(io.BytesIO(xls_bytes), data_only=True, read_only=True)

    # Sheet naming varies by year; find sheets whose name contains "Mid C"
    # (the actual hub name in EIA's file is "Mid C Peak" / "Mid C Off-Peak").
    peak_sheet = None
    offp_sheet = None
    for name in wb.sheetnames:
        norm = name.lower().replace("-", " ").replace("  ", " ")
        if "mid c" in norm and "peak" in norm and "off" not in norm:
            peak_sheet = name
        elif "mid c" in norm and "off" in norm:
            offp_sheet = name
    if not peak_sheet:
        raise RuntimeError(f"No Mid-C peak sheet in {wb.sheetnames}")

    def read_sheet(name):
        """Returns list of (date, wtd_avg_price) tuples, newest last."""
        ws = wb[name]
        # Header row: identify columns. EIA layout: Trade Date, Delivery Start,
        # Delivery End, High, Low, Wtd Avg Price $/MWh, Daily Volume MWh, # Trades.
        header = None
        out = []
        for row in ws.iter_rows(values_only=True):
            if header is None:
                if row and isinstance(row[0], str) and "date" in row[0].lower():
                    header = [str(c or "").lower().strip() for c in row]
                continue
            if not row or row[0] is None:
                continue
            d = row[0]
            if isinstance(d, datetime.datetime):
                d = d.date()
            elif isinstance(d, str):
                try:
                    d = datetime.date.fromisoformat(d[:10])
                except ValueError:
                    continue
            # find wtd avg column
            try:
                wtd_idx = next(i for i, h in enumerate(header) if "wtd" in h and "avg" in h)
            except StopIteration:
                wtd_idx = 5  # fallback: typical EIA layout
            try:
                p = float(row[wtd_idx])
            except (TypeError, ValueError):
                continue
            out.append((d, p))
        return out

    peak_series = read_sheet(peak_sheet)
    offp_series = read_sheet(offp_sheet) if offp_sheet else []

    if not peak_series:
        raise RuntimeError("Peak series empty")

    peak_series.sort()
    if offp_series:
        offp_series.sort()

    latest_date, latest_price = peak_series[-1]
    offp_latest = offp_series[-1][1] if offp_series else None

    # 7-day WoW
    week_ago = latest_date - datetime.timedelta(days=7)
    prior = next((p for d, p in reversed(peak_series[:-1]) if d <= week_ago), None)
    wow_pct = ((latest_price - prior) / prior * 100) if prior else None

    # 30-day chart history
    history = [
        {"d": _fmt_short_date(d.isoformat()), "p": round(p, 2)}
        for d, p in peak_series[-30:]
    ]

    _log(f"  ✓ Mid-C peak DA: ${latest_price:.2f}/MWh ({latest_date.isoformat()})")
    return {
        "peak_da":    round(latest_price, 2),
        "offpeak_da": round(offp_latest, 2) if offp_latest is not None else None,
        "wow_pct":    round(wow_pct, 1) if wow_pct is not None else None,
        "history":    history,
        "as_of":      latest_date.isoformat(),
    }


def _fmt_short_date(iso: str) -> str:
    d = datetime.date.fromisoformat(iso[:10])
    return d.strftime("%b %-d") if sys.platform != "win32" else d.strftime("%b %#d")


# ══════════════════════════════════════════════════════════════════════
#  TIER 2 STUBS (filled out in future sessions)
# ══════════════════════════════════════════════════════════════════════

def fetch_snotel_basin(huc: str) -> Optional[Dict[str, Any]]:
    """NRCS AWDB — basin SWE % of median. Stub."""
    return None

def fetch_cdec_sierra(region_id: str) -> Optional[Dict[str, Any]]:
    """CDEC Sierra region SWE. Stub."""
    return None

def fetch_bc_snow(region_id: str) -> Optional[Dict[str, Any]]:
    """BC RFC automated snow weather stations. Stub."""
    return None

def fetch_nwrfc_wsf() -> Optional[Dict[str, Any]]:
    """NWRFC Apr–Sep water supply forecast at The Dalles. Stub."""
    return None

def fetch_usace_project(code: str) -> Optional[Dict[str, Any]]:
    """USACE NWD dataquery — discharge + forebay. Stub."""
    return None

def fetch_caiso_hydro() -> Optional[Dict[str, Any]]:
    """CAISO OASIS — hydro dispatch + SP15. Stub."""
    return None


# ══════════════════════════════════════════════════════════════════════
#  REGULATORY PULSE — FERC + CAISO + BPA (3 feeds, deduped, max 6 items)
# ══════════════════════════════════════════════════════════════════════

# Hydropower keywords for filtering noisy feeds. Matches case-insensitive.
HYDRO_KEYWORDS = re.compile(
    r"\b(hydro|hydropower|hydroelectric|pumped storage|psh|"
    r"dam|reservoir|spillway|spill|columbia|snake|"
    r"bpa|bonneville|edam|wem|weim|markets\+?|markets plus|"
    r"ferc license|relicens|water power|"
    r"colorado river|hoover|glen canyon|"
    r"caiso|cal[ -]?iso|wecc)\b",
    re.IGNORECASE
)


def fetch_ferc_filings(max_items: int = 5) -> List[Dict[str, str]]:
    """
    FERC eForms RSS — filter for hydropower-relevant filings.
    Source: https://ecollection.ferc.gov/api/rssfeed
    """
    url = "https://ecollection.ferc.gov/api/rssfeed"
    r = _http_get(url)
    root = ET.fromstring(r.content)
    items = []
    # RSS 2.0: channel > item
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not HYDRO_KEYWORDS.search(title):
            continue
        link  = (item.findtext("link")  or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()
        try:
            dt = datetime.datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S")
            date_iso = dt.date().isoformat()
        except ValueError:
            date_iso = ""
        items.append({
            "tag": "FERC", "tag_class": "ferc",
            "title": title, "date": date_iso,
            "source": "FERC eLibrary", "url": link
        })
        if len(items) >= max_items:
            break
    _log(f"  · FERC: {len(items)} hydropower-relevant items")
    return items


def fetch_caiso_notices(max_items: int = 5) -> List[Dict[str, str]]:
    """
    CAISO market notices — scrape the notices listing page.
    Source: https://www.caiso.com/library/notices-iso-news-and-information
    """
    from bs4 import BeautifulSoup
    url = "https://www.caiso.com/library/notices-iso-news-and-information"
    r = _http_get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    # CAISO notices page renders each notice as <article> or list rows;
    # be permissive: any anchor inside a date-prefixed row.
    for row in soup.select("article, li, tr"):
        a = row.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 12:
            continue
        if not HYDRO_KEYWORDS.search(title):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.caiso.com" + href
        # Try to extract a date from the row text
        text = row.get_text(" ", strip=True)
        date_iso = ""
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
        if m:
            try:
                date_iso = datetime.datetime.strptime(m.group(1), "%m/%d/%Y").date().isoformat()
            except ValueError:
                pass
        items.append({
            "tag": "CAISO", "tag_class": "market",
            "title": title, "date": date_iso,
            "source": "CAISO Notices", "url": href
        })
        if len(items) >= max_items:
            break
    _log(f"  · CAISO: {len(items)} hydropower-relevant items")
    return items


def fetch_bpa_news(max_items: int = 5) -> List[Dict[str, str]]:
    """
    BPA press releases — scrape the news releases listing page.
    Source: https://www.bpa.gov/about/newsroom/news-releases
    """
    from bs4 import BeautifulSoup
    url = "https://www.bpa.gov/about/newsroom/news-releases"
    r = _http_get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if not title or len(title) < 18:
            continue
        # BPA releases are prefixed "PR-NN-YY" — use as a filter signal too
        is_release = bool(re.match(r"PR-\d{2}-\d{2}", title))
        if not (is_release or HYDRO_KEYWORDS.search(title)):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.bpa.gov" + href
        # BPA listing pages don't always show dates inline; leave blank
        items.append({
            "tag": "BPA", "tag_class": "ops",
            "title": re.sub(r"^PR-\d{2}-\d{2}\s*", "", title),
            "date": "",
            "source": "BPA Newsroom", "url": href
        })
        if len(items) >= max_items:
            break
    _log(f"  · BPA: {len(items)} items")
    return items


def fetch_regulatory_pulse() -> List[Dict[str, str]]:
    """
    Pull from 3 feeds, dedupe by title, sort by date desc, cap at 6.
    """
    feeds = []
    for label, fn in [
        ("FERC",  fetch_ferc_filings),
        ("CAISO", fetch_caiso_notices),
        ("BPA",   fetch_bpa_news),
    ]:
        result = _safe(fn, label)
        if result:
            feeds.extend(result)

    # Dedupe by lowercase title
    seen = set()
    deduped = []
    for it in feeds:
        key = it["title"].lower().strip()[:80]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    # Sort: items with dates first (newest), then dateless items
    deduped.sort(key=lambda x: x.get("date") or "0000", reverse=True)
    return deduped[:6]


# ══════════════════════════════════════════════════════════════════════
#  ARCHIVE (hand-maintained)
# ══════════════════════════════════════════════════════════════════════

def load_archive() -> List[Dict[str, str]]:
    if ARCHIVE_PATH.exists():
        try:
            return json.loads(ARCHIVE_PATH.read_text())
        except Exception as e:
            _log(f"  ⚠ archive.json parse error: {e}")
    return []


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    now = datetime.datetime.now(PT)
    _log(f"━━━ WECC Hydro fetch · {now.isoformat()} ━━━")

    # Basins
    _log("Basins:")
    basins: List[Dict[str, Any]] = []
    for b in BASINS:
        if   b["source"] == "snotel": data = _safe(lambda: fetch_snotel_basin(b["id"]), f"SNOTEL {b['id']}")
        elif b["source"] == "cdec":   data = _safe(lambda: fetch_cdec_sierra(b["id"]),  f"CDEC {b['id']}")
        elif b["source"] == "bc":     data = _safe(lambda: fetch_bc_snow(b["id"]),      f"BC {b['id']}")
        else: data = None
        if data:
            basins.append({"name": b["name"], **data})
            _log(f"  ✓ {b['name']}: {data.get('pct_median')}%")

    # Tier-1 markets
    _log("Markets:")
    bpa_mix = _safe(fetch_eia_bpa_mix,   "EIA-930 BPA")
    midc    = _safe(fetch_eia_midc_price, "EIA ICE bulk XLS Mid-C")
    wsf     = _safe(fetch_nwrfc_wsf,      "NWRFC WSF")

    # Ops
    _log("Ops:")
    usace_rows: List[Dict[str, Any]] = []
    for p in USACE_PROJECTS:
        data = _safe(lambda: fetch_usace_project(p["code"]), f"USACE {p['code']}")
        if data:
            usace_rows.append({"project": p["name"], **data})
    caiso = _safe(fetch_caiso_hydro, "CAISO OASIS")

    # Regulatory (3 feeds combined)
    _log("Regulatory pulse:")
    reg = _safe(fetch_regulatory_pulse, "Regulatory pulse") or []
    _log(f"  ✓ {len(reg)} items after dedupe")

    archive = load_archive()

    output = {
        "meta": {
            "last_updated_pt": now.strftime("%Y-%m-%d %H:%M PT"),
            "next_update_pt":  (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M PT"),
            "sources_ok": [k for k, v in {
                "bpa_mix": bpa_mix, "midc": midc, "wsf": wsf,
                "caiso": caiso, "usace": bool(usace_rows),
                "basins": bool(basins), "regulatory": bool(reg),
            }.items() if v],
        },
        "basins":     basins,
        "wsf":        wsf or {},
        "midc":       midc or {},
        "bpa_mix":    bpa_mix or {},
        "usace":      usace_rows,
        "caiso":      caiso or {},
        "regulatory": reg,
        "archive":    archive,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, indent=2, default=str))
    _log(f"━━━ wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes) ━━━")
    return 0


if __name__ == "__main__":
    sys.exit(main())
