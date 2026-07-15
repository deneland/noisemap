/* Static Zaventem noise map — Leaflet (no server, no WebGL). Reads precomputed
   grids from data/noise_<variant>.json and renders Lden/Lnight/peak-LAmax. */

const map = L.map("map", { zoomControl: true, attributionControl: true }).setView([50.90, 4.55], 11);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  subdomains: "abcd", maxZoom: 19,
  attribution: '&copy; OpenStreetMap &copy; CARTO',
}).addTo(map);

const $ = (id) => document.getElementById(id);
const state = { variant: "all", metric: "lden", mode: "smooth" };
const UPS = 6;                 // display upsample factor (bilinear)
const BAND_DB = 5;             // dB step for "bands" (filled-contour) mode
let META = null;
const GRIDS = {};             // variant -> grid json
let LOOKUP = null, GRID = null, overlay = null, pin = null;

/* ---- colour scale ------------------------------------------------------ */
const NOISE_RANGE = { lden: [45, 70], lnight: [40, 60], lamax: [55, 85] };
const STOPS = [[0, [26, 152, 80]], [0.35, [255, 235, 120]], [0.6, [253, 174, 97]],
               [0.8, [230, 74, 52]], [1, [136, 20, 90]]];
function ramp(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 1; i < STOPS.length; i++) {
    const [t1, c1] = STOPS[i], [t0, c0] = STOPS[i - 1];
    if (t <= t1) { const f = (t - t0) / ((t1 - t0) || 1); return c0.map((v, k) => Math.round(v + (c1[k] - v) * f)); }
  }
  return STOPS[STOPS.length - 1][1];
}
const noiseColor = (v, m) => { const [lo, hi] = NOISE_RANGE[m]; return ramp((v - lo) / (hi - lo)); };
const METRIC_IDX = { lden: 2, lnight: 3, lamax: 4 };
const METRIC_HINT = {
  lden: "Average over a full day, with evening +5 dB and night +10 dB penalties — the standard long-term annoyance measure. WHO: aim below 45 dB.",
  lnight: "Average over the night (23:00–07:00) — reflects sleep disturbance. WHO: aim below 40 dB.",
  lamax: "The loudest single modelled flyover here — how loud it gets when a plane passes, not an average.",
};
const METRIC_TITLE = { lden: "Predicted Lden (dB)", lnight: "Predicted Lnight (dB)", lamax: "Peak flyover LAmax (dB)" };
const METRIC_REF = {
  lden: "WHO advises below 45 dB Lden", lnight: "WHO advises below 40 dB Lnight", lamax: "",
};

/* ---- interpretation ---------------------------------------------------- */
function ldenBand(v) {
  if (v < 45) return ["calm", "#4ade80"];
  if (v < 54) return ["moderate annoyance", "#fde047"];
  if (v < 60) return ["considerable annoyance", "#fb923c"];
  return ["high annoyance", "#f87171"];
}
function lnightBand(v) {
  if (v < 40) return ["fine for sleep", "#4ade80"];
  if (v < 45) return ["some sleep disturbance", "#fde047"];
  if (v < 50) return ["disturbed sleep", "#fb923c"];
  return ["strong sleep disturbance", "#f87171"];
}
function loudnessRef(v) {
  if (v < 50) return "a quiet room";
  if (v < 60) return "normal conversation";
  if (v < 70) return "a vacuum cleaner nearby";
  if (v < 80) return "a busy street";
  return "very loud";
}

