"""Hosted limits and durable results across separate API instances."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_vercel_defaults_to_no_database(tmp_path):
    settings = Settings(vercel=True, database_url=None, data_dir=tmp_path / "uncreated")
    with TestClient(create_app(settings)) as api:
        assert api.app.state.store is None
        assert api.get("/health").json() == {"status": "ok"}
        config = api.get("/api/config/").json()
        assert config["storage_mode"] == "none"
        assert config["max_upload_bytes"] == 3 * 1024 * 1024
        assert config["max_result_bytes"] == 4 * 1024 * 1024
        response = api.post(
            "/api/process/",
            files={"file": ("survey.kml", Path("examples/survey.kml").read_bytes())},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["file"]["feature_count"] == 3
        assert [f["measurement"]["kind"] for f in result["features"] if f["measurement"]] == [
            "area",
            "length",
        ]
        assert result["map"]["type"] == "FeatureCollection"
        assert len(result["map"]["features"]) == 3
        assert response.headers["cache-control"] == "no-store"
        file_id = result["file"]["id"]
        for path in (
            f"/api/files/{file_id}/",
            f"/api/files/{file_id}/measurements/",
            f"/api/files/{file_id}/map/",
        ):
            assert api.get(path).status_code == 410
        assert api.post("/api/files/", files={"file": ("survey.kml", b"x")}).status_code == 410
    # A fresh instance needs no prior server state to process another file.
    with TestClient(create_app(settings)) as api:
        assert api.get(f"/api/files/{file_id}/").status_code == 410
        assert (
            api.post(
                "/api/process/",
                files={"file": ("survey.kml", Path("examples/survey.kml").read_bytes())},
            ).status_code
            == 200
        )
    assert not settings.data_dir.exists()


def test_explicit_no_database_ignores_database_url(tmp_path, monkeypatch):
    def unexpected_store(*args, **kwargs):
        pytest.fail("No database should be initialized in none mode")

    monkeypatch.setattr("app.main.Store", unexpected_store)
    monkeypatch.setattr("app.main.PostgresStore", unexpected_store)
    settings = Settings(
        storage_mode="none",
        database_url="postgresql://unused.invalid/geo",
        data_dir=tmp_path / "absent",
    )
    with TestClient(create_app(settings)) as api:
        assert api.get("/health").status_code == 200
    assert not settings.data_dir.exists()


@pytest.mark.parametrize(
    "filename, content, expected",
    [
        ("bad.kml", b"not XML", 422),
        ("bad.zip", b"bad ZIP", 422),
        ("bad.txt", b"x", 415),
        ("empty.kml", b"", 422),
        ("large.kml", b" " * (3 * 1024 * 1024 + 1), 413),
    ],
)
def test_inline_upload_rejects_invalid_inputs(filename, content, expected):
    with TestClient(create_app(Settings(vercel=True, database_url=None))) as api:
        response = api.post("/api/process/", files={"file": (filename, content)})
        assert response.status_code == expected, response.text


def test_inline_response_limit_accounts_for_expanded_geometry():
    # Compact source coordinates become floats in both source and map geometry.
    content = (
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString><coordinates>'
        + "77,28 " * 200_000
        + "</coordinates></LineString></Placemark></kml>"
    ).encode()
    assert len(content) < 3 * 1024 * 1024
    with TestClient(create_app(Settings(vercel=True, database_url=None))) as api:
        response = api.post("/api/process/", files={"file": ("dense.kml", content)})
        assert response.status_code == 413
        assert "response size limit" in response.json()["detail"]


def test_invalid_storage_configuration_is_actionable():
    with pytest.raises(ValueError, match="GEO_STORAGE_MODE"):
        create_app(Settings(storage_mode="unknown"))
    with pytest.raises(RuntimeError, match="SQLite is unavailable on Vercel"):
        with TestClient(create_app(Settings(vercel=True, storage_mode="sqlite"))):
            pass
    with pytest.raises(RuntimeError, match="PostgreSQL mode requires"):
        with TestClient(create_app(Settings(storage_mode="postgres", database_url=None))):
            pass


def test_settings_read_environment_at_creation(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/geo")
    settings = Settings()
    assert settings.upload_limit == 3 * 1024 * 1024
    assert settings.database_url == "postgresql://example.invalid/geo"
    assert "example.invalid" not in repr(settings)
    assert Settings(max_upload_bytes=128).upload_limit == 128


def test_local_configuration(tmp_path):
    with TestClient(
        create_app(Settings(data_dir=tmp_path, database_url=None, vercel=False))
    ) as api:
        assert api.get("/api/config/").json() == {
            "max_upload_bytes": 20 * 1024 * 1024,
            "max_result_bytes": None,
            "supported_extensions": [".kml", ".zip"],
            "storage_mode": "sqlite",
        }


@pytest.mark.skipif(
    not os.getenv("GEO_TEST_DATABASE_URL"), reason="No isolated test PostgreSQL URL"
)
def test_postgres_results_survive_api_restart(tmp_path):
    settings = Settings(
        database_url=os.environ["GEO_TEST_DATABASE_URL"],
        vercel=True,
        data_dir=tmp_path / "must-not-write",
    )
    file_id = None
    first_app = create_app(settings)
    try:
        with TestClient(first_app) as first:
            assert first.get("/health").json() == {"status": "ok"}
            assert first.get("/api/config/").json()["max_upload_bytes"] == 3 * 1024 * 1024
            oversized = first.post(
                "/api/files/", files={"file": ("large.kml", b" " * (3 * 1024 * 1024 + 1))}
            )
            assert oversized.status_code == 413
            sample = Path("examples/survey.kml").read_bytes()
            response = first.post("/api/files/", files={"file": ("survey.kml", sample)})
            assert response.status_code == 201, response.text
            info = response.json()
            file_id = info["id"]
            measurement_url = f"/api/files/{file_id}/measurements/"
            expected_results = first.get(measurement_url).json()
            expected_map = first.get(f"/api/files/{file_id}/map/").json()
        with TestClient(create_app(settings)) as second:
            assert second.get(f"/api/files/{file_id}/").json() == info
            assert second.get(measurement_url).json() == expected_results
            assert second.get(f"/api/files/{file_id}/map/").json() == expected_map
            assert expected_results["total"] == 3
            assert second.get("/api/files/' OR TRUE --/").status_code == 404
        assert not settings.data_dir.exists()
    finally:
        if file_id:
            with first_app.state.store.connect() as db:
                db.execute("DELETE FROM geo_files WHERE id = %s", (file_id,))
