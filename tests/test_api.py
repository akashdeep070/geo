import io
import json
import math
from zipfile import ZIP_DEFLATED, ZipFile

import fiona
import pytest
from fastapi.testclient import TestClient
from pyproj import Geod

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data"))) as test_client:
        yield test_client


def kml(body):
    return (
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2">'
        "<Document>" + body + "</Document></kml>"
    ).encode()


def placemark(geometry, attributes=""):
    return f'<Placemark id="survey"><name>Survey</name>{attributes}{geometry}</Placemark>'


def upload_kml(client, body):
    return client.post("/api/files/", files={"file": ("survey.kml", kml(body))})


def results(client, response):
    assert response.status_code == 201, response.text
    return client.get(f"/api/files/{response.json()['id']}/measurements/").json()["features"]


def archive_dataset(tmp_path, geometry, crs="EPSG:4326", omit=(), schema_geometry=None):
    directory = tmp_path / "shape"
    directory.mkdir(exist_ok=True)
    path = directory / "survey.shp"
    with fiona.open(
        path,
        "w",
        driver="ESRI Shapefile",
        crs=crs,
        schema={"geometry": schema_geometry or geometry["type"], "properties": {"name": "str"}},
    ) as out:
        out.write({"geometry": geometry, "properties": {"name": "test survey"}})
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for component in directory.iterdir():
            if component.suffix not in omit:
                archive.write(component, f"nested/{component.name}")
    return buffer.getvalue()


def test_geographic_line_in_metres_and_metadata(client):
    response = upload_kml(
        client,
        placemark(
            "<LineString><coordinates>77,28 77.01,28</coordinates></LineString>",
            '<ExtendedData><Data name="owner"><value>Ada</value></Data></ExtendedData>',
        ),
    )
    feature = results(client, response)[0]
    expected = Geod(ellps="WGS84").inv(77, 28, 77.01, 28)[2]
    assert feature["measurement"]["value"] == pytest.approx(expected, rel=1e-5)
    assert feature["measurement"]["unit"] == "m"
    assert feature["feature_id"] == "survey"
    assert feature["properties"]["owner"] == "Ada"
    assert feature["crs"] == "EPSG:4326"
    assert "+proj=aeqd" in feature["measurement_crs"]
    info = client.get(f"/api/files/{response.json()['id']}/")
    assert info.json() == response.json()
    assert info.json()["status"] == "COMPLETED"


def test_polygon_area_and_hole(client):
    outer = "77,28 77.01,28 77.01,28.01 77,28.01 77,28"
    inner = "77.002,28.002 77.002,28.008 77.008,28.008 77.008,28.002 77.002,28.002"
    geometry = (
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        + outer
        + "</coordinates></LinearRing></outerBoundaryIs><innerBoundaryIs>"
        "<LinearRing><coordinates>"
        + inner
        + "</coordinates></LinearRing></innerBoundaryIs></Polygon>"
    )
    feature = results(client, upload_kml(client, placemark(geometry)))[0]
    geod = Geod(ellps="WGS84")

    def ring_area(text):
        points = [tuple(map(float, part.split(","))) for part in text.split()]
        return abs(geod.polygon_area_perimeter(*zip(*points))[0])

    expected = ring_area(outer) - ring_area(inner)
    assert feature["measurement"]["value"] == pytest.approx(expected, rel=1e-4)
    assert feature["measurement"]["unit"] == "m2"
    assert "+proj=laea" in feature["measurement_crs"]


def test_point_multi_and_unsupported_collection(client):
    point = "<Point><coordinates>77,28,100</coordinates></Point>"
    line = "<LineString><coordinates>77,28 77.01,28</coordinates></LineString>"
    body = (
        placemark(point)
        + placemark("<MultiGeometry>" + line * 2 + "</MultiGeometry>")
        + placemark("<MultiGeometry>" + point + line + "</MultiGeometry>")
    )
    features = results(client, upload_kml(client, body))
    assert [f["status"] for f in features] == ["NO_MEASUREMENT", "MEASURED", "UNSUPPORTED"]
    assert features[0]["measurement"] is None
    assert features[0]["geometry"]["coordinates"] == [77, 28, 100]
    assert features[1]["geometry_type"] == "MultiLineString"


def test_invalid_geometry_is_reported_per_feature(client):
    geometry = (
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        "0,0 1,1 1,0 0,1 0,0</coordinates></LinearRing></outerBoundaryIs></Polygon>"
    )
    body = placemark(geometry) + placemark("<Point><coordinates>0,0</coordinates></Point>")
    response = upload_kml(client, body)
    features = results(client, response)
    assert features[0]["status"] == "INVALID"
    assert features[0]["measurement"] is None
    assert features[1]["status"] == "NO_MEASUREMENT"
    assert response.json()["invalid_feature_count"] == 1


