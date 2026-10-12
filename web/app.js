"use strict";

const $ = (id) => document.getElementById(id);
// Resolve from the application entry point instead of the hostname root.
// This also supports the legacy /assets/index.html entry point.
const documentURL = new URL(document.baseURI);
const appBase = /^(https?|file):$/.test(documentURL.protocol)
  ? new URL(
      documentURL.protocol !== "file:" &&
      /\/assets\/(?:index\.html)?$/.test(documentURL.pathname)
        ? "../"
        : "./",
      documentURL,
    )
  : null;
const hasServerURL = appBase && /^https?:$/.test(appBase.protocol);
const PAGE_SIZE = 10;
const state = {
  file: null,
  features: [],
  mapFeatures: [],
  filtered: [],
  page: 0,
  selected: null,
  busy: false,
  maxUploadBytes: 20 * 1024 * 1024,
  storageMode: null,
};
const sessionResults = new Map();
const SESSION_KEY = "geomeasure.session.v1";
let sessionSaved = true;
const layers = new Map();
const number = new Intl.NumberFormat("en", { maximumFractionDigits: 2 });
const statuses = {
  MEASURED: "Measured",
  NO_MEASUREMENT: "No measurement",
  UNSUPPORTED: "Unsupported",
  INVALID: "Invalid",
  EMPTY: "Empty",
};
let recent = [];
try {
  const stored = JSON.parse(localStorage.getItem("geomeasure.recent") || "[]");
  if (Array.isArray(stored))
    recent = stored
      .filter(
        (item) =>
          typeof item.id === "string" && typeof item.filename === "string",
      )
      .slice(0, 6);
} catch {
  /* Storage may be unavailable or contain old data. */
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function displayValue(value) {
  return typeof value === "object" && value !== null
    ? JSON.stringify(value)
    : String(value ?? "—");
}
function nameOf(feature) {
  return displayValue(
    feature.properties.name ||
      feature.properties.Name ||
      `Feature ${feature.index + 1}`,
  );
}
function kindOf(feature) {
  const type = feature.geometry_type || "";
  if (type.includes("Polygon")) return "polygon";
  if (type.includes("LineString")) return "line";
  if (type.includes("Point")) return "point";
  return "other";
}
function readableType(feature) {
  return (feature.geometry_type || "No geometry").replace("Multi", "Multi ");
}
function measurementText(feature) {
  const measurement = feature.measurement;
  return measurement
    ? `${number.format(measurement.value)} ${measurement.unit === "m2" ? "m²" : "m"}`
    : "—";
}
function badge(feature) {
  const className =
    feature.status === "MEASURED"
      ? ""
      : ["INVALID", "UNSUPPORTED"].includes(feature.status)
        ? "warning"
        : "neutral";
  return node(
    "span",
    `badge ${className}`,
    statuses[feature.status] || feature.status,
  );
}
function feedback(message, error = false) {
  $("feedback").textContent = message;
  $("feedback").classList.toggle("error", error);
  $("feedback").hidden = !message;
}
function busy(value, message = "Reading your file…") {
  state.busy = value;
  $("drop-zone").classList.toggle("is-busy", value);
  $("drop-zone").setAttribute("aria-busy", String(value));
  for (const id of [
    "browse-button",
    "demo-button",
    "sidebar-demo",
    "map-demo",
    "file-input",
  ])
    $(id).disabled = value;
  $("browse-button").querySelector("span").textContent = value
    ? "Processing…"
    : "Choose a file";
  $("upload-heading").textContent = value ? message : "Upload your geospatial file";
  $("upload-description").textContent = value
    ? "We’re checking your geometry and calculating measurements."
    : "Drop your file here, or choose one to get started.";
  renderRecent();
}
async function request(url, options) {
  if (!hasServerURL) {
    throw new Error(
      "To upload and measure files, open this page through the running GeoMeasure app.",
    );
  }
  let response;
  try {
    response = await fetch(new URL(url.replace(/^\/+/, ""), appBase), options);
  } catch {
    throw new Error(
      "Could not reach the API. Check that the server is running and try again.",
    );
  }
  if (!response.ok) {
    let message = `Request failed (${response.status}). Please try again.`;
    try {
      const error = await response.json();
      if (typeof error.detail === "string") message = error.detail;
      else if (Array.isArray(error.detail))
        message = error.detail.map((item) => item.msg).join("; ");
    } catch {
      /* Preserve the HTTP error when the response is not JSON. */
    }
    throw new Error(message);
  }
  return response;
}
async function loadResults(info) {
  const features = [],
    mapFeatures = [];
  for (let offset = 0; offset < info.feature_count; offset += 1000) {
    const id = encodeURIComponent(info.id);
    const [measurements, mapPage] = await Promise.all([
      request(
        `/api/files/${id}/measurements/?offset=${offset}&limit=1000`,
      ).then((r) => r.json()),
      request(`/api/files/${id}/map/?offset=${offset}&limit=1000`).then((r) =>
        r.json(),
      ),
    ]);
    features.push(...measurements.features);
    mapFeatures.push(...mapPage.features);
  }
  displayResults(info, features, mapFeatures);
}
function loadProcessed(result) {
  if (
    typeof result?.file?.id !== "string" ||
    !Array.isArray(result.features) ||
    !Array.isArray(result.map?.features)
  ) {
    throw new Error(
      "Could not read these results. Please upload the file again.",
    );
  }
  sessionResults.delete(result.file.id);
  sessionResults.set(result.file.id, result);
  if (sessionResults.size > 6)
    sessionResults.delete(sessionResults.keys().next().value);
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(result));
    sessionSaved = true;
  } catch {
    sessionSaved = false;
    try {
      sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* Avoid restoring an older result after a failed cache write when possible. */
    }
  }
  displayResults(result.file, result.features, result.map.features);
}
function displayResults(info, features, mapFeatures) {
  state.file = info;
  state.features = features;
  state.mapFeatures = mapFeatures;
  state.selected = null;
  $("feature-search").value = "";
  $("geometry-filter").value = "all";
  updateSummary();
  filterTable();
  renderMap();
  renderInspector(null);
  recent = [
    { id: info.id, filename: info.filename },
    ...recent.filter((file) => file.id !== info.id),
  ].slice(0, 6);
  renderRecent();
  if (state.storageMode !== "none") {
    try {
      localStorage.setItem("geomeasure.recent", JSON.stringify(recent));
    } catch {
      /* Optional history. */
    }
  }
  const url = new URL(window.location.href);
  if (state.storageMode === "none") url.searchParams.delete("file");
  else url.searchParams.set("file", info.id);
  history.replaceState(null, "", url);
  $("download-button").disabled = false;
  const warnings = features.filter((f) =>
    ["INVALID", "UNSUPPORTED", "EMPTY"].includes(f.status),
  ).length;
  const hidden = mapFeatures.filter((f) => !f.geometry).length;
  const retention =
    state.storageMode === "none"
      ? sessionSaved
        ? " The latest result is kept in this tab. Download JSON to keep a copy."
        : " Browser storage is unavailable or full; download JSON before refreshing."
      : "";
  feedback(
    `${info.filename} is ready. ${info.feature_count} features processed.${warnings ? ` ${warnings} features have no usable measurement; inspect their status.` : ""}${hidden ? ` ${hidden} features cannot be displayed on the map.` : ""}${retention}`,
  );
}
async function upload(file) {
  if (state.busy || !file) return;
  await configurationReady;
  if (state.busy) return;
  if (!/\.(kml|zip)$/i.test(file.name)) {
    feedback("Choose a .kml file or a .zip containing a Shapefile.", true);
    return;
  }
  if (file.size > state.maxUploadBytes) {
    feedback(
      `This file is too large. Choose a file up to ${number.format(state.maxUploadBytes / 1024 / 1024)} MiB.`,
      true,
    );
    return;
  }
  busy(true);
  feedback("");
  try {
    const data = new FormData();
    data.append("file", file);
    const response = await request(
      state.storageMode === "none" ? "/api/process/" : "/api/files/",
      {
        method: "POST",
        body: data,
      },
    );
    const result = await response.json();
    if (state.storageMode === "none") loadProcessed(result);
    else await loadResults(result);
  } catch (error) {
    feedback(error.message, true);
  } finally {
    busy(false);
    $("file-input").value = "";
  }
}
async function openFile(id) {
  if (state.busy) return;
  await configurationReady;
  if (state.busy) return;
  busy(true, "Opening your file…");
  feedback("");
  try {
    if (state.storageMode === "none") {
      const result = sessionResults.get(id);
      if (!result)
        throw new Error(
          "This result is no longer in this tab. Please upload the file again.",
        );
      loadProcessed(result);
      return;
    }
    const response = await request(`/api/files/${encodeURIComponent(id)}/`);
    await loadResults(await response.json());
  } catch (error) {
    feedback(error.message, true);
  } finally {
    busy(false);
  }
}
async function sample() {
  if (state.busy) return;
  // The sample passes through the same real upload and processing API as user files.
  busy(true, "Getting the sample…");
  feedback("");
  let file;
  try {
    const response = await request("/example.kml");
    file = new File([await response.blob()], "survey.kml", {
      type: "application/vnd.google-earth.kml+xml",
    });
  } catch (error) {
    feedback(error.message, true);
  } finally {
    busy(false);
  }
  if (file) await upload(file);
}
function updateSummary() {
  const features = state.features;
  const areas = features.filter((f) => f.measurement?.kind === "area");
  const lengths = features.filter((f) => f.measurement?.kind === "length");
  const area = areas.reduce((sum, f) => sum + f.measurement.value, 0);
  const length = lengths.reduce((sum, f) => sum + f.measurement.value, 0);
  $("feature-count").textContent = number.format(features.length);
  $("feature-caption").textContent =
    `${state.file.measured_feature_count} measured · ${features.length - state.file.measured_feature_count} without measurement`;
  $("area-total").textContent = number.format(
    area >= 1000000 ? area / 1000000 : area,
  );
  $("area-unit").textContent = area >= 1000000 ? "km²" : "m²";
  $("area-caption").textContent =
    `${areas.length} measured polygon${areas.length === 1 ? "" : "s"}`;
  $("length-total").textContent = number.format(
    length >= 1000 ? length / 1000 : length,
  );
  $("length-unit").textContent = length >= 1000 ? "km" : "m";
  $("length-caption").textContent =
    `${lengths.length} measured line${lengths.length === 1 ? "" : "s"}`;
  $("source-crs").textContent = state.file.crs;
  $("source-crs").title = state.file.crs;
  $("crs-caption").textContent = "Map display transformed to WGS84";
  $("file-description").textContent =
    `${state.file.filename} · ${(state.file.size_bytes / 1024).toFixed(1)} KB · ${state.file.crs}`;
  $("map-subtitle").textContent = state.file.filename;
}
function renderRecent() {
  const list = $("recent-files");
  list.replaceChildren();
  $("recent-count").textContent = recent.length;
  if (!recent.length)
    list.append(
      node("p", "recent-empty", "Your recent uploads will appear here."),
    );
  for (const file of recent) {
    const button = node(
      "button",
      `recent-file ${state.file?.id === file.id ? "current" : ""}`,
    );
    button.disabled = state.busy;
    button.append(icon("file"), node("span", "recent-name", file.filename));
    button.title = file.filename;
    button.addEventListener("click", () => openFile(file.id));
    list.append(button);
  }
}
function filterTable() {
  const search = $("feature-search").value.toLowerCase().trim();
  const kind = $("geometry-filter").value;
  state.filtered = state.features.filter(
    (feature) =>
      (kind === "all" || kindOf(feature) === kind) &&
      (!search ||
        `${nameOf(feature)} ${feature.feature_id} ${JSON.stringify(feature.properties)}`
          .toLowerCase()
          .includes(search)),
  );
  state.page = 0;
  renderTable();
}
function renderTable() {
  const body = $("feature-rows");
  body.replaceChildren();
  const total = state.filtered.length;
  const start = state.page * PAGE_SIZE;
  for (const feature of state.filtered.slice(start, start + PAGE_SIZE)) {
    const row = node("tr", feature.index === state.selected ? "selected" : "");
    const name = node("td");
    name.append(
      node("span", "feature-name", nameOf(feature)),
      node("span", "feature-id", `ID ${feature.feature_id}`),
    );
    const geometry = node("td");
    const label = node("span", "geometry-label");
    label.append(
      node("i", `geometry-dot ${kindOf(feature)}`),
      node("span", "", readableType(feature)),
    );
    geometry.append(label);
    const status = node("td");
    status.append(badge(feature));
    const action = node("td");
    const button = node("button", "row-inspect", "↗");
    button.setAttribute("aria-label", `Inspect ${nameOf(feature)}`);
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      selectFeature(feature.index);
    });
    action.append(button);
    row.append(
      name,
      geometry,
      node("td", "", measurementText(feature)),
      status,
      action,
    );
    row.addEventListener("click", () => selectFeature(feature.index));
    body.append(row);
  }
  $("result-count").textContent = state.features.length;
  $("table-empty").hidden = total > 0;
  if (state.file && !total) {
    $("table-empty").querySelector("h3").textContent = "No matching features";
    $("table-empty").querySelector("p").textContent =
      "Try another search or geometry filter.";
  }
  $("table-summary").textContent = state.file
    ? `${total ? start + 1 : 0}–${Math.min(start + PAGE_SIZE, total)} of ${total} features${total !== state.features.length ? " · filtered" : ""}`
    : "No file selected";
  $("page-indicator").textContent =
    `${total ? state.page + 1 : 0} / ${Math.ceil(total / PAGE_SIZE)}`;
  $("previous-page").disabled = state.page === 0;
  $("next-page").disabled = start + PAGE_SIZE >= total;
}

