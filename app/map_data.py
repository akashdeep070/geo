"""Create map coordinates without changing stored source geometries."""

import numpy as np
from pyproj import Transformer
from pyproj.exceptions import ProjError
from shapely import get_coordinates, transform
from shapely.errors import GeometryTypeError, GEOSException
from shapely.geometry import mapping, shape


def map_feature(feature: dict):
    geometry = None
    error = None
    source = feature["geometry"]
    if source is not None:
        try:
            projected = transform(
                shape(source),
                Transformer.from_crs(feature["crs"], 4326, always_xy=True).transform,
                interleaved=False,
            )
            coordinates = get_coordinates(projected)
            if projected.is_empty:
                raise ValueError("Feature has no displayable coordinates.")
            if not np.isfinite(coordinates).all() or np.any(np.abs(coordinates[:, 1]) > 90):
                raise ValueError("Invalid display coordinates.")
            geometry = mapping(projected)
        except (
            ValueError,
            TypeError,
            NotImplementedError,
            ProjError,
            GEOSException,
            GeometryTypeError,
        ):
            error = "This geometry cannot be displayed on the map."
    return {
        "type": "Feature",
        "id": feature["index"],
        "geometry": geometry,
        "properties": {"index": feature["index"], "map_error": error},
    }
