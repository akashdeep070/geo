# GeoMeasure

Upload a KML file or zipped Shapefile, calculate polygon areas and line lengths, and explore the results on an interactive map.

**Deploy the frontend and API together on Vercel without a database, PostgreSQL account, or SQL setup.** The API processes each upload and returns the complete result to your browser. The latest result can survive a refresh in the same tab; download the JSON to keep a copy.

Built with **FastAPI · PyShp · Shapely · pyproj · JavaScript · Leaflet**. No Node.js server or separate frontend build is needed to run the app.

## What you can do

- Upload `.kml` files or `.zip` archives containing a Shapefile.
- Calculate polygon area in square metres and line length in metres.
- Inspect feature names, attributes, source CRS, geometry, and measurement status.
- Explore a map with pan, zoom, and selection linked to the feature inspector.
- Search, filter by geometry type, and browse paginated results.
- Download measurements and source geometry as JSON.
- Try a bundled sample containing a polygon, line, and point.
- Use the responsive interface on desktop and mobile.

Points appear on the map but have no area or length measurement. Invalid or unsupported features receive an explanation while other features continue processing.

## Deploy to Vercel — no database

1. Push **the complete project** to your GitHub repository, including `app/`, `web/`, `examples/`, `pyproject.toml`, `uv.lock`, `.python-version`, and `vercel.json`.
2. In Vercel, choose **Add New → Project**, import the repository, and select the directory containing `vercel.json` as the **Root Directory**. Use the **FastAPI** framework preset. Keep the install command automatic and let `vercel.json` supply the build command; do not set an Output Directory.
3. Click **Deploy**. No database integration, `DATABASE_URL`, or other environment variable is required for a fresh project.
4. Open the preview address and click **Try a sample**. You should see three features, two measurements, and map outlines. `/health` should return `{"status":"ok"}`; API documentation is at `/docs`.

`vercel.json` explicitly sets **`GEO_STORAGE_MODE=none`**, so existing `DATABASE_URL` or `POSTGRES_URL` values are ignored. It includes the frontend and example files in the function, sets a 60-second function duration, and checks that the committed frontend matches its sources during the build. For private testing, use Vercel Deployment Protection; the app itself has no login.

Vercel supplies `VERCEL=1` automatically. This deployment starts in `none` storage mode and uses `POST /api/process/`. It creates no SQLite file and keeps no results on the server between requests. Uploads work across separate serverless instances without shared storage.

The repository includes the deployment configuration. **An actual Vercel deployment still requires your Vercel account; a live preview is not created by cloning or downloading these files.** Native geospatial libraries increase function size, so inspect your Vercel build logs and function resource settings if deployment fails.

### Deploy directly from the project folder

If you prefer to skip the GitHub import, use the Vercel CLI from the repository root:

```sh
npx vercel
```

Sign in, choose your account and project, and keep the project root as `.`. This creates a preview deployment. The CLI needs Node.js; the application itself runs on Python. Use `npx vercel --prod` only when you want a production deployment.

To create a portable copy of the complete source project:

```sh
python scripts/package_deployment.py
```

The resulting `dist/geo-vercel.zip` contains the app, frontend, configuration, README, tests, and development scripts. It excludes virtual environments, local databases, `.env` files, caches, Git metadata, and linked Vercel account files. Extract it into a fresh directory, then import its contents into GitHub or run `npx vercel` there. Vercel's import screen accepts repositories, rather than ZIP uploads.

Successful GitHub Actions runs also provide this ZIP as the `geo-vercel-source` artifact.

### Limits and result lifetime

| Behavior                        | Vercel without a database                                                 |
| ------------------------------- | ------------------------------------------------------------------------- |
| Upload size                     | Up to **3 MiB** per file, allowing room for multipart overhead            |
| Processed JSON response         | Up to **4 MiB**, including source and map geometry                        |
| Server storage                  | None; temporary source files are deleted after processing                 |
| Latest result                   | Cached in `sessionStorage` when browser storage permits                   |
| Refresh in the same tab         | Restores the latest cached result                                         |
| Recent files                    | Up to six in memory; after refresh, only the latest cached result remains |
| New independent browser session | Starts without previous results                                           |
| Sharing a result URL            | Unavailable in this mode; share the downloaded JSON instead               |