const map = L.map("map", {
  zoomControl: true,
  preferCanvas: false,
  minZoom: 1,
}).setView([28, 77], 8);
map.attributionControl.setPrefix("Leaflet");
const street = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
});
const satellite = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    maxZoom: 19,
    attribution:
      'Imagery &copy; <a href="https://www.esri.com/">Esri</a>, Maxar, Earthstar Geographics, and the GIS User Community',
  },
);
const basemaps = { street, satellite };
let basemapKind = "satellite";
let tileErrorShown = false;
for (const [kind, layer] of Object.entries(basemaps)) {
  layer.on("tileerror", () => {
    if (kind === basemapKind && map.hasLayer(layer) && !tileErrorShown) {
      tileErrorShown = true;
      const name = kind === "street" ? "OpenStreetMap" : "Satellite";
      $("map-status").textContent =
        `${name} tiles could not load. Your feature outlines still work. Check your connection or switch the basemap.`;
      $("map-status").hidden = false;
    }
  });
}
if ($("street-map").checked) basemaps[basemapKind].addTo(map);
let featureGroup = L.featureGroup().addTo(map);
function layerStyle(feature, selected = false) {
  const imagery = basemapKind === "satellite" && $("street-map").checked;
  const color = imagery
    ? selected
      ? "#ffffff"
      : kindOf(feature) === "line"
        ? "#ff7b45"
        : kindOf(feature) === "point"
          ? "#6fe4ec"
          : "#ffd479"
    : selected
      ? "#c77642"
      : kindOf(feature) === "line"
        ? "#c99159"
        : kindOf(feature) === "point"
          ? "#447f80"
          : "#64894a";
  return {
    color,
    fillColor: selected ? "#e9b38a" : color,
    weight: selected ? 4 : imagery ? 3 : 2,
    fillOpacity: selected ? 0.3 : 0.17,
    radius: selected ? 7 : 5,
  };
}
function renderMap() {
  featureGroup.clearLayers();
  layers.clear();
  let count = 0;
  for (const geo of state.mapFeatures) {
    if (!geo.geometry) continue;
    const feature = state.features[geo.properties.index];
    if (!feature) continue;
    const layer = L.geoJSON(geo, {
      style: () => layerStyle(feature),
      pointToLayer: (_, latlng) => L.circleMarker(latlng, layerStyle(feature)),
    });
    // DOM text prevents uploaded attribute values becoming executable HTML.
    const tooltip = node("span", "", nameOf(feature));
    layer.bindTooltip(tooltip);
    layer.on("click", () => selectFeature(feature.index));
    featureGroup.addLayer(layer);
    layers.set(feature.index, layer);
    count++;
  }
  $("map-count").textContent = `${count} feature${count === 1 ? "" : "s"}`;
  $("map-empty").hidden = count > 0;
  $("fit-map").disabled = count === 0;
  if (!count && state.file) {
    $("map-empty").querySelector("h3").textContent = "No displayable geometry";
    $("map-empty").querySelector("p").textContent =
      "Your results are available in the table below.";
  }
  fitMap();
}
function fitMap() {
  map.stop();
  const bounds = featureGroup.getBounds();
  if (bounds.isValid())
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16, animate: false });
}
function selectFeature(index) {
  state.selected = index;
  const feature = state.features[index];
  if (!feature) return;
  for (const [id, layer] of layers)
    layer.setStyle(layerStyle(state.features[id], id === index));
  const layer = layers.get(index);
  if (layer) {
    const bounds = layer.getBounds();
    if (bounds.isValid()) map.stop();
    map.fitBounds(bounds, { padding: [50, 50], maxZoom: 17, animate: false });
  }
  // Selecting a map feature also brings its table page into view when it matches the filter.
  const position = state.filtered.findIndex((f) => f.index === index);
  if (position >= 0) state.page = Math.floor(position / PAGE_SIZE);
  renderTable();
  renderInspector(feature);
}
function detailRow(label, value) {
  const row = node("div");
  row.append(node("dt", "", label), node("dd", "", displayValue(value)));
  return row;
}
function renderInspector(feature) {
  if (!feature) {
    $("inspector").replaceChildren();
    const empty = node("div", "inspector-empty");
    const art = node("span", "inspector-empty-icon");
    art.append(icon("search"));
    empty.append(
      art,
      node("h3", "", "Meet your features"),
      node(
        "p",
        "",
        "Select a feature on the map or in the table to see its geometry, attributes, and measurements.",
      ),
    );
    $("inspector").append(empty);
    return;
  }
  const content = node("div", "inspector-content");
  content.append(node("h3", "", nameOf(feature)), badge(feature));
  const measure = node("div", "detail-measure");
  measure.append(
    node(
      "div",
      "detail-label",
      feature.measurement?.kind === "area"
        ? "POLYGON AREA"
        : feature.measurement?.kind === "length"
          ? "LINE LENGTH"
          : "MEASUREMENT",
    ),
  );
  measure.append(node("div", "detail-number", measurementText(feature)));
  content.append(measure);
  const details = node("dl", "detail-list");
  details.append(
    detailRow("Feature ID", feature.feature_id),
    detailRow("Geometry", readableType(feature)),
    detailRow("Source CRS", feature.crs),
  );
  content.append(details, node("h4", "attributes-heading", "ATTRIBUTES"));
  const attributes = node("dl", "detail-list");
  for (const [key, value] of Object.entries(feature.properties))
    attributes.append(detailRow(key, value));
  if (!Object.keys(feature.properties).length)
    attributes.append(detailRow("Attributes", "None"));
  content.append(attributes);
  if (feature.message)
    content.append(node("p", "detail-message", feature.message));
  const geo = state.mapFeatures[feature.index];
  if (geo?.properties.map_error)
    content.append(node("p", "detail-message", geo.properties.map_error));
  $("inspector").replaceChildren(content);
}

