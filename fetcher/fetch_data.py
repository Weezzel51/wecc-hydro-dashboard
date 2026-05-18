"""
WECC Hydro Market Brief — Daily Data Fetcher
─────────────────────────────────────────────
Runs daily at 06:00 PT via GitHub Actions. Pulls from primary public
sources, writes a single JSON file consumed by the static dashboard.

Design rules
────────────
1. Graceful degradation: any source can fail without killing the run.
   Failed sources log a warning and emit null/empty fields; dashboard
   shows "—" for missing values.
2. Primary sources only. No paywalled feeds. No scraping behind logins.
3. One JSON output. Frontend reads /data/dashboard.json — that's it.
4. Idempotent. Safe to re-run any time; output always reflects "now".

Environment
───────────
    EIA_API_KEY    Required for BPA mix + wholesale prices.
                   Free at https://www.eia.gov/opendata/register.php
                   Set as GitHub Actions secret.
"""

import os
import sys
import json
import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any

import requests

# ── Config ────────────────────────────────────────────────────────────
EIA_API_KEY = os.environ.get("EIA_API_KEY", "")
OUT_PATH    = Path(__file__).resolve().parent.parent / "data" / "dashboard.json"
ARCHIVE_PATH = Path(__file__).resolve().parent.parent / "data" / "archive.json"
PT          = datetime.timezone(datetime.timedelta(hours=-7))  # PDT/PST shift in workflow
HTTP_TIMEOUT = 20

# Basin roster. Each entry routes to a fetcher by `source`.
# `id` is source-specific (HUC, CDEC region code, BC RFC region key).
BASINS: List[Dict[str, str]] = [
    # ── BC Columbia headwaters (BC RFC) ──
    {"name": "Mica / Upper Columbia (BC)", "source": "bc",     "id": "UCOL"},
    {"name": "Kootenay (BC)",              "source": "bc",     "id": "KOOT"},
    # ── PNW (NRCS SNOTEL) ──
    {"name": "Upper Columbia (US)",        "source": "snotel", "id": "17020001"},
    {"name": "Pend Oreille",               "source": "snotel", "id": "17010216"},
    {"name": "Snake River",                "source": "snotel", "id": "17040201"},
    {"name": "Clearwater",                 "source": "snotel", "id": "17060306"},
    {"name": "Salmon",                     "source": "snotel", "id": "17060201"},
    {"name": "Yakima",                     "source": "snotel", "id": "17030003"},
    {"name": "Owyhee",                     "source": "snotel", "id": "17050106"},
    # ── CAISO / Sierra (CDEC) ──
    {"name": "Northern Sierra (Feather)",  "source": "cdec",   "id": "NSF"},
    {"name": "Central Sierra (American)",  "source": "cdec",   "id": "CSF"},
    {"name": "Southern Sierra (San Joaq.)","source": "cdec",   "id": "SSF"},
    {"name": "Tulare (Kings/Kern)",        "source": "cdec",   "id": "TLR"},
]