@pytest.mark.parametrize(
    "crs,coordinates,expected",
    [
        ("EPSG:4326", [(0, 0), (0.01, 0)], 1113.1949),
        ("EPSG:32643", [(500000, 3100000), (501000, 3100000)], 1000.4),
        ("EPSG:2263", [(1000000, 200000), (1001000, 200000)], 304.8),
    ],
)
def test_shapefile_crs_units(client, tmp_path, crs, coordinates, expected):
    content = archive_dataset(tmp_path, {"type": "LineString", "coordinates": coordinates}, crs)
    response = client.post("/api/files/", files={"file": ("survey.zip", content)})
    feature = results(client, response)[0]
    assert feature["measurement"]["value"] == pytest.approx(expected, rel=0.001)
    assert feature["properties"]["name"] == "test survey"
    assert feature["crs"] == crs


def test_shapefile_missing_crs(client, tmp_path):
    content = archive_dataset(tmp_path, {"type": "Point", "coordinates": (0, 0)}, omit=(".prj",))
    response = client.post("/api/files/", files={"file": ("survey.zip", content)})
    assert response.status_code == 422
    assert ".prj" in response.json()["detail"]


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": (77, 28, 100)},
        {"type": "LineString", "coordinates": [(77, 28, 100), (77.01, 28, 500)]},
    ],
)
def test_shapefile_z_is_retained_and_measurements_are_horizontal(client, tmp_path, geometry):
    content = archive_dataset(tmp_path, geometry, schema_geometry="3D " + geometry["type"])
    response = client.post("/api/files/", files={"file": ("survey.zip", content)})
    feature = results(client, response)[0]
    assert feature["geometry"]["coordinates"] == json.loads(json.dumps(geometry["coordinates"]))
    if geometry["type"] == "LineString":
        expected = Geod(ellps="WGS84").inv(77, 28, 77.01, 28)[2]
        assert feature["measurement"]["value"] == pytest.approx(expected, rel=1e-5)


@pytest.mark.parametrize("cpg", ["CP1252", "1252"])
def test_shapefile_dbf_encoding_dates_and_null_geometry(client, tmp_path, cpg):
    directory = tmp_path / "encoded"
    directory.mkdir()
    with fiona.open(
        directory / "survey.shp",
        "w",
        driver="ESRI Shapefile",
        crs="EPSG:4326",
        encoding="CP1252",
        schema={"geometry": "Point", "properties": {"name": "str", "survey_day": "date"}},
    ) as source:
        source.write({"geometry": None, "properties": {"name": "Café", "survey_day": "2026-10-11"}})
    (directory / "survey.cpg").write_text(cpg)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for component in directory.iterdir():
            archive.write(component, component.name)
    response = client.post("/api/files/", files={"file": ("survey.zip", buffer.getvalue())})
    feature = results(client, response)[0]
    assert feature["geometry"] is None
    assert feature["status"] == "EMPTY"
    assert feature["properties"] == {"name": "Café", "survey_day": "2026-10-11"}


def test_shapefile_polygon_hole_is_preserved(client, tmp_path):
    from shapely.geometry import shape

    geometry = {
        "type": "Polygon",
        "coordinates": [
            [(77, 28), (77.01, 28), (77.01, 28.01), (77, 28.01), (77, 28)],
            [
                (77.002, 28.002),
                (77.002, 28.008),
                (77.008, 28.008),
                (77.008, 28.002),
                (77.002, 28.002),
            ],
        ],
    }
    content = archive_dataset(tmp_path, geometry)
    response = client.post("/api/files/", files={"file": ("survey.zip", content)})
    feature = results(client, response)[0]
    assert len(feature["geometry"]["coordinates"]) == 2
    expected = abs(Geod(ellps="WGS84").geometry_area_perimeter(shape(geometry))[0])
    assert feature["measurement"]["value"] == pytest.approx(expected, rel=1e-4)


def test_shapefile_truncated_binary_is_rejected(client, tmp_path):
    valid = archive_dataset(tmp_path, {"type": "Point", "coordinates": (77, 28)})
    buffer = io.BytesIO()
    with ZipFile(io.BytesIO(valid)) as source, ZipFile(buffer, "w") as output:
        for name in source.namelist():
            content = source.read(name)
            output.writestr(name, content[:10] if name.endswith(".shp") else content)
    response = client.post("/api/files/", files={"file": ("broken.zip", buffer.getvalue())})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        b"not XML",
        b'<!DOCTYPE kml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><kml>&xxe;</kml>',
        kml("<Placemark><Point><coordinates>nan,0</coordinates></Point></Placemark>"),
        kml("<Placemark><Point><coordinates>181,0</coordinates></Point></Placemark>"),
        kml("<Placemark><LineString><coordinates>0,0</coordinates></LineString></Placemark>"),
        kml(""),
    ],
)
def test_reject_bad_kml(client, body):
    response = client.post("/api/files/", files={"file": ("bad.kml", body)})
    assert response.status_code == 422


