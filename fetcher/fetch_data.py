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
    EIA Wholesale Market Prices — Mid-Columbia peak DA price.
    Discovers the correct location code via the facets endpoint first,
    then pulls 30 days of peak prices.
    """
    if not EIA_API_KEY:
        _log("  ⚠ EIA_API_KEY unset — skipping Mid-C price")
        return None

    base = "https://api.eia.gov/v2/electricity/wholesale-market-prices"
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=45)).isoformat()

    # Step 1: discover the correct location code (changes occasionally)
    mid_loc = None
    try:
        facets = _http_get(f"{base}/facet/location",
                           params={"api_key": EIA_API_KEY}).json()
        locs = facets.get("response", {}).get("facets", [])
        # Find the one that looks like Mid-Columbia
        for item in locs:
            name = str(item.get("name", "") or item.get("id", "")).lower()
            if "mid" in name and ("columbia" in name or "col" in name or name in ("mid-c", "midc")):
                mid_loc = item.get("id") or item.get("name")
                break
        if not mid_loc and locs:
            _log(f"  · EIA locations available: {[i.get('id') for i in locs[:10]]}")
    except Exception as e:
        _log(f"  · EIA facets: {e}")

    # Step 2: if discovery failed, try known codes
    candidates = ([mid_loc] if mid_loc else []) + ["Mid-C", "MID-C", "MIDC", "Mid Columbia", "MIC"]

    rows = []
    used_loc = None
    for loc in candidates:
        if not loc:
            continue
        try:
            resp = _http_get(f"{base}/data/", params={
                "api_key":             EIA_API_KEY,
                "facets[location][]":  loc,
                "facets[type][]":      "peak",
                "start":               start,
                "end":                 today.isoformat(),
                "sort[0][column]":     "period",
                "sort[0][direction]":  "desc",
                "length":              60,
            }).json()
            rows = resp.get("response", {}).get("data", [])
        except Exception:
            rows = []
        if rows:
            used_loc = loc
            break

    if not rows:
        _log("  ✗ EIA Mid-C price: no data for any location code")
        return None

    series: List[tuple] = []
    for row in rows:
        try:
            d = datetime.date.fromisoformat(str(row.get("period", ""))[:10])
            # Field name varies: "price", "value", "dollars-per-megawatthour"
            p = None
            for key in ("price", "value", "dollars-per-megawatthour", "Price"):
                if row.get(key) is not None:
                    p = float(row[key])
                    break
            if p and p > 0:
                series.append((d, p))
        except (ValueError, TypeError):
            continue

    if not series:
        _log("  ✗ EIA Mid-C price: no parseable values")
        return None

    series.sort()
    latest_date, latest_price = series[-1]
    week_ago = latest_date - datetime.timedelta(days=7)
    prior    = next((p for d, p in reversed(series[:-1]) if d <= week_ago), None)
    wow_pct  = round((latest_price - prior) / prior * 100, 1) if prior else None
    history  = [{"d": _fmt_short_date(d.isoformat()), "p": round(p, 2)} for d, p in series[-30:]]

    _log(f"  ✓ Mid-C peak DA: ${latest_price:.2f}/MWh [{used_loc}] ({latest_date.isoformat()})")
    return {
        "peak_da": round(latest_price, 2), "offpeak_da": None,
        "wow_pct": wow_pct, "history": history, "as_of": latest_date.isoformat(),
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
    base     = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"

    # 1. Station list for this HUC-6
    try:
        stations = _http_get(f"{base}/stations", params={
            "hucs": huc, "networkCds": "SNTL", "activeStationsOnly": "true",
        }).json()
    except Exception as e:
        _log(f"    ✗ SNOTEL {huc} stations: {e}")
        return None

    if not stations:
        _log(f"    ⚠ SNOTEL {huc}: no stations")
        return None

    triplets = [s["stationTriplet"] for s in stations]
    # Split into chunks of 30 to stay under URL length limit
    def chunks(lst, n=30):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    # 2. Current WTEQ (snow water equivalent, inches)
    def get_wteq(date_str):
        vals = {}
        for chunk in chunks(triplets):
            try:
                rows = _http_get(f"{base}/data", params={
                    "stationTriplets": ",".join(chunk),
                    "elementCd": "WTEQ",
                    "beginDate": date_str,
                    "endDate":   date_str,
                    "duration":  "DAILY",
                    "getFlags":  "false",
                }).json()
                for row in (rows if isinstance(rows, list) else []):
                    t = row.get("stationTriplet", "")
                    v_list = row.get("values") or []
                    if v_list:
                        v = v_list[0].get("value")
                        if v is not None and str(v) not in ("", "-999999"):
                            try:
                                vals[t] = float(v)
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass
        return vals

    cur = get_wteq(today.isoformat())
    prv = get_wteq(week_ago.isoformat())

    # If all stations are 0.0 (melted out), that's valid — 0% of median
    # If none returned any value, try returning 0 rather than None for summer
    if not cur:
        _log(f"    ⚠ SNOTEL {huc}: no current WTEQ values — late season 0")
        return {"pct_median": 0, "swe_in": 0.0, "delta_7d": None, "as_of": today.isoformat()}

    # 3. Normals via AWDB /normals endpoint
    normals = {}
    for chunk in chunks(list(cur.keys())):
        try:
            nrows = _http_get(f"{base}/normals", params={
                "stationTriplets": ",".join(chunk),
                "elementCd":       "WTEQ",
                "durationCd":      "DAILY",
                "beginMonthDay":   today.strftime("%m-%d"),
                "endMonthDay":     today.strftime("%m-%d"),
            }).json()
            for row in (nrows if isinstance(nrows, list) else []):
                t = row.get("stationTriplet", "")
                v_list = row.get("values") or []
                if v_list:
                    v = v_list[0].get("value")
                    if v is not None:
                        try:
                            normals[t] = float(v)
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

    # 4. Compute % of median
    pct_list, swe_list = [], []
    for t, obs in cur.items():
        swe_list.append(obs)
        med = normals.get(t)
        if med and med > 0:
            pct_list.append(obs / med * 100)
        else:
            pct_list.append(0.0)   # melted out → 0%

    pct_median = round(sum(pct_list) / len(pct_list)) if pct_list else 0
    mean_swe   = round(sum(swe_list)  / len(swe_list), 1) if swe_list else 0.0

    # 7-day delta
    delta_7d = None
    if prv and normals:
        prv_pcts = []
        for t, obs in prv.items():
            med = normals.get(t)
            prv_pcts.append(obs / med * 100 if (med and med > 0) else 0.0)
        if prv_pcts and pct_list:
            delta_7d = round(pct_median - sum(prv_pcts) / len(prv_pcts), 1)

    _log(f"    ✓ SNOTEL {huc}: {mean_swe}\" / {pct_median}% of median ({len(cur)} stations)")
    return {"pct_median": pct_median, "swe_in": mean_swe, "delta_7d": delta_7d, "as_of": today.isoformat()}


def fetch_cdec_sierra(region_id: str) -> Optional[Dict[str, Any]]:
    """
    Sierra SWE via NRCS AWDB REST API, using HUC-8 sub-basin codes.
    Same implementation as fetch_snotel_basin but with HUC-8 codes.
    """
    huc_map = {
        "NSF": "180201",   # Northern Sierra — use HUC-6 (more stations)
        "CSF": "180202",
        "SSF": "180400",
        "TLR": "180300",
    }
    huc = huc_map.get(region_id)
    if not huc:
        return None
    # Re-use the SNOTEL basin fetcher with the HUC-6 code
    return fetch_snotel_basin(huc)


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
    NWRFC Apr–Sep water supply forecast at The Dalles.

    NWRFC publishes water supply outlook data at:
    https://www.nwrfc.noaa.gov/water_supply/ws_forecasts.php (HTML — fragile)

    More reliable: NWRFC's ensemble forecast summary CSV, or scraping
    the published PDF. Simplest machine-readable source: the NWRFC
    Water Supply Summary page, which has consistent HTML table structure.

    Fallback: use the NOAA Water Prediction Service API.
    """
    today = datetime.date.today()

    # Try NWRFC water supply summary page (HTML table, parse with regex)
    urls_to_try = [
        "https://www.nwrfc.noaa.gov/water_supply/ws_forecasts.php?id=TDAO3",
        "https://www.nwrfc.noaa.gov/water_supply/ws_forecasts.php?loc=TDAO3",
    ]

    text = None
    for url in urls_to_try:
        try:
            r = _http_get(url)
            if r.status_code == 200:
                text = r.text
                break
        except Exception:
            pass

    if text:
        # Look for a % of normal value in the page HTML
        # Common patterns: "88%" "88 %" "88%MAF" near "Apr" or "volume"
        m = re.search(r"(?:Apr|April)[\s\S]{0,200}?(\d{2,3})\s*%", text, re.IGNORECASE)
        if not m:
            # Broader scan
            m = re.search(r"(\d{2,3})\s*%\s*(?:of\s*)?(?:normal|median|average)", text, re.IGNORECASE)
        if m:
            pct = int(m.group(1))
            # Try to find MAF value nearby
            maf_m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*MAF", text, re.IGNORECASE)
            maf = float(maf_m.group(1)) if maf_m else None
            _log(f"  ✓ NWRFC WSF: {pct}% of normal ({maf} MAF)")
            return {"site": "The Dalles", "pct_normal": pct,
                    "forecast_maf": maf, "as_of": today.isoformat()}

    # Fallback: NOAA NWPS API (National Water Prediction Service)
    # This provides streamflow data but not seasonal volume forecasts directly.
    # Skip and return None — WSF panel will show "pending".
    _log("  ✗ NWRFC WSF: no parseable forecast found")
    return None


