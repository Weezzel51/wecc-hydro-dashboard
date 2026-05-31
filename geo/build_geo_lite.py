"""
WECC Hydro Dashboard — Lightweight geographic data builder
──────────────────────────────────────────────────────────
Replaces build_geodata.py for cases where geopandas/rasterio aren't
available. Requires ONLY: requests (pip install requests)

What it generates:
  data/geo/basins.geojson   — 17 WECC drainage basins (HUC-6 via USGS REST)
  data/geo/rivers.geojson   — Major rivers (Natural Earth via GitHub)
  data/geo/dams.geojson     — 15 major dams (hardcoded, already written)

Hillshade is skipped — the dashboard degrades gracefully without it.

Usage:
    pip install requests
    python geo/build_geo_lite.py
    git add data/geo/ && git commit -m "geo: rebuild static map data"
"""

import json
import sys
import time
import zipfile
import io
from pathlib import Path

import requests

OUT_DIR  = Path(__file__).resolve().parent.parent / "data" / "geo"
OUT_DIR.mkdir(parents=True, exist_ok=True)

UA      = "WECC-Hydro-Brief/1.0 (https://wecchydrobrief.com)"
TIMEOUT = 60

# WECC bounding box [west, south, east, north]
WECC_BBOX = (-125.0, 33.0, -104.0, 53.0)

# Map dashboard basin names → HUC-6 code(s) to dissolve
HUC6_MAP = {
    "Upper Columbia (US)":          ["170200"],
    "Pend Oreille":                 ["170102"],
    "Yakima":                       ["170300"],
    "Clearwater":                   ["170603"],
    "Salmon":                       ["170602"],
    "Upper Snake":                  ["170401", "170402"],
    "Lower Snake":                  ["170601"],
    "Owyhee":                       ["170501"],
    "Lower Columbia":               ["170800"],
    "Northern Sierra (Feather)":    ["180201"],
    "Central Sierra (American)":    ["180202"],
    "Southern Sierra (San Joaq.)":  ["180400"],
    "Tulare (Kings/Kern)":          ["180300"],
    "Upper Colorado":               ["140100", "140200"],
    "Lower Colorado":               ["150100", "150200"],
}

# BC basins: hand-coded approximate polygons (no USGS coverage north of border)
BC_BASINS = {
    "Mica / Upper Columbia (BC)": [
        [-118.7,52.6],[-117.0,52.4],[-116.0,51.5],[-115.8,50.6],
        [-117.0,49.5],[-118.0,49.2],[-118.8,49.8],[-119.2,50.8],
        [-119.0,51.8],[-118.7,52.6]
    ],
    "Kootenay (BC)": [
        [-116.0,51.5],[-114.5,51.0],[-114.2,49.5],[-114.5,49.0],
        [-115.5,49.0],[-116.5,49.4],[-117.0,49.5],[-115.8,50.6],
        [-116.0,51.5]
    ],
}