@pytest.mark.parametrize("filename", ["../escape.shp", "/escape.shp", "folder\\escape.shp"])
def test_unsafe_archive(client, filename):
    content = io.BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(filename, b"bad")
    response = client.post("/api/files/", files={"file": ("bad.zip", content.getvalue())})
    assert response.status_code == 422
    assert "unsafe" in response.json()["detail"]


def test_bad_file_types_and_missing_file(client):
    assert client.post("/api/files/", files={"file": ("bad.zip", b"bad")}).status_code == 422
    assert client.post("/api/files/", files={"file": ("bad.txt", b"bad")}).status_code == 415
    assert client.post("/api/files/", files={"file": ("bad.kml", b"")}).status_code == 422
    assert client.post("/api/files/").status_code == 422
    assert client.get("/api/files/nonexistent/").status_code == 404
    assert client.get("/api/files/nonexistent/measurements/").status_code == 404


def test_limits_and_pagination(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        max_upload_bytes=1024,
        max_features=2,
        max_uncompressed_bytes=10,
        max_coordinates=2,
    )
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/api/files/", files={"file": ("big.kml", b"x" * 1025)}).status_code == 413
        )
        point = placemark("<Point><coordinates>0,0</coordinates></Point>")
        assert upload_kml(client, point * 3).status_code == 413
        response = upload_kml(client, point * 2)
        assert response.status_code == 201
        url = f"/api/files/{response.json()['id']}/measurements/"
        page = client.get(url, params={"offset": 1, "limit": 1}).json()
        assert page["total"] == 2 and len(page["features"]) == 1
        assert page["features"][0]["index"] == 1
        assert client.get(url, params={"limit": 0}).status_code == 422
        assert client.get(url, params={"offset": -1}).status_code == 422
        content = io.BytesIO()
        with ZipFile(content, "w", ZIP_DEFLATED) as archive:
            archive.writestr("survey.shp", b"a" * 11)
        assert (
            client.post("/api/files/", files={"file": ("big.zip", content.getvalue())}).status_code
            == 413
        )


def test_persistence_across_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        response = upload_kml(client, placemark("<Point><coordinates>0,0</coordinates></Point>"))
        file_id = response.json()["id"]
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get(f"/api/files/{file_id}/").json() == response.json()
        assert client.get(f"/api/files/{file_id}/measurements/").json()["total"] == 1


def test_dateline_line(client):
    body = placemark("<LineString><coordinates>179.99,0 -179.99,0</coordinates></LineString>")
    feature = results(client, upload_kml(client, body))[0]
    expected = Geod(ellps="WGS84").inv(179.99, 0, -179.99, 0)[2]
    assert math.isfinite(feature["measurement"]["value"])
    assert feature["measurement"]["value"] == pytest.approx(expected, rel=1e-4)


def test_unmeasured_kml_model_preserves_source(client):
    features = results(
        client,
        upload_kml(
            client,
            placemark(
                "<Model><Location><longitude>77</longitude><latitude>28</latitude>"
                "</Location></Model>"
            ),
        ),
    )
    assert features[0]["geometry_type"] == "Model"
    assert features[0]["status"] == "UNSUPPORTED"
    assert "longitude" in features[0]["geometry"]["source_kml"]
    assert features[0]["measurement"] is None


def test_coordinate_limit_and_failed_upload_atomicity(tmp_path):
    settings = Settings(data_dir=tmp_path / "db", max_coordinates=1)
    api = create_app(settings)
    with TestClient(api) as client:
        response = upload_kml(
            client, placemark("<LineString><coordinates>0,0 0.01,0</coordinates></LineString>")
        )
        assert response.status_code == 413
        with api.state.store.connect() as db:
            assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0


def test_archive_entry_limit_and_symlink(tmp_path):
    from zipfile import ZipInfo

    settings = Settings(data_dir=tmp_path / "db", max_archive_entries=1)
    with TestClient(create_app(settings)) as client:
        content = io.BytesIO()
        with ZipFile(content, "w") as archive:
            archive.writestr("one", b"1")
            archive.writestr("two", b"2")
        response = client.post("/api/files/", files={"file": ("bad.zip", content.getvalue())})
        assert response.status_code == 413
        content = io.BytesIO()
        with ZipFile(content, "w") as archive:
            info = ZipInfo("survey.shp")
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/passwd")
        response = client.post("/api/files/", files={"file": ("bad.zip", content.getvalue())})
        assert response.status_code == 422
        assert "unsafe" in response.json()["detail"]