Browser session storage is temporary. Closing the tab normally clears it; browser session restoration may retain it. If storage is blocked or full, processing and JSON download still work, but refresh cannot restore the result. The interface tells you when caching fails.

Dense geometries can exceed the response limit even when the uploaded file is small. Use a smaller dataset if the API returns that error.

## Run locally

Install **Python 3.12+** and [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these commands from the repository root:

```sh
uv sync --locked
GEO_STORAGE_MODE=none uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Windows PowerShell, use:

```powershell
uv sync --locked
$env:GEO_STORAGE_MODE = "none"
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in your local browser. This runs the same processing flow used by the Vercel preview, with a **20 MiB** local upload limit. Add `VERCEL=1` to the environment to reproduce Vercel's smaller request and response limits locally.

Without `GEO_STORAGE_MODE=none`, ordinary local development defaults to SQLite in `./data` when no database URL is set. Existing local saved results remain available in that mode.

Shapefile uploads use PyShp, a pure Python reader, so the production app does not require Fiona or GDAL. Shapely and pyproj use native GEOS and PROJ libraries included in their locked wheels on supported platforms. Fiona is a development dependency used only to generate independent Shapefile test fixtures.

## Using the interface

1. Click **Try a sample**, choose a file, or drag it into the upload area.
2. Check the feature count, area and length totals, and source CRS.
3. Click a feature in the map or table to inspect its geometry and attributes.
4. Use search and the geometry filter to narrow the table.
5. Click the JSON download button to save your results.

The optional **Street basemap** checkbox requests OpenStreetMap tiles and shares the visible map bounds with that provider. It starts disabled. Feature outlines and map controls work without external tiles; upload processing still requires the running API.

Opening `web/index.html` as a static file loads the interface, styling, and map scripts, but does not start the API. Use the Vercel address or the local app server for uploads.

## Accepted files and measurements

| Input         | Requirements                                                                          |
| ------------- | ------------------------------------------------------------------------------------- |
| KML           | Standard KML 2.2 with Point, LineString, Polygon, or MultiGeometry                    |
| Shapefile ZIP | Exactly one dataset with matching `.shp`, `.shx`, `.dbf`, and `.prj`; optional `.cpg` |

Nested ZIP folders and uppercase extensions are accepted. Missing or invalid Shapefile CRS is rejected rather than guessed. KML names, descriptions, ExtendedData, folder nesting, polygon holes, and multipart geometries are retained where supported. KMZ and remote NetworkLinks are not processed. KML Model and gx:Track geometries are marked unsupported and retain their source XML.

Every measured feature is transformed from its source CRS to WGS84, then to a feature-centered projection in metres:

- **Polygon/MultiPolygon:** Lambert azimuthal equal-area (LAEA), reported in `m2`.
- **LineString/MultiLineString:** azimuthal equidistant (AEQD), reported in `m`.
- **Point/MultiPoint:** `NO_MEASUREMENT`, with a null measurement.
- **Mixed GeometryCollection:** `UNSUPPORTED`.
- **Invalid or empty geometry:** `INVALID` or `EMPTY`, with an explanation.

Source coordinates remain in the original CRS. The separate map geometry is converted to WGS84; `measurement_crs` identifies the calculation projection. Z coordinates do not contribute to horizontal area or length. Projected input in feet is converted to metric units before measurement.

These are planar measurements intended for local and regional data. Sparse, very large, global, or dateline-crossing geometries can have significant distortion or ambiguous topology. Results are not survey-certified, and invalid geometries are not automatically repaired.

Other processing limits: **100 MiB** expanded ZIP, **100** archive entries, **10,000** features, and **500,000** coordinate positions. XML entities/DTDs, archive traversal, links, duplicate entries, and encrypted ZIP entries are rejected. Limits are centralized in `app/config.py`.

## API

| Method | Endpoint        | Purpose                                                                                            |
| ------ | --------------- | -------------------------------------------------------------------------------------------------- |
| POST   | `/api/process/` | Process multipart field `file`; return metadata, all measurements, and a WGS84 map in one response |
| GET    | `/api/config/`  | Effective upload/result limits, supported extensions, and storage mode                             |
| GET    | `/health`       | Readiness; also checks the database when one is enabled                                            |
| GET    | `/example.kml`  | Download the bundled sample                                                                        |
| GET    | `/docs`         | Interactive OpenAPI documentation                                                                  |

Process a sample against a running local app:

```sh
curl -fsS -F 'file=@examples/survey.kml' \
  http://127.0.0.1:8000/api/process/ -o measurements.json
```

Successful processing returns HTTP 200 with this shape:

```json
{
  "file": {
    "id": "generated-uuid",
    "filename": "survey.kml",
    "feature_count": 3,
    "crs": "EPSG:4326",
    "status": "COMPLETED",
    "size_bytes": 864,
    "created_at": "2026-10-11T00:00:00Z",
    "measured_feature_count": 2,
    "invalid_feature_count": 0
  },
  "features": [],
  "map": {
    "type": "FeatureCollection",
    "file_id": "generated-uuid",
    "features": []
  }
}
```

The two `features` arrays are omitted here for readability; real responses contain processed features. Each measurement feature includes its source geometry, attributes, source CRS, status, measurement, and measurement CRS. IDs, timestamps, and sizes vary. `COMPLETED` indicates parsing finished, not that every feature has a valid measurement.

Errors use `{"detail":"..."}`: **413** for upload/result/processing limits, **415** for unsupported extensions, and **422** for malformed input. Request validation can return a structured detail list.

### Optional persistent storage

You do not need this section for the Vercel test app. Persistence is available if you later need saved files and bookmarkable result URLs.

| Environment variable            | Meaning                                                    |
| ------------------------------- | ---------------------------------------------------------- |
| `GEO_STORAGE_MODE`              | `auto` (default), `none`, `sqlite`, or `postgres`          |
| `DATABASE_URL` / `POSTGRES_URL` | PostgreSQL connection URL; `DATABASE_URL` takes precedence |
| `GEO_DATA_DIR`                  | Local SQLite directory; default `data`                     |
| `VERCEL`                        | Vercel supplies `1`; enables hosted size limits            |

In `auto` mode, a database URL selects PostgreSQL. Without one, Vercel uses `none` and local development uses SQLite. Explicit `none` ignores database URLs. SQLite is rejected on Vercel because its filesystem does not provide reliable shared persistence.

For PostgreSQL, configure the URL securely in the host's environment settings and select `postgres` or `auto`. For a Vercel deployment, also change `env.GEO_STORAGE_MODE` in `vercel.json` from `none` to `postgres` or `auto`, then redeploy. The role needs permission to create and read/write the `geo_files` table. Remote connections verify certificates and hostnames using public CA roots; only loopback test connections allow unencrypted traffic.

Persistent modes additionally support:

| Method | Endpoint                                           | Purpose                                                 |
| ------ | -------------------------------------------------- | ------------------------------------------------------- |
| POST   | `/api/files/`                                      | Process and store a file; return metadata with HTTP 201 |
| GET    | `/api/files/{id}/`                                 | Retrieve metadata                                       |
| GET    | `/api/files/{id}/measurements/?offset=0&limit=100` | Retrieve measurements; page limit 1–1000                |
| GET    | `/api/files/{id}/map/?offset=0&limit=100`          | Retrieve a WGS84 map page                               |

These storage endpoints return **410** in `none` mode; use `/api/process/` instead. Unknown file IDs return **404** in persistent modes. `/api/process/` never creates a stored record, even when a database is configured.

## Development and checks

Run the API and static checks:

```sh
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/build_frontend.py --check
```

Tests cover independent metric references, projected metre/foot CRSs, polygon holes, multipart geometries, Shapefile altitude preservation, DBF dates and character encodings, archive safety, invalid files, limits, stateless processing, and SQLite persistence. The optional PostgreSQL integration test is skipped unless `GEO_TEST_DATABASE_URL` points to a disposable test database. CI supplies one; local testing does not require it.

Deployment preparation was validated with Vercel's `@vercel/python` builder 24.0.1. It produced a Python 3.12 function with the frontend and sample included. The extracted function's Vercel handler also passed startup, frontend, KML, and projected Shapefile checks in the official Amazon Lambda Python 3.12 image with a read-only filesystem and only its bundled dependencies. Fiona/GDAL is excluded from that bundle, avoiding the missing `libexpat.so.1` startup failure. Browser checks cover the app's stateless deployment flow. A hosted preview still requires deployment through your Vercel account.

For browser checks:

```sh
uv sync --locked --group browser
# Install Chromium only if a system Chromium is unavailable:
uv run --locked --group browser playwright install chromium
GEO_STORAGE_MODE=none VERCEL=1 uv run --locked --group browser python scripts/browser_smoke.py
```

The script starts and stops its own local API. It checks uploads, maps, inspection, search/filtering, pagination, exports, mobile layout, safe text rendering, session restore, unavailable browser storage, and unavailable basemap tiles. In stateless mode it blocks calls to stored-file endpoints and verifies that no SQLite directory is created. Use the PowerShell environment-variable syntax shown above on Windows.

### Editing the frontend

Edit `web/index.template.html`, `web/styles.css`, or `web/app.js`, then regenerate:

```sh
uv run --locked python scripts/build_frontend.py
```

`web/index.html` is generated and self-contained: CSS, JavaScript, Leaflet 1.9.4, its BSD license, and images are embedded. Commit the regenerated file with your changes. CI checks that it matches the sources. HTML responses use `Cache-Control: no-store`.

## Project layout

```text
app/
  main.py             API, frontend routes, and upload lifecycle
  config.py           Storage selection and processing limits
  parsers.py          Bounded KML and Shapefile parsing
  measurements.py     Geometry validation and metric projections
  map_data.py         WGS84 geometry for display
  storage.py          Optional SQLite and PostgreSQL persistence
  schemas.py          Typed response contracts
web/                  Frontend source, generated page, and Leaflet assets
examples/survey.kml   Bundled polygon, line, and point
scripts/              Frontend generator, browser checks, and ZIP packaging
tests/                API and optional hosted-storage tests
vercel.json           Native FastAPI deployment configuration
pyproject.toml        Dependencies and Vercel entrypoint
uv.lock               Locked dependency versions
```

## Troubleshooting

| Symptom                                  | Check                                                                                            |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Page opens but uploads are unavailable   | Use the running app address, not a static HTML preview                                           |
| Vercel connects to an old database       | Check that `vercel.json` contains `env.GEO_STORAGE_MODE` set to `none`, then redeploy            |
| Upload gets HTTP 413                     | Check the 3 MiB hosted upload limit; expanded results also have a 4 MiB limit                    |
| Result disappears after refresh          | Browser storage may be blocked or full; download JSON before refreshing                          |
| Result is missing in another tab/browser | Results belong to the original tab; share JSON or enable persistence                             |
| Street tiles are blank                   | Disable Street basemap; feature outlines work independently                                      |
| Vercel build fails installing libraries  | Confirm Python 3.12, the complete lockfile, the correct root, and default FastAPI build settings |
| `ImportError: libexpat.so.1` from Fiona   | Redeploy the latest commit. Production now uses PyShp and excludes Fiona/GDAL; redeploy without the build cache if the old dependency remains |

This is a test/demo application with synchronous processing. Production use would need authentication, access controls, rate limiting, retention policies, and appropriate processing resource limits.
