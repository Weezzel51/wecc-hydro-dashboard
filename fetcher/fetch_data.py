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
    EIA Wholesale Market Prices JSON API — Mid-Columbia peak DA price.

    Source: https://api.eia.gov/v2/electricity/wholesale-market-prices/
    Uses the same EIA API key as the BPA fuel mix. Returns last 30 days
    of Mid-C peak day-ahead prices. No Excel/openpyxl needed.

    Location code for Mid-Columbia: "Mid-C" or "MIC" depending on EIA
    dataset version. We query both and take whichever returns data.
    """
    if not EIA_API_KEY:
        _log("  ⚠ EIA_API_KEY unset — skipping Mid-C price")
        return None

    today = datetime.date.today()
    start = (today - datetime.timedelta(days=45)).isoformat()
    end   = today.isoformat()

    url = "https://api.eia.gov/v2/electricity/wholesale-market-prices/data/"
    # Try both location codes EIA has used for Mid-Columbia
    for loc in ("Mid-C", "MIC", "Mid Columbia"):
        params = {
            "api_key":             EIA_API_KEY,
            "facets[location][]":  loc,
            "facets[type][]":      "peak",
            "start":               start,
            "end":                 end,
            "sort[0][column]":     "period",
            "sort[0][direction]":  "desc",
            "length":              60,
        }
        try:
            rows = _http_get(url, params=params).json().get("response", {}).get("data", [])
        except Exception:
            rows = []
        if rows:
            break

    if not rows:
        _log("  ✗ EIA Mid-C price: no data returned for any location code")
        return None

    # rows are sorted newest-first; each has {"period": "YYYY-MM-DD", "price": 123.45, ...}
    series: List[tuple] = []
    for row in rows:
        try:
            d = datetime.date.fromisoformat(row["period"][:10])
            p = float(row.get("price") or row.get("value") or 0)
            if p > 0:
                series.append((d, p))
        except (KeyError, ValueError, TypeError):
            continue

    if not series:
        _log("  ✗ EIA Mid-C price: could not parse any price values")
        return None

    series.sort()
    latest_date, latest_price = series[-1]

    # 7-day WoW
    week_ago = latest_date - datetime.timedelta(days=7)
    prior    = next((p for d, p in reversed(series[:-1]) if d <= week_ago), None)
    wow_pct  = round((latest_price - prior) / prior * 100, 1) if prior else None

    # 30-day chart history (oldest→newest for Chart.js)
    history = [
        {"d": _fmt_short_date(d.isoformat()), "p": round(p, 2)}
        for d, p in series[-30:]
    ]

    _log(f"  ✓ Mid-C peak DA: ${latest_price:.2f}/MWh ({latest_date.isoformat()})")
    return {
        "peak_da":    round(latest_price, 2),
        "offpeak_da": None,
        "wow_pct":    wow_pct,
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
    """
    NRCS Report Generator CSV — basin SWE % of 1991-2020 median for a HUC-6.

    Uses the NRCS Report Generator, the same source the WECC Hydro Brief uses
    manually each week. Returns a pre-computed % of median — no station-level
    averaging needed.

    URL pattern:
    https://wcc.sc.egov.usda.gov/reportGenerator/view_csv/
      customBasinTimeSeriesGroupBy,basin/daily/start_of_period_values/
      {HUC6}|0|SNTL|SNOWPACK_UPDATED/POR_BEGIN,POR_END/
      WTEQ::value,WTEQ::pctOfMedian_1991

    """
    today    = datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)

    # NRCS Report Generator — pre-computed basin SWE % of 1991-2020 median.
    # Returns a two-row CSV (today + week-ago) for the given HUC-6.
    base = "https://wcc.sc.egov.usda.gov/reportGenerator/view_csv/customBasinTimeSeriesGroupBy,basin/daily/start_of_period_values"
    period = f"{week_ago.isoformat()},{today.isoformat()}"
    path   = f"{base}/{huc}|0|SNTL|SNOWPACK_UPDATED/{period}/WTEQ::value,WTEQ::pctOfMedian_1991"

    try:
        r = _http_get(path)
    except Exception as e:
        _log(f"    ✗ SNOTEL {huc}: {e}")
        return None

    # CSV has comment lines starting with '#', then a header, then data rows.
    # Columns: Date, SWE (in), % of Median
    rows = []
    for ln in r.text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.lower().startswith("date"):
            continue  # header
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 3:
            rows.append(parts)

    if not rows:
        _log(f"    ⚠ SNOTEL {huc}: no data rows in response")
        return None

    def parse_row(parts):
        try:
            swe = float(parts[1]) if parts[1] not in ("", "-", "N/A") else None
            pct = int(float(parts[2])) if parts[2] not in ("", "-", "N/A") else None
            return swe, pct
        except (ValueError, IndexError):
            return None, None

    # Most recent row = today (or most recent available)
    swe_today, pct_today = parse_row(rows[-1])
    swe_week,  pct_week  = parse_row(rows[0]) if len(rows) > 1 else (None, None)

    delta_7d = round(pct_today - pct_week, 1) if (pct_today is not None and pct_week is not None) else None

    _log(f"    ✓ SNOTEL {huc}: {swe_today}\" / {pct_today}% of median")
    return {
        "pct_median": pct_today,
        "swe_in":     round(swe_today, 1) if swe_today is not None else None,
        "delta_7d":   delta_7d,
        "as_of":      today.isoformat(),
    }


def fetch_cdec_sierra(region_id: str) -> Optional[Dict[str, Any]]:
    """
    CDEC regional SWE % of average — same Report Generator approach as SNOTEL.
    Uses NRCS station data mapped to CDEC HUC-8 codes inside Sierra basins.

    Region mapping to NRCS HUC-8 representative sub-basins:
      NSF → 18020101 (Upper Feather)
      CSF → 18020111 (Upper American)
      SSF → 18040001 (Upper San Joaquin)
      TLR → 18030001 (Kings)
    """
    huc8_map = {
        "NSF": "18020101",
        "CSF": "18020111",
        "SSF": "18040001",
        "TLR": "18030001",
    }
    huc8 = huc8_map.get(region_id)
    if not huc8:
        return None

    today    = datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)
    base     = "https://wcc.sc.egov.usda.gov/reportGenerator/view_csv/customBasinTimeSeriesGroupBy,basin/daily/start_of_period_values"
    period   = f"{week_ago.isoformat()},{today.isoformat()}"
    path     = f"{base}/{huc8}|0|SNTL|SNOWPACK_UPDATED/{period}/WTEQ::value,WTEQ::pctOfMedian_1991"

    try:
        r = _http_get(path)
    except Exception as e:
        _log(f"    ✗ CDEC {region_id}: {e}")
        return None

    rows = []
    for ln in r.text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or ln.lower().startswith("date"):
            continue
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 3:
            rows.append(parts)

    if not rows:
        return None

    def _parse(parts):
        try:
            swe = float(parts[1]) if parts[1] not in ("", "-", "N/A") else None
            pct = int(float(parts[2])) if parts[2] not in ("", "-", "N/A") else None
            return swe, pct
        except (ValueError, IndexError):
            return None, None

    swe_today, pct_today = _parse(rows[-1])
    swe_week,  pct_week  = _parse(rows[0]) if len(rows) > 1 else (None, None)
    delta_7d = round(pct_today - pct_week, 1) if (pct_today and pct_week) else None

    return {
        "pct_median": pct_today,
        "swe_in":     round(swe_today, 1) if swe_today is not None else None,
        "delta_7d":   delta_7d,
        "as_of":      today.isoformat(),
    }


def fetch_bc_snow(region_id: str) -> Optional[Dict[str, Any]]:
    """
    BC basins — use NRCS HUC-8 stations near the US/BC border as a proxy,
    since BC RFC data is not machine-readable. Upper Columbia US-side
    snowpack (HUC 17020001) correlates with BC Upper Columbia headwaters.
    Returns None if no data; map shows "no data" for BC basins gracefully.
    """
    # BC RFC data not reliably machine-readable. Return None and let the
    # dashboard show "no data" for these two basins. They'll turn gray
    # which is honest — we don't have a free, stable API for BC data.
    return None


def fetch_nwrfc_wsf() -> Optional[Dict[str, Any]]:
    """
    NWRFC Apr–Sep unregulated water supply forecast at The Dalles.
    Uses the NWRFC JSON API endpoint.
    Source: https://www.nwrfc.noaa.gov/water_supply/
    """
    # NWRFC Water Supply API — returns JSON with current forecast
    url = "https://www.nwrfc.noaa.gov/water_supply/ws_api.php"
    params = {"loc": "TDAO3", "type": "json"}
    try:
        r = _http_get(url, params=params)
        data = r.json()
    except Exception:
        # Fallback: parse the text page
        try:
            r2 = _http_get("https://www.nwrfc.noaa.gov/water_supply/ws_text.php",
                           params={"id": "TDAO3", "wfo": ""})
            text = r2.text
        except Exception as e2:
            _log(f"  ✗ NWRFC WSF: {e2}")
            return None
        # Scan for "Apr" and a percentage on the same line
        for line in text.splitlines():
            if re.search(r"\bApr\b", line, re.IGNORECASE):
                m_pct = re.search(r"(\d{1,3})\s*%", line)
                m_maf = re.search(r"(\d{2,3}(?:\.\d)?)\s*(?=\s+\d{1,3}%)", line)
                if m_pct:
                    pct = int(m_pct.group(1))
                    maf = float(m_maf.group(1)) if m_maf else None
                    _log(f"  ✓ NWRFC WSF (text): {pct}% / {maf} MAF")
                    return {"site": "The Dalles", "pct_normal": pct,
                            "forecast_maf": maf, "as_of": datetime.date.today().isoformat()}
        _log("  ✗ NWRFC WSF: no parseable data")
        return None

    # JSON path — structure varies; try common keys
    today = datetime.date.today()
    try:
        # Typical structure: {"forecasts": [{"period": "Apr-Sep", "volume": 123, "pct_normal": 88}]}
        forecasts = data.get("forecasts") or data.get("data") or []
        for f in forecasts:
            period = str(f.get("period") or f.get("forecastPeriod") or "")
            if "Apr" in period or "apr" in period:
                pct = f.get("pct_normal") or f.get("percentNormal") or f.get("percent_normal")
                maf = f.get("volume") or f.get("forecastVolume")
                if pct is not None:
                    _log(f"  ✓ NWRFC WSF: {pct}% / {maf} MAF")
                    return {"site": "The Dalles", "pct_normal": int(pct),
                            "forecast_maf": float(maf) if maf else None,
                            "as_of": today.isoformat()}
    except Exception:
        pass
    _log("  ✗ NWRFC WSF: JSON structure unrecognized")
    return None


# USGS gauge IDs for major Columbia/Snake dams (station below dam)
USGS_GAUGE_MAP = {
    "GCL": ("12436500",  "Grand Coulee"),
    "CHJ": ("12437522",  "Chief Joseph"),
    "LWG": ("13340600",  "Lower Granite"),
    "TDA": ("14105700",  "The Dalles"),
    "BON": ("14128910",  "Bonneville"),
}

def fetch_usace_project(code: str) -> Optional[Dict[str, Any]]:
    """
    USGS NWIS instantaneous values — discharge (cfs→kcfs) for major Columbia
    River projects. Uses USGS gauge stations immediately below each dam.

    Source: https://waterservices.usgs.gov/nwis/iv/
    Parameter 00060 = discharge (cfs). No API key required.
    """
    if code not in USGS_GAUGE_MAP:
        return None
    gauge_id, _ = USGS_GAUGE_MAP[code]

    url = "https://waterservices.usgs.gov/nwis/iv/"
    params = {
        "sites":       gauge_id,
        "parameterCd": "00060,00065",  # discharge + gage height
        "period":      "P2D",
        "format":      "json",
    }
    try:
        r = _http_get(url, params=params)
        data = r.json()
    except Exception as e:
        _log(f"  ✗ USGS {code} ({gauge_id}): {e}")
        return None

    discharge_kcfs = None
    forebay_ft     = None

    try:
        ts_list = data["value"]["timeSeries"]
        for ts in ts_list:
            var_code = ts["variable"]["variableCode"][0]["value"]
            values   = ts["values"][0]["value"]
            # Find most recent non-null value
            for v in reversed(values):
                val_str = v.get("value")
                if val_str and val_str != "-999999":
                    val = float(val_str)
                    if var_code == "00060":
                        discharge_kcfs = round(val / 1000, 1)  # cfs → kcfs
                    elif var_code == "00065":
                        forebay_ft = round(val, 1)
                    break
    except (KeyError, IndexError, ValueError, TypeError) as e:
        _log(f"  ✗ USGS {code}: parse error: {e}")
        return None

    if discharge_kcfs is None:
        _log(f"  ✗ USGS {code}: no discharge value")
        return None

    _log(f"  ✓ USGS {code}: {discharge_kcfs} kcfs / {forebay_ft} ft")
    return {
        "discharge_kcfs": discharge_kcfs,
        "forebay_ft":     forebay_ft,
        "as_of":          datetime.date.today().isoformat(),
    }


def fetch_caiso_hydro() -> Optional[Dict[str, Any]]:
    """
    CAISO hydro via EIA-930 — same infrastructure as BPA mix, just for
    CISO (California ISO) balancing authority. Pulls WAT (water/hydro)
    fuel type for the last 24h and reports average MW.

    No API key needed beyond the existing EIA_API_KEY.
    """
    if not EIA_API_KEY:
        return None

    end   = datetime.datetime.utcnow()
    start = end - datetime.timedelta(days=2)
    url   = "https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
    params = {
        "api_key":             EIA_API_KEY,
        "frequency":           "hourly",
        "data[0]":             "value",
        "facets[respondent][]": "CISO",
        "start":  start.strftime("%Y-%m-%dT%H"),
        "end":    end.strftime("%Y-%m-%dT%H"),
        "sort[0][column]":    "period",
        "sort[0][direction]": "desc",
        "length": 500,
    }
    try:
        rows = _http_get(url, params=params).json().get("response", {}).get("data", [])
    except Exception as e:
        _log(f"  ✗ CAISO EIA-930: {e}")
        return None

    if not rows:
        return None

    # Group by hour
    by_hour: Dict[str, Dict[str, float]] = {}
    for row in rows:
        h, ft, v = row.get("period"), row.get("fueltype"), row.get("value")
        if None in (h, ft, v):
            continue
        by_hour.setdefault(h, {})[ft] = float(v)

    last_24 = sorted(by_hour.keys(), reverse=True)[:24]
    hydro_vals = [by_hour[h].get("WAT", 0.0) for h in last_24]
    total_vals = [sum(by_hour[h].values()) for h in last_24]

    avg_hydro_mw = round(sum(hydro_vals) / len(hydro_vals)) if hydro_vals else None
    avg_total_mw = sum(total_vals) / len(total_vals) if total_vals else None
    share_pct    = round(avg_hydro_mw / avg_total_mw * 100) if avg_hydro_mw and avg_total_mw else None

    _log(f"  ✓ CAISO hydro: {avg_hydro_mw} MW ({share_pct}% of grid)")
    return {
        "hydro_mw":        avg_hydro_mw,
        "hydro_share_pct": share_pct,
        "sp15_da":         None,   # SP15 DA price requires separate OASIS query; omit for now
        "history":         [],
        "as_of":           datetime.date.today().isoformat(),
    }


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

    archive = load_archive()

    output = {
        "meta": {
            "last_updated_pt": now.strftime("%Y-%m-%d %H:%M PT"),
            "next_update_pt":  (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M PT"),
            "sources_ok": [k for k, v in {
                "bpa_mix": bpa_mix, "midc": midc, "wsf": wsf,
                "caiso": caiso, "usace": bool(usace_rows),
                "basins": bool(basins),
            }.items() if v],
        },
        "basins":  basins,
        "wsf":     wsf or {},
        "midc":    midc or {},
        "bpa_mix": bpa_mix or {},
        "usace":   usace_rows,
        "caiso":   caiso or {},
        "archive": archive,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, indent=2, default=str))
    _log(f"━━━ wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes) ━━━")
    return 0


if __name__ == "__main__":
    sys.exit(main())