def get(url, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    r = requests.get(url, headers=headers, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


def log(msg):
    print(msg, flush=True)


# ── 1. Basins ──────────────────────────────────────────────────────────

def fetch_huc6_feature(huc_codes):
    """
    Query USGS WBD REST service for one or more HUC-6 codes.
    Returns a list of geometry dicts (GeoJSON Polygon/MultiPolygon).
    """
    # USGS WBD MapServer layer 4 = HUC6
    url = "https://hydroweb.nationalmap.gov/wbd/rest/services/wbd/MapServer/4/query"
    huc_list = ",".join(f"'{h}'" for h in huc_codes)
    params = {
        "where": f"huc6 IN ({huc_list})",
        "outFields": "huc6,name",
        "f": "geojson",
        "outSR": "4326",
        "geometryPrecision": 4,   # ~10km precision — plenty for dashboard
        "maxAllowableOffset": 0.05,  # simplify ~5km
    }
    r = get(url, params=params)
    data = r.json()
    features = data.get("features", [])
    return [f["geometry"] for f in features if f.get("geometry")]


def dissolve_geometries(geoms):
    """
    Merge a list of Polygon/MultiPolygon geometry dicts into one MultiPolygon.
    Pure Python — no shapely.
    """
    all_polys = []
    for g in geoms:
        if g["type"] == "Polygon":
            all_polys.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            all_polys.extend(g["coordinates"])
    if len(all_polys) == 1:
        return {"type": "Polygon", "coordinates": all_polys[0]}
    return {"type": "MultiPolygon", "coordinates": all_polys}


def build_basins_geojson():
    log("━━ Building basins.geojson ━━")
    features = []

    for basin_name, huc_codes in HUC6_MAP.items():
        log(f"  fetching {basin_name} ({', '.join(huc_codes)})…")
        try:
            geoms = fetch_huc6_feature(huc_codes)
            if not geoms:
                log(f"  ⚠ {basin_name}: no features returned, skipping")
                continue
            geom = dissolve_geometries(geoms)
            features.append({
                "type": "Feature",
                "properties": {"name": basin_name, "source": "USGS WBD HUC-6"},
                "geometry": geom
            })
            log(f"  ✓ {basin_name}")
        except Exception as e:
            log(f"  ✗ {basin_name}: {e}")
        time.sleep(0.3)  # be polite to USGS

    # BC basins (hand-coded)
    for basin_name, ring in BC_BASINS.items():
        features.append({
            "type": "Feature",
            "properties": {"name": basin_name, "source": "BC RFC (approximate)"},
            "geometry": {"type": "Polygon", "coordinates": [ring]}
        })
        log(f"  ✓ {basin_name} (BC approximation)")

    out = {"type": "FeatureCollection", "features": features}
    out_path = OUT_DIR / "basins.geojson"
    out_path.write_text(json.dumps(out))
    log(f"  wrote {out_path} ({out_path.stat().st_size // 1024} KB, {len(features)} features)")


# ── 2. Rivers ─────────────────────────────────────────────────────────

def build_rivers_geojson():
    log("━━ Building rivers.geojson ━━")

    # Natural Earth 10m rivers — GeoJSON hosted on GitHub
    url = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_rivers_lake_centerlines.geojson"
    log(f"  fetching Natural Earth rivers…")
    try:
        r = get(url)
        data = r.json()
    except Exception as e:
        log(f"  ✗ GitHub fetch failed ({e}), trying zip fallback…")
        # Fallback: NaturalEarthData CDN zip
        zip_url = "https://naciscdn.org/naturalearth/10m/physical/ne_10m_rivers_lake_centerlines.zip"
        r = get(zip_url, stream=True)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        # find the .geojson inside (there may not be one — it's usually shapefiles)
        # In that case, fall back to an empty rivers file
        names = z.namelist()
        geojson_names = [n for n in names if n.endswith(".geojson")]
        if geojson_names:
            data = json.loads(z.read(geojson_names[0]))
        else:
            log("  ⚠ no GeoJSON in zip — writing empty rivers file")
            data = {"type": "FeatureCollection", "features": []}

    # Filter to WECC bbox and major rivers only (scalerank ≤ 7)
    west, south, east, north = WECC_BBOX
    filtered = []
    for f in data.get("features", []):
        props = f.get("properties", {})
        sr = props.get("scalerank", 99)
        if sr > 7:
            continue
        geom = f.get("geometry", {})
        if not geom:
            continue
        # Quick bbox check on coordinates
        coords = geom.get("coordinates", [])
        if not coords:
            continue
        # For LineString, coords is [[lon,lat],...]; for MultiLineString [[...]]
        if geom["type"] == "LineString":
            sample = coords
        elif geom["type"] == "MultiLineString":
            sample = [pt for seg in coords for pt in seg]
        else:
            continue
        # At least one point must be within WECC bbox
        in_bbox = any(west <= pt[0] <= east and south <= pt[1] <= north for pt in sample)
        if not in_bbox:
            continue
        filtered.append({
            "type": "Feature",
            "properties": {
                "name": props.get("name") or props.get("name_en") or "",
                "scalerank": sr,
            },
            "geometry": geom
        })

    out = {"type": "FeatureCollection", "features": filtered}
    out_path = OUT_DIR / "rivers.geojson"
    out_path.write_text(json.dumps(out))
    log(f"  wrote {out_path} ({out_path.stat().st_size // 1024} KB, {len(filtered)} features)")


# ── 3. Dams ────────────────────────────────────────────────────────────

def write_dams_geojson():
    out_path = OUT_DIR / "dams.geojson"
    if out_path.exists():
        log("━━ dams.geojson already exists — skipping ━━")
        return
    log("━━ Writing dams.geojson ━━")
    dams = [
        ("Mica",          -118.566, 52.083, "BC Hydro · 1,805 MW",  "left"),
        ("Revelstoke",    -118.196, 51.049, "BC Hydro · 2,480 MW",  "right"),
        ("Grand Coulee",  -118.982, 47.957, "USBR · 6,809 MW",       "right"),
        ("Chief Joseph",  -119.638, 47.997, "USACE · 2,620 MW",      "left"),
        ("Dworshak",      -116.301, 46.516, "USACE · 465 MW",         "right"),
        ("Lower Granite", -117.428, 46.660, "USACE · 932 MW",         "right"),
        ("Ice Harbor",    -118.881, 46.249, "USACE · 693 MW",         "right"),
        ("McNary",        -119.298, 45.935, "USACE · 1,127 MW",       "right"),
        ("John Day",      -120.693, 45.715, "USACE · 2,160 MW",       "left"),
        ("The Dalles",    -121.135, 45.614, "USACE · 2,160 MW",       "left"),
        ("Bonneville",    -121.940, 45.644, "USACE · 1,212 MW",       "left"),
        ("Shasta",        -122.418, 40.720, "USBR · 710 MW",          "left"),
        ("Oroville",      -121.486, 39.541, "DWR · 819 MW",           "left"),
        ("Glen Canyon",   -111.484, 36.937, "USBR · 1,320 MW",        "right"),
        ("Hoover",        -114.737, 36.016, "USBR · 2,080 MW",        "left"),
    ]
    features = [
        {
            "type": "Feature",
            "properties": {"name": name, "meta": meta, "anchor": anchor},
            "geometry": {"type": "Point", "coordinates": [lon, lat]}
        }
        for name, lon, lat, meta, anchor in dams
    ]
    out = {"type": "FeatureCollection", "features": features}
    out_path.write_text(json.dumps(out, indent=2))
    log(f"  wrote {out_path}")


# ── Main ───────────────────────────────────────────────────────────────

def main():
    log("━━━━━━━━ WECC geo data build (lite) ━━━━━━━━")
    log(f"Output: {OUT_DIR}")
    log("")
    write_dams_geojson()
    build_basins_geojson()
    build_rivers_geojson()
    log("")
    log("━━━━━━━━ DONE ━━━━━━━━")
    log("Now run:")
    log("  git add data/geo/ && git commit -m 'geo: build static map data' && git push")


if __name__ == "__main__":
    sys.exit(main() or 0)
