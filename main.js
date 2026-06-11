/* Geographically accurate topographic map of Canada.
 *
 * The relief raster (data/relief.png) is stored in equirectangular (plate
 * carrée) lon/lat space. We reproject it pixel-by-pixel into the Canada
 * Lambert Conformal Conic projection (EPSG:3978) and overlay Natural Earth
 * vector features rendered with the same projection. */

const OCEAN = [70, 104, 148]; // matches the relief's ocean tone

const layers = {
  relief: document.getElementById("relief"),
  overlay: document.getElementById("overlay"),
};
const ctx = layers.relief.getContext("2d", { willReadFrequently: false });
const tooltip = document.getElementById("tooltip");

let bounds, srcData, data = {};

async function load() {
  const [relief, prov, lakes, rivers, cities] = await Promise.all([
    d3.json("data/relief.json"),
    d3.json("data/provinces.json"),
    d3.json("data/lakes.json"),
    d3.json("data/rivers.json"),
    d3.json("data/cities.json"),
  ]);
  bounds = relief;
  data = { prov, lakes, rivers, cities };

  // Pull the relief PNG into an offscreen canvas to read its pixels.
  const img = await new Promise((res, rej) => {
    const im = new Image();
    im.onload = () => res(im);
    im.onerror = rej;
    im.src = "data/relief.png";
  });
  const off = document.createElement("canvas");
  off.width = relief.width;
  off.height = relief.height;
  const octx = off.getContext("2d");
  octx.drawImage(img, 0, 0);
  srcData = octx.getImageData(0, 0, relief.width, relief.height);

  render();
  document.getElementById("loader").classList.add("done");
  window.addEventListener("resize", debounce(render, 200));
  wireControls();
}

/* Canada Atlas Lambert (EPSG:3978): standard parallels 49°N & 77°N,
 * central meridian 95°W, latitude of origin 49°N. */
function makeProjection(w, h) {
  return d3.geoConicConformal()
    .parallels([49, 77])
    .rotate([95, 0])
    .center([0, 63])
    .fitExtent([[12, 70], [w - 12, h - 30]], data.prov);
}

let projection, path;

function render() {
  const rect = layers.relief.getBoundingClientRect();
  const cssW = Math.max(2, Math.floor(rect.width));
  const cssH = Math.max(2, Math.floor(rect.height));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  projection = makeProjection(cssW, cssH);
  path = d3.geoPath(projection);

  drawRelief(cssW, cssH, dpr);
  drawOverlay(cssW, cssH);
}

/* --- Raster reprojection ------------------------------------------------- */
function drawRelief(cssW, cssH, dpr) {
  const W = Math.floor(cssW * dpr);
  const H = Math.floor(cssH * dpr);
  layers.relief.width = W;
  layers.relief.height = H;

  const out = ctx.createImageData(W, H);
  const od = out.data;
  const sd = srcData.data, sw = srcData.width, sh = srcData.height;
  const { west, east, south, north } = bounds;
  const invLon = (sw - 1) / (east - west);
  const invLat = (sh - 1) / (north - south);

  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const oi = (y * W + x) << 2;
      const p = projection.invert([x / dpr, y / dpr]);
      if (!p) { setOcean(od, oi); continue; }
      const lon = p[0], lat = p[1];
      if (lon < west || lon > east || lat < south || lat > north || Number.isNaN(lon)) {
        setOcean(od, oi); continue;
      }
      const sx = ((lon - west) * invLon) | 0;
      const sy = ((north - lat) * invLat) | 0;
      const si = (sy * sw + sx) << 2;
      od[oi] = sd[si];
      od[oi + 1] = sd[si + 1];
      od[oi + 2] = sd[si + 2];
      od[oi + 3] = 255;
    }
  }
  ctx.putImageData(out, 0, 0);
}

function setOcean(od, i) {
  od[i] = OCEAN[0]; od[i + 1] = OCEAN[1]; od[i + 2] = OCEAN[2]; od[i + 3] = 255;
}