$("browse-button").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", () =>
  upload($("file-input").files[0]),
);
for (const id of ["demo-button", "sidebar-demo", "map-demo"])
  $(id).addEventListener("click", sample);
for (const event of ["dragenter", "dragover"])
  $("drop-zone").addEventListener(event, (e) => {
    e.preventDefault();
    if (!state.busy) $("drop-zone").classList.add("dragging");
  });
$("drop-zone").addEventListener("dragleave", (event) => {
  if (!$("drop-zone").contains(event.relatedTarget))
    $("drop-zone").classList.remove("dragging");
});
$("drop-zone").addEventListener("drop", (event) => {
  event.preventDefault();
  $("drop-zone").classList.remove("dragging");
  if (event.dataTransfer.files.length > 1) {
    feedback("Please upload one geospatial dataset at a time.", true);
    return;
  }
  upload(event.dataTransfer.files[0]);
});
// Prevent the browser from navigating away when a file is dropped outside the upload area.
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());
$("feature-search").addEventListener("input", filterTable);
$("geometry-filter").addEventListener("change", filterTable);
$("previous-page").addEventListener("click", () => {
  if (state.page > 0) {
    state.page--;
    renderTable();
  }
});
$("next-page").addEventListener("click", () => {
  if ((state.page + 1) * PAGE_SIZE < state.filtered.length) {
    state.page++;
    renderTable();
  }
});
$("fit-map").addEventListener("click", fitMap);
function updateBasemap() {
  $("map-status").hidden = true;
  tileErrorShown = false;
  for (const [kind, layer] of Object.entries(basemaps)) {
    if (kind !== basemapKind || !$("street-map").checked) map.removeLayer(layer);
    $(`${kind}-view`).setAttribute("aria-pressed", String(kind === basemapKind));
  }
  if ($("street-map").checked) basemaps[basemapKind].addTo(map);
  for (const [index, layer] of layers)
    layer.setStyle(layerStyle(state.features[index], index === state.selected));
}
$("street-map").addEventListener("change", updateBasemap);
for (const kind of Object.keys(basemaps))
  $(`${kind}-view`).addEventListener("click", () => {
    basemapKind = kind;
    $("street-map").checked = true;
    updateBasemap();
  });
