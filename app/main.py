"""HTTP API and bounded upload lifecycle."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import Error as DatabaseError
from starlette.concurrency import run_in_threadpool

from app.config import FileError, Settings
from app.map_data import map_feature
from app.parsers import process
from app.schemas import FileInformation, MeasurementsPage, ProcessedFile
from app.storage import PostgresStore, Store


class FrontendFiles(StaticFiles):
    """Keep HTML previews current while retaining normal caching for assets."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    storage_backend = settings.storage_backend

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = None
        if storage_backend == "postgres":
            if not settings.database_url:
                raise RuntimeError("PostgreSQL mode requires DATABASE_URL or POSTGRES_URL.")
            app.state.store = await run_in_threadpool(PostgresStore, settings.database_url)
        elif storage_backend == "sqlite":
            if settings.vercel:
                raise RuntimeError("SQLite is unavailable on Vercel. Use none or postgres mode.")
            app.state.store = await run_in_threadpool(Store, settings.data_dir)
        yield

    api = FastAPI(title="Geospatial File Measurement API", version="0.1.0", lifespan=lifespan)
    root = Path(__file__).resolve().parent.parent

    @api.exception_handler(DatabaseError)
    async def database_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Result storage is unavailable."})

    api.mount("/assets", FrontendFiles(directory=root / "web"), name="assets")

    @api.get("/api/config/", tags=["Configuration"])
    def configuration():
        return {
            "max_upload_bytes": settings.upload_limit,
            "max_result_bytes": settings.result_limit,
            "supported_extensions": [".kml", ".zip"],
            "storage_mode": storage_backend,
        }

    @api.get("/", include_in_schema=False)
    def frontend():
        return FileResponse(root / "web" / "index.html", headers={"Cache-Control": "no-store"})

    @api.get("/example.kml", include_in_schema=False)
    def example():
        return FileResponse(root / "examples" / "survey.kml")

    @api.get("/health", tags=["Health"])
    def health():
        if api.state.store is not None:
            with api.state.store.connect() as db:
                db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    async def process_upload(file: UploadFile):
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        extension = Path(filename).suffix.lower()
        try:
            if extension not in {".zip", ".kml"}:
                raise FileError("Upload a .zip Shapefile or a .kml file.", 415)
            size = 0
            with TemporaryDirectory(prefix="geo-upload-") as temporary:
                path = Path(temporary) / ("upload" + extension)
                with path.open("wb") as target:
                    while chunk := await file.read(64 * 1024):
                        size += len(chunk)
                        if size > settings.upload_limit:
                            raise FileError("File exceeds the upload size limit.", 413)
                        target.write(chunk)
                if size == 0:
                    raise FileError("Uploaded file is empty.")
                crs, features = await run_in_threadpool(process, path, extension, settings)
            information = {
                "id": str(uuid4()),
                "filename": filename,
                "feature_count": len(features),
                "crs": crs,
                "status": "COMPLETED",
                "size_bytes": size,
                "created_at": datetime.now(UTC).isoformat(),
                "measured_feature_count": sum(f["status"] == "MEASURED" for f in features),
                "invalid_feature_count": sum(f["status"] == "INVALID" for f in features),
            }
            return information, features
        except FileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    def require_store():
        if api.state.store is None:
            raise HTTPException(
                status_code=410,
                detail="Server storage is disabled. Upload to /api/process/ for inline results.",
            )
        return api.state.store

    @api.post("/api/process/", tags=["Processing"], response_model=ProcessedFile)
    async def process_inline(file: UploadFile = File(...)):
        """Process without storing anything; return all results in this response."""
        try:
            information, features = await process_upload(file)

            def build_response():
                data = ProcessedFile(
                    file=information,
                    features=features,
                    map={
                        "type": "FeatureCollection",
                        "file_id": information["id"],
                        "features": [map_feature(feature) for feature in features],
                    },
                ).model_dump(mode="json")
                response = JSONResponse(content=data, headers={"Cache-Control": "no-store"})
                if settings.result_limit and len(response.body) > settings.result_limit:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Processed results exceed the response size limit. Use a smaller file."
                        ),
                    )
                return response

            return await run_in_threadpool(build_response)
        finally:
            await file.close()

    @api.post("/api/files/", status_code=201, tags=["Files"], response_model=FileInformation)
    async def upload(file: UploadFile = File(...)):
        try:
            store = require_store()
            information, features = await process_upload(file)
            await run_in_threadpool(store.save, information, features)
            return information
        finally:
            await file.close()

    def lookup(file_id):
        record = require_store().get(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found.")
        return record

    @api.get("/api/files/{file_id}/", tags=["Files"], response_model=FileInformation)
    def information(file_id: str):
        return lookup(file_id)[0]

    @api.get(
        "/api/files/{file_id}/measurements/", tags=["Measurements"], response_model=MeasurementsPage
    )
    def measurements(
        file_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
    ):
        info, features = lookup(file_id)
        return {
            "file_id": info["id"],
            "total": len(features),
            "offset": offset,
            "limit": limit,
            "features": features[offset : offset + limit],
        }

    @api.get("/api/files/{file_id}/map/", tags=["Measurements"])
    def map_data(
        file_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
    ):
        info, features = lookup(file_id)
        return {
            "type": "FeatureCollection",
            "file_id": info["id"],
            "total": len(features),
            "offset": offset,
            "limit": limit,
            "features": [map_feature(feature) for feature in features[offset : offset + limit]],
        }

    # Keep API routes ahead of the frontend fallback. Relative asset paths work
    # at the server root, at /index.html, and behind a mounted URL prefix.
    api.mount("/", FrontendFiles(directory=root / "web", html=True), name="frontend")

    return api


app = create_app()