/* ---- overlay + legend -------------------------------------------------- */
// Both modes bilinear-upsample the coarse grid to a fine canvas (removes the
// ~500 m blockiness). "smooth" uses the continuous ramp; "bands" quantises to
// BAND_DB steps -> flat colours with boundaries that follow the smooth field,
// i.e. a filled-contour look, without a marching-squares library.
function renderOverlay() {
  if (!GRID) return;
  const { nx, ny, lat0, lon0, cell_deg } = GRID, mi = METRIC_IDX[state.metric];
  const [lo, hi] = NOISE_RANGE[state.metric];
  const floor = GRID.floor_db != null ? GRID.floor_db : 35;
  const V = new Float32Array(nx * ny).fill(floor);   // value (missing -> floor)
  const M = new Float32Array(nx * ny);               // presence mask
  for (const c of GRID.cells) { const v = c[mi]; if (v != null) { V[c[0] * nx + c[1]] = v; M[c[0] * nx + c[1]] = 1; } }
  const banded = state.mode === "bands";
  const nb = Math.max(1, Math.round((hi - lo) / BAND_DB));
  const W = nx * UPS, H = ny * UPS;
  const cvs = document.createElement("canvas"); cvs.width = W; cvs.height = H;
  const ctx = cvs.getContext("2d"), img = ctx.createImageData(W, H);
  for (let py = 0; py < H; py++) {
    const gy = (H - 1 - py) / UPS, gi0 = Math.min(Math.floor(gy), ny - 1), fy = gy - gi0, gi1 = Math.min(gi0 + 1, ny - 1);
    for (let px = 0; px < W; px++) {
      const gx = px / UPS, gj0 = Math.min(Math.floor(gx), nx - 1), fx = gx - gj0, gj1 = Math.min(gj0 + 1, nx - 1);
      const w00 = (1 - fx) * (1 - fy), w01 = fx * (1 - fy), w10 = (1 - fx) * fy, w11 = fx * fy;
      const i00 = gi0 * nx + gj0, i01 = gi0 * nx + gj1, i10 = gi1 * nx + gj0, i11 = gi1 * nx + gj1;
      const m = M[i00] * w00 + M[i01] * w01 + M[i10] * w10 + M[i11] * w11;
      if (m < 0.03) continue;                          // no data here -> transparent
      const v = V[i00] * w00 + V[i01] * w01 + V[i10] * w10 + V[i11] * w11;
      let t = Math.max(0, Math.min(1, (v - lo) / (hi - lo)));
      if (banded) t = (Math.floor(t * nb) + 0.5) / nb;
      const [r, g, b] = ramp(t);
      const p = (py * W + px) * 4;
      img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = Math.round(Math.min(1, m) * 190);
    }
  }
  ctx.putImageData(img, 0, 0);
  const bounds = [[lat0, lon0], [lat0 + ny * cell_deg, lon0 + nx * cell_deg]];
  if (overlay) map.removeLayer(overlay);
  overlay = L.imageOverlay(cvs.toDataURL(), bounds, { opacity: 0.82, interactive: false }).addTo(map);
  updateLegend();
}
function updateLegend() {
  const m = state.metric, [lo, hi] = NOISE_RANGE[m];
  const stops = [];
  if (state.mode === "bands") {                        // hard steps matching the map
    const nb = Math.max(1, Math.round((hi - lo) / BAND_DB));
    for (let i = 0; i < nb; i++) {
      const c = `rgb(${noiseColor(lo + (i + 0.5) / nb * (hi - lo), m).join(",")})`;
      stops.push(`${c} ${i / nb * 100}%`, `${c} ${(i + 1) / nb * 100}%`);
    }
  } else {
    for (let i = 0; i <= 10; i++) stops.push(`rgb(${noiseColor(lo + (hi - lo) * i / 10, m).join(",")}) ${i * 10}%`);
  }
  $("legend-bar").style.background = `linear-gradient(90deg,${stops.join(",")})`;
  $("legend-lbl").innerHTML = `<span>${lo}</span><span>${Math.round((lo + hi) / 2)}</span><span>${hi}+ dB</span>`;
  $("legend-ref").textContent = METRIC_REF[m];
  $("metric-hint").textContent = METRIC_HINT[m];
}