map.on("moveend", () => {
  const center = map.getCenter();
  $("map-coordinate").textContent =
    `${Math.abs(center.lat).toFixed(4)}° ${center.lat >= 0 ? "N" : "S"} · ${Math.abs(center.lng).toFixed(4)}° ${center.lng >= 0 ? "E" : "W"}`;
});
new ResizeObserver(() => map.invalidateSize()).observe($("map"));
$("download-button").addEventListener("click", () => {
  if (!state.file) return;
  const blob = new Blob(
    [JSON.stringify({ file: state.file, features: state.features }, null, 2)],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const anchor = node("a");
  anchor.href = url;
  anchor.download =
    state.file.filename.replace(/\.(kml|zip)$/i, "") + "-measurements.json";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
renderRecent();
renderTable();
const bookmarked = new URLSearchParams(window.location.search).get("file");
const configurationReady = hasServerURL
  ? request("api/config/")
      .then((response) => response.json())
      .then((config) => {
        if (
          !Number.isSafeInteger(config.max_upload_bytes) ||
          config.max_upload_bytes <= 0
        ) {
          throw new Error("Invalid upload configuration.");
        }
        state.maxUploadBytes = config.max_upload_bytes;
        state.storageMode = config.storage_mode;
        if (state.storageMode === "none") {
          recent = [];
          renderRecent();
        }
        $("upload-limit").textContent =
          `Up to ${number.format(state.maxUploadBytes / 1024 / 1024)} MiB`;
      })
      .catch(() => {
        feedback(
          "The interface is loaded, but file processing is unavailable. Open this page through the running GeoMeasure app.",
          true,
        );
      })
  : Promise.resolve();
if (hasServerURL) {
  $("api-docs-link").href = new URL("docs", appBase).href;
  $("brand-home").href = appBase.href;
} else {
  $("api-docs-link").removeAttribute("href");
  $("api-docs-link").setAttribute("aria-disabled", "true");
}
if (!hasServerURL) {
  feedback(
    "The interface is loaded. To upload and measure files, open this page through the running GeoMeasure app.",
    true,
  );
} else {
  configurationReady.then(() => {
    if (state.storageMode === "none") {
      try {
        const saved = sessionStorage.getItem(SESSION_KEY);
        if (saved) loadProcessed(JSON.parse(saved));
        else if (bookmarked)
          feedback(
            "Results are kept in the original browser tab. Please upload your file here.",
            true,
          );
      } catch {
        feedback(
          "Saved results could not be restored. Please upload the file again.",
          true,
        );
      }
    } else if (bookmarked) {
      openFile(bookmarked);
    }
  });
  request("health").catch(() =>
    feedback(
      "The interface is loaded, but file processing is unavailable. Open this page through the running GeoMeasure app.",
      true,
    ),
  );
}
