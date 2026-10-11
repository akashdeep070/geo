"""Project each feature into a locally centered metric CRS before measuring."""

import math

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely import get_coordinates, transform
from shapely.errors import GEOSException
from shapely.geometry import mapping, shape


def measure(index: int, feature_id: str, geometry: dict | None, properties: dict, crs: CRS):
    result = {
        "index": index,
        "feature_id": feature_id,
        "geometry_type": geometry.get("type") if geometry else None,
        "geometry": geometry,
        "crs": crs.to_string(),
        "properties": properties,
        "measurement": None,
        "measurement_crs": None,
        "status": "NO_MEASUREMENT",
        "message": None,
    }
    if geometry is None:
        result.update(status="EMPTY", message="Feature has no geometry.")
        return result
    if geometry.get("type") not in {
        "Polygon",
        "MultiPolygon",
        "LineString",
        "MultiLineString",
        "Point",
        "MultiPoint",
    }:
        result.update(status="UNSUPPORTED", message="Geometry type has no measurement support.")
        return result
    try:
        source = shape(geometry)
        coordinates = get_coordinates(source, include_z=True)
        # A missing Z is represented as NaN; only XY participates in measurement.
        if not np.isfinite(coordinates[:, :2]).all():
            raise ValueError("Geometry contains non-finite coordinates.")
        result["geometry"] = mapping(source)
        if source.is_empty:
            result.update(status="EMPTY", message="Feature geometry is empty.")
            return result
        if not source.is_valid:
            raise ValueError("Geometry is invalid; repair it before calculating measurements.")
        if source.geom_type in {"Point", "MultiPoint"}:
            return result
        geographic = transform(
            source, Transformer.from_crs(crs, 4326, always_xy=True).transform, interleaved=False
        )
        xy = get_coordinates(geographic)
        if not np.isfinite(xy).all() or np.any(np.abs(xy[:, 1]) > 90):
            raise ValueError("Coordinates cannot be transformed to valid longitude/latitude.")
        # Circular mean avoids centering a dateline-crossing feature at Greenwich.
        radians = np.radians(xy[:, 0])
        longitude = math.degrees(math.atan2(np.sin(radians).mean(), np.cos(radians).mean()))
        latitude = float(xy[:, 1].mean())
        area = source.geom_type in {"Polygon", "MultiPolygon"}
        projection = "laea" if area else "aeqd"
        local_crs = CRS.from_proj4(
            f"+proj={projection} +lat_0={latitude} +lon_0={longitude} "
            "+datum=WGS84 +units=m +no_defs"
        )
        projected = transform(
            geographic,
            Transformer.from_crs(4326, local_crs, always_xy=True).transform,
            interleaved=False,
        )
        value = projected.area if area else projected.length
        if not math.isfinite(value):
            raise ValueError("Feature is outside the usable extent of its local projection.")
        result.update(
            status="MEASURED",
            measurement_crs=local_crs.to_string(),
            measurement={
                "kind": "area" if area else "length",
                "value": value,
                "unit": "m2" if area else "m",
            },
        )
    except (ValueError, TypeError, OverflowError, ProjError, GEOSException) as exc:
        # Per-feature geometry failures must not abort the rest of a valid file.
        # Errors are kept generic rather than exposing native-library internals.
        result.update(
            status="INVALID",
            message=str(exc) if isinstance(exc, ValueError) else "Geometry could not be measured.",
            geometry=geometry,
        )
    return result