# USGS daily-values gauge IDs for Columbia/Snake projects.
# Using /nwis/dv/ (daily values) — more universally available than /iv/.
# Gauges are on the river immediately below each dam.
USGS_GAUGE_MAP = {
    "GCL": ("12440900", "Grand Coulee"),   # Columbia R below Grand Coulee Dam WA
    "CHJ": ("12443700", "Chief Joseph"),   # Columbia R below Chief Joseph Dam WA
    "LWG": ("13340600", "Lower Granite"),  # Snake R at Lower Granite Dam WA
    "TDA": ("14105700", "The Dalles"),     # Columbia R at The Dalles OR
    "BON": ("14128910", "Bonneville"),     # Columbia R at Bonneville OR
}

def fetch_usace_project(code: str) -> Optional[Dict[str, Any]]:
    """
    USGS NWIS daily values — discharge (cfs→kcfs) and gage height (ft)
    for major Columbia/Snake River projects.

    Uses /nwis/dv/ (daily mean) which is available for all gauges.
    Parameter 00060 = mean daily discharge (cfs).
    Parameter 00065 = mean daily gage height (ft) — used as forebay proxy.
    """
    if code not in USGS_GAUGE_MAP:
        return None
    gauge_id, label = USGS_GAUGE_MAP[code]

    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=3)  # buffer for data lag

    url = "https://waterservices.usgs.gov/nwis/dv/"
    params = {
        "sites":       gauge_id,
        "parameterCd": "00060,00065",
        "startDT":     yesterday.isoformat(),
        "endDT":       today.isoformat(),
        "siteStatus":  "all",
        "format":      "json",
    }
    try:
        data = _http_get(url, params=params).json()
    except Exception as e:
        _log(f"  ✗ USGS {code} ({gauge_id}): {e}")
        return None

    discharge_kcfs = None
    forebay_ft     = None

    try:
        ts_list = data["value"]["timeSeries"]
        for ts in ts_list:
            var_code = ts["variable"]["variableCode"][0]["value"]
            vals     = ts["values"][0]["value"]
            # Walk newest-first for a valid (non-sentinel) value
            for v in reversed(vals):
                s = str(v.get("value", ""))
                if s and s not in ("", "-999999", "Ice"):
                    try:
                        fv = float(s)
                    except ValueError:
                        continue
                    if var_code == "00060":
                        discharge_kcfs = round(fv / 1000, 1)  # cfs → kcfs
                    elif var_code == "00065":
                        forebay_ft = round(fv, 1)
                    break
    except (KeyError, IndexError, TypeError) as e:
        _log(f"  ✗ USGS {code}: parse error: {e}")
        return None

    if discharge_kcfs is None and forebay_ft is None:
        _log(f"  ✗ USGS {code} ({gauge_id}): no usable values")
        return None

    _log(f"  ✓ USGS {code}: {discharge_kcfs} kcfs / {forebay_ft} ft")
    return {
        "discharge_kcfs": discharge_kcfs,
        "forebay_ft":     forebay_ft,
        "as_of":          today.isoformat(),
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
