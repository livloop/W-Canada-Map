#!/usr/bin/env python3
"""
build_data.py — Prepare data for a geographically accurate topographic map of Canada.

Outputs into ./data:
  - relief.png        Hillshaded + hypsometrically tinted elevation raster (equirectangular)
  - relief.json       Geographic bounds + size metadata for the raster
  - provinces.json    Canada provinces / territories (GeoJSON)
  - lakes.json        Major lakes intersecting Canada (GeoJSON)
  - rivers.json       Major rivers intersecting Canada (GeoJSON)
  - cities.json       Notable Canadian populated places (GeoJSON)

Also vendors d3 into ./vendor so the site works fully offline.

Sources:
  - Elevation: AWS "Terrain Tiles" (Terrarium RGB encoding), Mapzen/Tilezen, public domain mix.
  - Vectors:   Natural Earth via nvkelso/natural-earth-vector (public domain).
"""

import io
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
VENDOR = os.path.join(HERE, "vendor")
os.makedirs(DATA, exist_ok=True)
os.makedirs(VENDOR, exist_ok=True)

# Geographic bounding box for Canada (with a little margin), [W, E, S, N]
WEST, EAST, SOUTH, NORTH = -141.5, -51.0, 41.0, 83.6

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "canada-topo-builder/1.0"})


# --------------------------------------------------------------------------- #
# Web-Mercator tile math
# --------------------------------------------------------------------------- #
def lon2tilex(lon, z):
    return (lon + 180.0) / 360.0 * (2 ** z)