/* ---- data -------------------------------------------------------------- */
async function loadVariant(name) {
  if (!GRIDS[name]) {
    $("sub").textContent = "loading…";
    GRIDS[name] = await (await fetch(`data/noise_${name}.json`)).json();
  }
  GRID = GRIDS[name];
  LOOKUP = new Map();
  for (const c of GRID.cells) LOOKUP.set(c[0] * GRID.nx + c[1], c);
  $("sub").textContent = `modelled · uncalibrated · ${GRID.days} day(s)`;
  const dd = $("doc-days"); if (dd) dd.textContent = `${GRID.days} day(s)`;
  if (name === "all") { const e = $("d-days"); if (e) e.textContent = `${GRID.days} days of data`; }
  renderOverlay();
  if (pin) updateDrawer(pin.getLatLng());
}

/* ---- click / drawer ---------------------------------------------------- */
function cellAt(lat, lng) {
  const gi = Math.round((lat - GRID.lat0) / GRID.cell_deg);
  const gj = Math.round((lng - GRID.lon0) / GRID.cell_deg);
  return LOOKUP.get(gi * GRID.nx + gj);
}
const row = (lab, val) => `<div class="dw-row"><span class="lab">${lab}</span><span>${val}</span></div>`;
function updateDrawer(latlng) {
  const c = cellAt(latlng.lat, latlng.lng);
  $("drawer").classList.add("open");
  let h = `<p class="sub" style="margin:0 0 4px">${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}</p>`;
  if (!c) {
    h += `<div class="hint">Below the modelled noise floor here — little or no low aircraft traffic.</div>`;
  } else {
    const lden = c[2], lnight = c[3], lamax = c[4];
    const [lt, lc] = ldenBand(lden), [nt, nc] = lnightBand(lnight);
    h += row("Lden (day-eve-night)", `<b>${lden} dB</b> · <span style="color:${lc}">${lt}</span>`);
    h += row("Lnight (23–07h)", `<b>${lnight} dB</b> · <span style="color:${nc}">${nt}</span>`);
    h += row("Peak flyover", `<b>${lamax} dB</b> <span style="color:#93a1b0">· like ${loudnessRef(lamax)}</span>`);
    h += `<div class="hint" style="margin-top:8px">${GRID.variant === "all" ? "All days" :
      GRID.variant === "weekday" ? "Weekdays only" : "Weekends only"}, ${GRID.days} day(s). ` +
      `WHO advises Lden&lt;45, Lnight&lt;40 dB. Modelled estimate, not a measurement.</div>`;
  }
  $("dw-body").innerHTML = h;
}
map.on("click", (e) => {
  if (pin) pin.setLatLng(e.latlng);
  else pin = L.circleMarker(e.latlng, { radius: 7, color: "#fff", weight: 2, fillColor: "#4aa8ff", fillOpacity: 1 }).addTo(map);
  updateDrawer(e.latlng);
});
$("dw-close").onclick = () => { if (pin) { map.removeLayer(pin); pin = null; } $("drawer").classList.remove("open"); };

/* ---- controls ---------------------------------------------------------- */
function seg(id, key, after) {
  $(id).querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      $(id).querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on"); state[key] = b.dataset.v; after();
    };
  });
}
seg("metric", "metric", renderOverlay);
seg("variant", "variant", () => loadVariant(state.variant));
seg("mode", "mode", renderOverlay);

/* ---- boot -------------------------------------------------------------- */
(async () => {
  META = await (await fetch("data/meta.json")).json();
  if (META.airport) L.circleMarker([META.airport[0], META.airport[1]],
    { radius: 5, color: "#111", weight: 2, fillColor: "#ffd479", fillOpacity: 1 })
    .addTo(map).bindTooltip("Brussels Airport (EBBR)");
  const set = (id, v) => { const e = $(id); if (e) v != null && (e.textContent = v); };
  set("d-source", META.source);
  if (META.date_from) set("d-range", `${META.date_from} → ${META.date_to}`);
  const b = META.bbox;
  if (b) set("d-bbox", `${b[0]}–${b[1]}°N, ${b[2]}–${b[3]}°E`);
  set("d-alt", META.max_alt_ft && META.max_alt_ft.toLocaleString());
  set("d-samples", META.samples && META.samples.toLocaleString());
  await loadVariant("all");
})();