def test_geographic_shapefile_polygon_and_multipolygon(client, tmp_path):
    from shapely.geometry import shape

    polygon = {
        "type": "Polygon",
        "coordinates": [[(77, 28), (77.01, 28), (77.01, 28.01), (77, 28.01), (77, 28)]],
    }
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [
            polygon["coordinates"],
            [[(77.02, 28), (77.03, 28), (77.03, 28.01), (77.02, 28.01), (77.02, 28)]],
        ],
    }
    content = archive_dataset(tmp_path, geometry)
    response = client.post("/api/files/", files={"file": ("survey.zip", content)})
    feature = results(client, response)[0]
    expected = abs(Geod(ellps="WGS84").geometry_area_perimeter(shape(geometry))[0])
    assert feature["geometry_type"] == "MultiPolygon"
    assert feature["measurement"]["value"] == pytest.approx(expected, rel=1e-4)


def test_frontend_assets_and_demo(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "GeoMeasure" in response.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/assets/vendor/leaflet/leaflet.js").status_code == 200
    demo = client.get("/example.kml")
    assert demo.status_code == 200
    response = client.post("/api/files/", files={"file": ("survey.kml", demo.content)})
    features = results(client, response)
    assert len(features) == 3


def test_map_projected_coordinates_are_wgs84(client, tmp_path):
    from pyproj import Transformer

    content = archive_dataset(
        tmp_path, {"type": "Point", "coordinates": (500000, 3100000)}, "EPSG:32643"
    )
    response = client.post("/api/files/", files={"file": ("projected.zip", content)})
    assert response.status_code == 201
    file_id = response.json()["id"]
    page = client.get(f"/api/files/{file_id}/map/").json()
    assert page["type"] == "FeatureCollection"
    assert page["features"][0]["geometry"]["coordinates"] == pytest.approx(
        Transformer.from_crs(32643, 4326, always_xy=True).transform(500000, 3100000)
    )
    source = client.get(f"/api/files/{file_id}/measurements/").json()["features"][0]
    assert source["geometry"]["coordinates"] == [500000, 3100000]
    assert client.get(f"/api/files/{file_id}/map/?offset=-1").status_code == 422
    assert client.get("/api/files/missing/map/").status_code == 404


def test_inline_projected_shapefile_preserves_source_and_maps_wgs84(tmp_path):
    content = archive_dataset(
        tmp_path, {"type": "Point", "coordinates": (500000, 3100000)}, "EPSG:32643"
    )
    settings = Settings(storage_mode="none", database_url=None, vercel=True)
    with TestClient(create_app(settings)) as api:
        response = api.post("/api/process/", files={"file": ("survey.zip", content)})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["file"]["crs"] == "EPSG:32643"
        assert result["features"][0]["geometry"]["coordinates"] == [500000, 3100000]
        assert result["features"][0]["status"] == "NO_MEASUREMENT"
        mapped = result["map"]["features"][0]["geometry"]["coordinates"]
        assert mapped[0] == pytest.approx(75)
        assert 27 < mapped[1] < 29


def test_unsupported_geometry_does_not_break_map(client):
    response = upload_kml(client, placemark("<Model/>"))
    assert response.status_code == 201
    page = client.get(f"/api/files/{response.json()['id']}/map/").json()
    assert page["features"][0]["geometry"] is None
    assert page["features"][0]["properties"]["map_error"]


def test_relative_frontend_assets_and_mounted_prefix(tmp_path):
    from fastapi import FastAPI

    child = create_app(Settings(data_dir=tmp_path / "mounted"))
    with TestClient(child):
        parent = FastAPI()
        parent.mount("/tools/geo", child)
        with TestClient(parent) as mounted:
            for entry in ("/tools/geo/", "/tools/geo/index.html", "/tools/geo/assets/index.html"):
                response = mounted.get(entry)
                assert response.status_code == 200
                assert '<style data-source="styles.css">' in response.text
                assert '<script data-source="app.js">' in response.text
                assert "<script defer src=" not in response.text
                assert '<link rel="stylesheet"' not in response.text
                assert response.headers["cache-control"] == "no-store"
            for asset in ("styles.css", "app.js", "vendor/leaflet/leaflet.js"):
                response = mounted.get("/tools/geo/" + asset)
                assert response.status_code == 200
            assert mounted.get("/tools/geo/health").json() == {"status": "ok"}
            demo = mounted.get("/tools/geo/example.kml")
            response = mounted.post(
                "/tools/geo/api/files/", files={"file": ("survey.kml", demo.content)}
            )
            assert response.status_code == 201
            file_id = response.json()["id"]
            mapped = mounted.get(f"/tools/geo/api/files/{file_id}/map/")
            assert len(mapped.json()["features"]) == 3