# USACE NWD projects (lower Columbia / Snake mainstem).
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
    """Run a fetcher; catch and log any exception; return None on failure."""
    try:
        return fn()
    except Exception as e:
        _log(f"  ✗ {label}: {type(e).__name__}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
#  TIER 1 — WORKING ENDPOINTS (EIA)
# ══════════════════════════════════════════════════════════════════════

def fetch_eia_bpa_mix() -> Optional[Dict[str, int]]:
    """
    EIA-930 BPA balancing authority hourly generation by fuel type.
    Averages the last 24h, rolls up to hydro / wind / thermal-and-other.

    Endpoint: https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/
    Docs:     https://www.eia.gov/opendata/documentation.php
    """
    if not EIA_API_KEY:
        _log("  ⚠ EIA_API_KEY unset — skipping BPA mix")
        return None

    end   = datetime.datetime.utcnow()
    start = end - datetime.timedelta(days=2)  # buffer for late posts
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
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    rows = r.json().get("response", {}).get("data", [])
    if not rows:
        return None

    # Group hours; take the 24 most-recent complete hours.
    by_hour: Dict[str, Dict[str, float]] = {}
    for row in rows:
        h  = row.get("period")
        ft = row.get("fueltype")
        v  = row.get("value")
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
    Scrape Mid-C peak wholesale spot price from EIA's Today in Energy
    daily prices page. Updates weekdays ~07:30-08:30 ET.

    Source: https://www.eia.gov/todayinenergy/prices.php

    Note: EIA's v2 API does not expose ICE hub prices, only via web pages
    and bulk XLS downloads. This scrape is the simplest working path.
    """
    from bs4 import BeautifulSoup

    url = "https://www.eia.gov/todayinenergy/prices.php"
    headers = {"User-Agent": "WECC-Hydro-Brief/1.0 (https://wecchydrobrief.com)"}
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers=headers)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    midc_price = None

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        row_text = " ".join(c.get_text(strip=True) for c in cells).lower()
        if "mid" in row_text and ("columbia" in row_text or "mid-c" in row_text or "mid c" in row_text):
            for cell in cells:
                txt = cell.get_text(strip=True).replace("$", "").replace(",", "").strip()
                try:
                    v = float(txt)
                    if 5 < v < 500:  # sanity: typical Mid-C $/MWh range
                        midc_price = v
                        break
                except ValueError:
                    continue
            if midc_price:
                break

    if midc_price is None:
        _log("  ⚠ Mid-C row not found in EIA Today in Energy table")
        return None

    _log(f"  ✓ Mid-C peak DA: ${midc_price:.2f}/MWh (EIA Today in Energy)")
    return {
        "peak_da":    round(midc_price, 2),
        "offpeak_da": None,
        "wow_pct":    None,
        "history":    [],  # 30-day history comes later via XLS bulk download
    }


def _fmt_short_date(iso: str) -> str:
    d = datetime.date.fromisoformat(iso[:10])
    return d.strftime("%b %-d") if sys.platform != "win32" else d.strftime("%b %#d")


# ══════════════════════════════════════════════════════════════════════
#  TIER 2 — STUBS (real endpoints documented; fill in incrementally)
# ══════════════════════════════════════════════════════════════════════

def fetch_snotel_basin(huc: str) -> Optional[Dict[str, Any]]:
    """
    NRCS Air & Water Database (AWDB) — basin-level SWE % of median.

    Endpoint: https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/
    Approach: Query basin-index station for HUC, element=WTEQ, duration=DAILY.
              Compute current value, % of median, and 7d delta.

    Return shape:
        {"swe_in": float, "pct_median": int, "delta_7d": int}
    """
    # TODO: implement once basin→station mapping is confirmed.
    # NRCS publishes a station catalog at wcc.sc.egov.usda.gov/reportGenerator.
    return None


def fetch_cdec_sierra(region_id: str) -> Optional[Dict[str, Any]]:
    """
    CA Data Exchange Center (CDEC) — Sierra snow region indices.

    Endpoint: https://cdec.water.ca.gov/dynamicapp/req/JSONDataServlet
    Sensors:  SWE = sensor 82 (snow water content, daily). Region keys:
              NSF (Northern), CSF (Central), SSF (Southern), TLR (Tulare).

    Return shape:
        {"swe_in": float, "pct_median": int, "delta_7d": int}
    """
    return None


def fetch_bc_snow(region_id: str) -> Optional[Dict[str, Any]]:
    """
    BC River Forecast Centre — automated snow weather station network.

    Source:   https://bcrfc.env.gov.bc.ca/data/asp/asplive.csv (live ASP data)
              https://bcrfc.env.gov.bc.ca/bulletins/ (monthly bulletins)
    Approach: Average % of normal across stations within UCOL or KOOT region.

    Return shape:
        {"swe_in": float, "pct_median": int, "delta_7d": int}
    """
    return None


def fetch_nwrfc_wsf() -> Optional[Dict[str, Any]]:
    """
    NWRFC Apr–Sep water supply forecast at The Dalles (key Columbia metric).

    Source:   https://www.nwrfc.noaa.gov/water_supply/ws_forecasts.php
    Approach: Scrape the forecast table; pull TDA, latest issue date, MAF, % of normal.

    Return shape:
        {"site": "The Dalles", "forecast_maf": float,
         "pct_normal": int, "delta_prior_maf": float}
    """
    return None


def fetch_usace_project(code: str) -> Optional[Dict[str, Any]]:
    """
    USACE NWD dataquery — project discharge and forebay.

    Source:   https://www.nwd-wc.usace.army.mil/dd/common/dataquery/www/
    Endpoint: ?query=[%22{code}.Discharge.Inst.5Minutes.0...%22]
    Approach: Latest 5-min discharge in kcfs + forebay elevation in ft.

    Return shape:
        {"discharge_kcfs": int, "forebay_ft": float}
    """
    return None


def fetch_caiso_hydro() -> Optional[Dict[str, Any]]:
    """
    CAISO OASIS — system-wide hydro dispatch + SP15 DA.

    Source:   http://oasis.caiso.com/oasisapi/SingleZip
    Queries:  SLD_FCST (load), ENE_SLRS (hydro dispatch), PRC_LMP (SP15).
    Note:     Returns zipped XML — parse with xml.etree.

    Return shape:
        {"hydro_mw": int, "hydro_share_pct": float,
         "sp15_da": float, "history": [{"h": "HH", "mw": int}]}
    """
    return None


def fetch_ferc_hydro_filings() -> List[Dict[str, str]]:
    """
    FERC eLibrary — recent hydro/PSH docket filings.

    Source:   https://elibrary.ferc.gov/eLibrary/search (RSS/Atom feed)
    Filter:   Subcategory = "Hydropower" OR "Pumped Storage", last 14 days.

    Return shape (list):
        [{"tag": "FERC"|"MARKET"|"OPS", "tag_class": "ferc"|"market"|"ops",
          "title": str, "date": "YYYY-MM-DD", "source": str}]
    """
    return []


def load_archive() -> List[Dict[str, str]]:
    """
    Brief archive is hand-maintained in /data/archive.json so you control
    what surfaces. Falls back to empty list if file missing.
    """
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
            _log(f"  ✓ {b['name']}: {data.get('pct_median')}% of median")

    # Tier-1 metrics
    _log("Markets:")
    bpa_mix = _safe(fetch_eia_bpa_mix,   "EIA-930 BPA")
    midc    = _safe(fetch_eia_midc_price, "EIA Mid-C")
    wsf     = _safe(fetch_nwrfc_wsf,      "NWRFC WSF")

    # Tier-2 metrics
    _log("Ops:")
    usace_rows: List[Dict[str, Any]] = []
    for p in USACE_PROJECTS:
        data = _safe(lambda: fetch_usace_project(p["code"]), f"USACE {p['code']}")
        if data:
            usace_rows.append({"project": p["name"], **data})

    caiso   = _safe(fetch_caiso_hydro,         "CAISO OASIS")
    reg     = _safe(fetch_ferc_hydro_filings,  "FERC eLibrary") or []

    archive = load_archive()

    # Assemble
    output = {
        "meta": {
            "last_updated_pt": now.strftime("%Y-%m-%d %H:%M PT"),
            "next_update_pt":  (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M PT"),
            "sources_ok": [k for k, v in {
                "bpa_mix": bpa_mix, "midc": midc, "wsf": wsf,
                "caiso": caiso, "usace": bool(usace_rows),
                "basins": bool(basins), "ferc": bool(reg),
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
