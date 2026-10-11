"""Response contracts used by validation and generated OpenAPI documentation."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class FileInformation(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str
    status: Literal["COMPLETED"]
    size_bytes: int
    created_at: datetime
    measured_feature_count: int
    invalid_feature_count: int


class Measurement(BaseModel):
    kind: Literal["area", "length"]
    value: float
    unit: Literal["m2", "m"]


class FeatureResult(BaseModel):
    index: int
    feature_id: str
    geometry_type: str | None
    geometry: dict[str, Any] | None
    crs: str
    properties: dict[str, Any]
    measurement: Measurement | None
    measurement_crs: str | None
    status: Literal["MEASURED", "NO_MEASUREMENT", "UNSUPPORTED", "INVALID", "EMPTY"]
    message: str | None


class MeasurementsPage(BaseModel):
    file_id: str
    total: int
    offset: int
    limit: int
    features: list[FeatureResult]


class ProcessedFile(BaseModel):
    file: FileInformation
    features: list[FeatureResult]
    map: dict[str, Any]
