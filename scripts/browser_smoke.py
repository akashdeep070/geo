"""Run the real frontend against an isolated API instance and temporary database.

Run: uv run --locked --group browser python scripts/browser_smoke.py
Uses system Chromium when installed, otherwise Playwright's installed Chromium.
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import fiona
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def wait_ready(url, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Smoke-test API exited during startup.")
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Smoke-test API did not become ready.")


def settled(page):
    expect(page.locator("#browse-button")).to_be_enabled(timeout=20000)


def run_loading_checks(browser, url):
    """Exercise HTML entry points, a path-stripping proxy and static previews."""
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def block_assets(route):
        if route.request.resource_type in {"script", "stylesheet", "image"}:
            route.abort()
        else:
            route.continue_()

    # The document must render even when its preview cannot fetch sibling assets.
    page.route("**/*", block_assets)
    for entry in ("/index.html", "/assets/index.html"):
        page.goto(url + entry, wait_until="networkidle")
        assert page.evaluate("typeof L") == "object"
        assert (
            page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--green').trim()"
            )
            == "#173f33"
        )
        page.click("#demo-button")
        expect(page.locator("#feature-count")).to_have_text("3", timeout=20000)
        settled(page)
    context.close()

    context = browser.new_context()
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content((ROOT / "web/index.html").read_text())
    assert page.evaluate("typeof L") == "object"
    assert (
        page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--green').trim()"
        )
        == "#173f33"
    )
    expect(page.locator("#feedback")).to_contain_text("open this page through the running")
    context.close()

    prefix = url + "/tools/geo/"
    context = browser.new_context()
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))

    def proxy(route):
        if route.request.url.startswith(prefix):
            response = route.fetch(url=url + "/" + route.request.url[len(prefix) :])
            route.fulfill(response=response)
        else:
            route.fulfill(status=404, body="Request escaped the application prefix.")

    # Model an external gateway that strips /tools/geo and has no app at its root.
    page.route(url + "/**", proxy)
    page.goto(prefix, wait_until="networkidle")
    page.click("#demo-button")
    expect(page.locator("#feature-count")).to_have_text("3", timeout=20000)
    settled(page)
    expect(page.locator("#map path.leaflet-interactive")).to_have_count(3)
    assert page.locator("#api-docs-link").get_attribute("href") == prefix + "docs"
    context.close()

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "web"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context()
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{server.server_port}/index.html", wait_until="networkidle")
        assert page.evaluate("typeof L") == "object"
        assert (
            page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--green').trim()"
            )
            == "#173f33"
        )
        expect(page.locator("#feedback")).to_contain_text("file processing is unavailable")
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not errors, errors

    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(url, wait_until="networkidle")
    expect(page.locator("noscript .feedback")).to_be_visible()
    expect(page.locator("noscript .feedback")).to_contain_text("does not run JavaScript")
    context.close()


def run_checks(url, directory):
    with urllib.request.urlopen(url + "/api/config/", timeout=10) as response:
        stateless = json.load(response)["storage_mode"] == "none"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("chromium") or None,
            headless=True,
            args=["--no-sandbox"],
        )
        run_loading_checks(browser, url)
        context = browser.new_context(viewport={"width": 1440, "height": 1080})
        database_requests = []
        if stateless:

            def forbid_database_request(route):
                database_requests.append(route.request.url)
                route.abort()

            context.route("**/api/files/**", forbid_database_request)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="networkidle")
        expect(page.locator("h1")).to_have_text("Your data, in perspective.")
        expect(page.locator("#map-empty")).to_be_visible()
        expect(page.locator("#street-map")).to_be_checked()
        assert page.evaluate("map.hasLayer(street)")
        expect(page.locator(".leaflet-control-attribution")).to_contain_text("OpenStreetMap")
        limit = 3 if os.getenv("VERCEL") == "1" else 20
        expect(page.locator("#upload-limit")).to_have_text(f"Up to {limit} MiB")
        if limit == 3:
            page.locator("#file-input").set_input_files(
                {
                    "name": "large.kml",
                    "mimeType": "application/xml",
                    "buffer": b" " * (3 * 1024 * 1024 + 1),
                }
            )
            expect(page.locator("#feedback")).to_contain_text("up to 3 MiB")
        page.click("#demo-button")
        expect(page.locator("#feature-count")).to_have_text("3", timeout=20000)
        settled(page)
        expect(page.locator("#feature-rows tr")).to_have_count(3)
        expect(page.locator("#map path.leaflet-interactive")).to_have_count(3)
        # Regression: global icon CSS must not shrink the map renderer SVG.
        box = page.locator("#map path.leaflet-interactive").first.bounding_box()
        map_box = page.locator("#map").bounding_box()
        assert box["width"] > 50 and box["height"] > 50
        assert map_box["x"] <= box["x"] < map_box["x"] + map_box["width"]
        page.locator("#map path.leaflet-interactive").first.click(position={"x": 30, "y": 30})
        expect(page.locator("#inspector h3")).to_have_text("Survey plot")
        page.locator("#feature-search").fill("route")
        expect(page.locator("#feature-rows tr")).to_have_count(1)
        page.locator("#geometry-filter").select_option("point")
        expect(page.locator("#table-empty")).to_be_visible()
        page.locator("#feature-search").fill("")
        expect(page.locator("#feature-rows tr")).to_have_count(1)
        page.get_by_role("button", name="Inspect Survey marker", exact=True).click()
        expect(page.locator("#inspector h3")).to_have_text("Survey marker")
        with page.expect_download() as download:
            page.click("#download-button")
        export = download.value
        assert export.suggested_filename == "survey-measurements.json"
        data = json.loads(Path(export.path()).read_text())
        assert len(data["features"]) == 3
        assert data["features"][0]["measurement"]["unit"] == "m2"
        page.locator("#geometry-filter").select_option("all")
        page.get_by_role("button", name="Inspect Survey plot", exact=True).click()
        page.click("#fit-map")
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.4)  # Allow the Leaflet selection animation to settle for screenshots.
        screenshot = os.getenv("GEO_SCREENSHOT_DIR")
        if screenshot:
            Path(screenshot).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(screenshot) / "geomeasure-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("window.scrollTo(0, 0)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        expect(page.locator("#browse-button")).to_be_visible()
        if screenshot:
            page.screenshot(path=str(Path(screenshot) / "geomeasure-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1080})

        # Malformed uploads show an actionable error while retaining previous results.
        page.locator("#file-input").set_input_files(
            {"name": "broken.kml", "mimeType": "application/xml", "buffer": b"not XML"}
        )
        expect(page.locator("#feedback")).to_have_class("feedback error")
        settled(page)
        expect(page.locator("#feature-count")).to_have_text("3")

        # Drag/drop exercises real uploads, pagination and safe attribute rendering.
        body = '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        for index in range(23):
            name = (
                '&lt;img src=x onerror="window.injected=true"&gt;'
                if index == 0
                else f"Marker {index}"
            )
            body += (
                f"<Placemark><name>{name}</name><Point><coordinates>"
                f"{77 + index * 0.001},28</coordinates></Point></Placemark>"
            )
        body += "</Document></kml>"
        page.evaluate(
            """(body) => {
            const transfer = new DataTransfer();
            transfer.items.add(new File([body], 'markers.kml', {type:'application/xml'}));
            document.getElementById('drop-zone').dispatchEvent(new DragEvent('drop', {
                bubbles: true, cancelable: true, dataTransfer: transfer
            }));
        }""",
            body,
        )
        expect(page.locator("#feature-count")).to_have_text("23", timeout=20000)
        settled(page)
        expect(page.locator("#feature-rows tr")).to_have_count(10)
        assert page.locator("#feature-rows img").count() == 0
        assert page.evaluate("window.injected !== true")
        page.click("#next-page")
        expect(page.locator("#page-indicator")).to_have_text("2 / 3")
        page.click("#next-page")
        expect(page.locator("#feature-rows tr")).to_have_count(3)
        page.click("#previous-page")
        expect(page.locator("#page-indicator")).to_have_text("2 / 3")
        if stateless:
            assert "file=" not in page.url
            page.locator("#recent-files .recent-file").filter(has_text="survey.kml").click()
            expect(page.locator("#feature-count")).to_have_text("3")
            page.locator("#recent-files .recent-file").filter(has_text="markers.kml").click()
            expect(page.locator("#feature-count")).to_have_text("23")
        page.reload(wait_until="networkidle")
        expect(page.locator("#feature-count")).to_have_text("23", timeout=20000)
        settled(page)
        expect(page.locator("#recent-files .recent-file")).to_have_count(1 if stateless else 2)

        # Projected Shapefile is displayed at geographic coordinates, not at easting/northing.
        shapefile = directory / "projected.shp"
        with fiona.open(
            shapefile,
            "w",
            driver="ESRI Shapefile",
            crs="EPSG:32643",
            schema={"geometry": "Point", "properties": {"name": "str"}},
        ) as output:
            output.write(
                {
                    "geometry": {"type": "Point", "coordinates": (500000, 3100000)},
                    "properties": {"name": "Projected marker"},
                }
            )
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            for component in directory.glob("projected.*"):
                archive.write(component, component.name)
        page.locator("#file-input").set_input_files(
            {"name": "projected.zip", "mimeType": "application/zip", "buffer": buffer.getvalue()}
        )
        expect(page.locator("#source-crs")).to_have_text("EPSG:32643", timeout=20000)
        settled(page)
        expect(page.locator("#map path.leaflet-interactive")).to_have_count(1)
        center = page.evaluate("({lat: map.getCenter().lat, lng: map.getCenter().lng})")
        assert abs(center["lng"] - 75) < 0.01 and 27 < center["lat"] < 29

        # A failed default basemap must not break feature rendering or upload feedback.
        page.locator("#street-map").uncheck()
        page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
        page.locator("#street-map").check()
        expect(page.locator("#map-status")).to_contain_text("OpenStreetMap tiles could not load")
        expect(page.locator("#feedback")).to_contain_text("projected.zip is ready")
        expect(page.locator("#map path.leaflet-interactive")).to_have_count(1)
        page.locator("#street-map").uncheck()
        expect(page.locator("#map-status")).to_be_hidden()
        assert not errors, errors
        if stateless:
            assert not database_requests, database_requests
            assert not (directory / "data").exists()
            # A separate browser has no access to another tab's result cache.
            fresh = browser.new_context()
            fresh_page = fresh.new_page()
            fresh_page.goto(url, wait_until="networkidle")
            expect(fresh_page.locator("#map-empty")).to_be_visible()
            fresh.close()

            # Browsers that block storage must still support processing and export.
            blocked = browser.new_context()
            blocked_page = blocked.new_page()
            blocked_page.goto(url, wait_until="networkidle")
            blocked_page.click("#demo-button")
            expect(blocked_page.locator("#feature-count")).to_have_text("3", timeout=20000)
            assert blocked_page.evaluate("sessionStorage.getItem('geomeasure.session.v1') !== null")
            blocked_page.evaluate(
                "() => { Storage.prototype.setItem = () => {"
                " throw new Error('Storage disabled'); }; }"
            )
            blocked_page.locator("#file-input").set_input_files(
                {"name": "markers.kml", "mimeType": "application/xml", "buffer": body.encode()}
            )
            expect(blocked_page.locator("#feature-count")).to_have_text("23", timeout=20000)
            expect(blocked_page.locator("#feedback")).to_contain_text(
                "Browser storage is unavailable"
            )
            expect(blocked_page.locator("#download-button")).to_be_enabled()
            blocked_page.reload(wait_until="networkidle")
            expect(blocked_page.locator("#map-empty")).to_be_visible()
            blocked.close()

            corrupt = browser.new_context()
            corrupt.add_init_script(
                "sessionStorage.setItem('geomeasure.session.v1', '{broken JSON');"
            )
            corrupt_page = corrupt.new_page()
            corrupt_page.goto(url, wait_until="networkidle")
            expect(corrupt_page.locator("#feedback")).to_contain_text("could not be restored")
            corrupt_page.click("#demo-button")
            expect(corrupt_page.locator("#feature-count")).to_have_text("3", timeout=20000)
            corrupt.close()
        browser.close()
    print(
        f"Browser smoke passed ({'no database' if stateless else 'persistent storage'}): "
        "sample, map, inspection, filters, export, mobile, errors, "
        "drag/drop, pagination, escaping, history, projected CRS, offline basemap."
    )


def main():
    with TemporaryDirectory(prefix="geo-browser-") as temporary:
        directory = Path(temporary)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        environment = {**os.environ, "GEO_DATA_DIR": str(directory / "data")}
        with (directory / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                wait_ready(url, process)
                run_checks(url, directory)
            except Exception:
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