def lat2tiley(lat, z):
    r = math.radians(lat)
    return (1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * (2 ** z)


def tilex2lon(x, z):
    return x / (2 ** z) * 360.0 - 180.0


def tiley2lat(y, z):
    n = math.pi - 2.0 * math.pi * y / (2 ** z)
    return math.degrees(math.atan(math.sinh(n)))


def mercator_ynorm(lat):
    """Normalized [0,1] Web-Mercator Y for a latitude (0=north pole-ish, 1=south)."""
    r = math.radians(lat)
    return (1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0


# --------------------------------------------------------------------------- #
# 1. Elevation: fetch + mosaic Terrarium terrain tiles
# --------------------------------------------------------------------------- #
TERRAIN_URL = "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png"


def fetch_tile(z, x, y):
    url = TERRAIN_URL.format(z=z, x=x, y=y)
    for attempt in range(4):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 200:
                return x, y, np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
            return x, y, None
        except Exception:
            if attempt == 3:
                return x, y, None
    return x, y, None


def build_elevation(zoom=5):
    x0 = int(math.floor(lon2tilex(WEST, zoom)))
    x1 = int(math.floor(lon2tilex(EAST, zoom)))
    y0 = int(math.floor(lat2tiley(NORTH, zoom)))
    y1 = int(math.floor(lat2tiley(SOUTH, zoom)))
    xs = list(range(x0, x1 + 1))
    ys = list(range(y0, y1 + 1))
    print(f"[elev] zoom {zoom}: x {x0}..{x1} ({len(xs)}), y {y0}..{y1} ({len(ys)}) = {len(xs)*len(ys)} tiles")

    ts = 256
    mosaic = np.zeros((len(ys) * ts, len(xs) * ts, 3), dtype=np.uint8)

    jobs = [(zoom, x, y) for x in xs for y in ys]
    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for x, y, arr in ex.map(lambda a: fetch_tile(*a), jobs):
            done += 1
            if arr is None:
                continue
            r = (y - y0) * ts
            c = (x - x0) * ts
            mosaic[r:r + ts, c:c + ts] = arr
            if done % 20 == 0:
                print(f"[elev] {done}/{len(jobs)} tiles")

    # Decode Terrarium RGB -> meters
    m = mosaic.astype(np.float64)
    elev = m[:, :, 0] * 256.0 + m[:, :, 1] + m[:, :, 2] / 256.0 - 32768.0

    # Geographic extent of the mosaic (Web-Mercator aligned tile edges)
    mlon_w = tilex2lon(x0, zoom)
    mlon_e = tilex2lon(x1 + 1, zoom)
    mlat_n = tiley2lat(y0, zoom)
    mlat_s = tiley2lat(y1 + 1, zoom)
    return elev, (mlon_w, mlon_e, mlat_s, mlat_n)


def resample_to_equirect(elev, extent, out_w=2000):
    """Resample a Web-Mercator elevation grid onto a regular lon/lat grid over the Canada bbox."""
    mlon_w, mlon_e, mlat_s, mlat_n = extent
    mh, mw = elev.shape

    out_h = int(round(out_w * (NORTH - SOUTH) / (EAST - WEST)))
    lons = np.linspace(WEST, EAST, out_w)
    lats = np.linspace(NORTH, SOUTH, out_h)  # top row = north

    # Column index (linear in lon for Mercator)
    cols = (lons - mlon_w) / (mlon_e - mlon_w) * mw
    cols = np.clip(cols, 0, mw - 1).astype(np.int32)

    # Row index (non-linear: via normalized mercator Y)
    yn_top = mercator_ynorm(mlat_n)
    yn_bot = mercator_ynorm(mlat_s)
    yn = np.array([mercator_ynorm(l) for l in lats])
    rows = (yn - yn_top) / (yn_bot - yn_top) * mh
    rows = np.clip(rows, 0, mh - 1).astype(np.int32)

    grid = elev[np.ix_(rows, cols)]
    return grid, (out_w, out_h)


# --------------------------------------------------------------------------- #
# 2. Hillshade + hypsometric tint -> relief PNG
# --------------------------------------------------------------------------- #
def hillshade(elev, dx_m, dy_m, azimuth=315.0, altitude=45.0, z_factor=1.0):
    gy, gx = np.gradient(elev * z_factor, dy_m, dx_m)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    shaded = (np.sin(alt) * np.sin(slope) +
              np.cos(alt) * np.cos(slope) * np.cos(az - aspect))
    return np.clip(shaded, 0, 1)


# Hypsometric colour ramp (land), elevation in metres -> RGB
LAND_STOPS = [
    (0,    (96, 150, 96)),
    (150,  (140, 178, 99)),
    (350,  (190, 199, 110)),
    (700,  (224, 211, 130)),
    (1200, (214, 178, 120)),
    (2000, (183, 138, 95)),
    (3000, (158, 120, 100)),
    (4200, (245, 245, 245)),
    (5500, (255, 255, 255)),
]
OCEAN_DEEP = np.array([60, 96, 140])
OCEAN_SHALLOW = np.array([110, 152, 188])


def hypsometric(elev):
    h, w = elev.shape
    rgb = np.zeros((h, w, 3), dtype=np.float64)

    land = elev > 0
    # Land tint via piecewise-linear interpolation across stops
    es = np.array([s[0] for s in LAND_STOPS])
    cs = np.array([s[1] for s in LAND_STOPS], dtype=np.float64)
    e = np.clip(elev, 0, es[-1])
    for ch in range(3):
        rgb[:, :, ch] = np.interp(e, es, cs[:, ch])

    # Ocean / inland-below-zero: depth shading from shallow to deep
    depth = np.clip(-elev, 0, 4000) / 4000.0
    ocean = (OCEAN_SHALLOW[None, None, :] * (1 - depth[:, :, None]) +
             OCEAN_DEEP[None, None, :] * depth[:, :, None])
    water = ~land
    for ch in range(3):
        rgb[:, :, ch] = np.where(water, ocean[:, :, ch], rgb[:, :, ch])

    return rgb, land


def build_relief(elev, size):
    out_w, out_h = size
    # Approx metres per pixel for hillshade scaling
    mid_lat = (NORTH + SOUTH) / 2.0
    dx_m = (EAST - WEST) / out_w * 111320.0 * math.cos(math.radians(mid_lat))
    dy_m = (NORTH - SOUTH) / out_h * 110540.0

    shade = hillshade(elev, dx_m, dy_m, z_factor=1.0)
    # Soften so flat areas aren't pure mid-grey
    shade = 0.55 + 0.65 * (shade - 0.5)
    shade = np.clip(shade, 0.25, 1.15)

    tint, land = hypsometric(elev)
    rgb = tint * shade[:, :, None]
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    out = np.dstack([rgb, np.full((out_h, out_w), 255, dtype=np.uint8)])
    Image.fromarray(out, "RGBA").save(os.path.join(DATA, "relief.png"))

    with open(os.path.join(DATA, "relief.json"), "w") as f:
        json.dump({"west": WEST, "east": EAST, "south": SOUTH, "north": NORTH,
                   "width": out_w, "height": out_h}, f)
    print(f"[relief] wrote relief.png ({out_w}x{out_h})")


# --------------------------------------------------------------------------- #
# 3. Natural Earth vector data
# --------------------------------------------------------------------------- #
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}"


def get_geojson(name):
    print(f"[vec] fetching {name}")
    r = SESSION.get(NE.format(name), timeout=120)
    r.raise_for_status()
    return r.json()


def bbox_of(geom):
    xs, ys = [], []

    def walk(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)

    walk(geom.get("coordinates"))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def intersects_canada(geom, pad=2.0):
    b = bbox_of(geom)
    if not b:
        return False
    return not (b[2] < WEST - pad or b[0] > EAST + pad or
                b[3] < SOUTH - pad or b[1] > NORTH + pad)


def save(name, fc):
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(fc, f)
    print(f"[vec] wrote {name} ({len(fc['features'])} features)")


def build_vectors():
    # Provinces & territories
    states = get_geojson("ne_50m_admin_1_states_provinces.geojson")
    prov = [f for f in states["features"]
            if (f["properties"].get("admin") == "Canada")]
    save("provinces.json", {"type": "FeatureCollection", "features": prov})

    # Lakes (major)
    lakes = get_geojson("ne_50m_lakes.geojson")
    lk = [f for f in lakes["features"] if intersects_canada(f["geometry"])]
    save("lakes.json", {"type": "FeatureCollection", "features": lk})

    # Rivers
    rivers = get_geojson("ne_50m_rivers_lake_centerlines.geojson")
    rv = [f for f in rivers["features"] if intersects_canada(f["geometry"], pad=0.5)]
    save("rivers.json", {"type": "FeatureCollection", "features": rv})

    # Cities — Canadian populated places, keep the notable ones
    cities = get_geojson("ne_50m_populated_places.geojson")
    cc = []
    for f in cities["features"]:
        p = f["properties"]
        if p.get("ADM0NAME") != "Canada":
            continue
        cc.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": {
                "name": p.get("NAME"),
                "rank": p.get("SCALERANK", 10),
                "pop": p.get("POP_MAX", 0),
                "capital": int(p.get("FEATURECLA", "").startswith("Admin")),
            },
        })
    cc.sort(key=lambda f: (f["properties"]["rank"], -f["properties"]["pop"]))
    save("cities.json", {"type": "FeatureCollection", "features": cc[:45]})