/* --- Vector overlay (SVG) ------------------------------------------------ */
function drawOverlay(w, h) {
  const svg = d3.select(layers.overlay)
    .attr("viewBox", `0 0 ${w} ${h}`)
    .attr("preserveAspectRatio", "none");
  svg.selectAll("*").remove();

  const show = id => document.getElementById(id).checked;

  // Graticule (optional)
  if (show("t-grid")) {
    const grat = d3.geoGraticule().step([10, 10]);
    svg.append("path").datum(grat()).attr("class", "graticule").attr("d", path);
  }

  // Rivers
  if (show("t-rivers")) {
    svg.append("g").selectAll("path")
      .data(data.rivers.features).join("path")
      .attr("class", "river").attr("d", path);
  }

  // Lakes
  svg.append("g").selectAll("path")
    .data(data.lakes.features).join("path")
    .attr("class", "lake").attr("d", path);

  // Province / territory borders + coastline
  if (show("t-borders")) {
    svg.append("g").selectAll("path")
      .data(data.prov.features).join("path")
      .attr("class", "province").attr("d", path);
  }

  // Invisible hit areas for province tooltips
  svg.append("g").selectAll("path")
    .data(data.prov.features).join("path")
    .attr("class", "province-hit").attr("d", path)
    .on("mousemove", (e, d) => showTip(e, d.properties.name || d.properties.gn_name))
    .on("mouseleave", hideTip);

  // Cities
  if (show("t-labels")) drawCities(svg);
}

function drawCities(svg) {
  const g = svg.append("g");
  const pts = data.cities.features
    .map(f => ({ f, xy: projection(f.geometry.coordinates) }))
    .filter(d => d.xy);

  // Simple greedy de-clutter so labels don't pile up
  const placed = [];
  const labelled = pts.filter(d => {
    const [x, y] = d.xy;
    const ok = !placed.some(p => Math.abs(p[0] - x) < 60 && Math.abs(p[1] - y) < 14);
    if (ok) placed.push(d.xy);
    return ok;
  });

  const dots = g.selectAll("g.city").data(pts).join("g").attr("class", "city")
    .attr("transform", d => `translate(${d.xy[0]},${d.xy[1]})`)
    .on("mousemove", (e, d) => showTip(e, cityLabel(d.f)))
    .on("mouseleave", hideTip);

  dots.append("circle")
    .attr("r", d => d.f.properties.capital ? 3.6 : 2.6)
    .attr("class", d => "city-dot" + (d.f.properties.capital ? " capital" : ""));

  g.selectAll("text").data(labelled).join("text")
    .attr("class", d => "city-label" + (d.f.properties.capital ? " capital" : ""))
    .attr("x", d => d.xy[0] + 6)
    .attr("y", d => d.xy[1] + 3.5)
    .text(d => d.f.properties.name);
}

function cityLabel(f) {
  const p = f.properties;
  const pop = p.pop ? ` · ${d3.format(",")(p.pop)}` : "";
  return (p.capital ? "★ " : "") + p.name + pop;
}

/* --- Tooltip + controls -------------------------------------------------- */
function showTip(e, text) {
  if (!text) return;
  tooltip.textContent = text;
  tooltip.style.left = e.clientX + "px";
  tooltip.style.top = e.clientY + "px";
  tooltip.classList.remove("hidden");
}
function hideTip() { tooltip.classList.add("hidden"); }

function wireControls() {
  ["t-labels", "t-rivers", "t-borders", "t-grid"].forEach(id =>
    document.getElementById(id).addEventListener("change", () => {
      const r = layers.relief.getBoundingClientRect();
      drawOverlay(Math.floor(r.width), Math.floor(r.height));
    }));
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

load().catch(err => {
  console.error(err);
  document.getElementById("loader").textContent =
    "Failed to load map data. Did you run: pip install -r requirements.txt && python build_data.py ?";
});