# --------------------------------------------------------------------------- #
# 4. Vendor d3 (offline support)
# --------------------------------------------------------------------------- #
def vendor_libs():
    """Vendor d3 locally so the site works offline. Pulls the dist bundle from
    the npm registry tarball (public CDNs are often firewalled)."""
    dest = os.path.join(VENDOR, "d3.v7.min.js")
    if os.path.exists(dest) and os.path.getsize(dest) > 100000:
        print("[vendor] d3 already present")
        return
    import tarfile
    print("[vendor] fetching d3 from npm registry")
    meta = SESSION.get("https://registry.npmjs.org/d3", timeout=60).json()
    ver = meta["dist-tags"]["latest"]
    tgz_url = meta["versions"][ver]["dist"]["tarball"]
    tgz = SESSION.get(tgz_url, timeout=120).content
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as t:
        member = t.extractfile("package/dist/d3.min.js")
        with open(dest, "wb") as f:
            f.write(member.read())
    print(f"[vendor] wrote d3.v7.min.js (d3 {ver})")


# --------------------------------------------------------------------------- #
def main():
    zoom = int(os.environ.get("TOPO_ZOOM", "5"))
    vendor_libs()
    elev, extent = build_elevation(zoom=zoom)
    grid, size = resample_to_equirect(elev, extent, out_w=2000)
    build_relief(grid, size)
    build_vectors()
    print("\nDone. Start the site with:  python -m http.server 8000")


if __name__ == "__main__":
    main()
