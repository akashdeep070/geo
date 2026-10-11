"""Bounded parsing of zipped Shapefiles and KML without trusting archive paths."""

import json
import math
import stat
import zlib
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

import fiona
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pyproj import CRS

from app.config import FileError, Settings
from app.measurements import measure

KML = "{http://www.opengis.net/kml/2.2}"
GEOMETRIES = {"Point", "LineString", "Polygon", "MultiGeometry"}
UNMEASURED_KML = {
    f"{KML}Model",
    "{http://www.google.com/kml/ext/2.2}Track",
    "{http://www.google.com/kml/ext/2.2}MultiTrack",
}


def is_geometry(element):
    return element.tag in {f"{KML}{kind}" for kind in GEOMETRIES} | UNMEASURED_KML


def coordinates(element):
    node = element.find(f"{KML}coordinates")
    if node is None or not node.text or not node.text.strip():
        raise FileError("KML geometry must contain coordinates.")
    points = []
    for item in node.text.split():
        parts = item.split(",")
        if len(parts) not in (2, 3):
            raise FileError("KML coordinates must be longitude,latitude[,altitude].")
        point = [float(part) for part in parts]
        if not all(math.isfinite(value) for value in point):
            raise FileError("Coordinates must be finite numbers.")
        if abs(point[0]) > 180 or abs(point[1]) > 90:
            raise FileError("KML coordinates exceed longitude/latitude bounds.")
        points.append(point)
    return points


def kml_geometry(element):
    if element.tag in UNMEASURED_KML:
        return {
            "type": element.tag.split("}")[-1],
            "source_kml": ET.tostring(element, encoding="unicode"),
        }
    kind = element.tag.removeprefix(KML)
    if kind == "MultiGeometry":
        children = [kml_geometry(child) for child in element if is_geometry(child)]
        types = {child["type"] for child in children}
        if len(types) == 1 and types <= {"Point", "LineString", "Polygon"}:
            return {
                "type": "Multi" + children[0]["type"],
                "coordinates": [child["coordinates"] for child in children],
            }
        return {"type": "GeometryCollection", "geometries": children}
    if kind == "Polygon":
        outer = element.find(f"{KML}outerBoundaryIs/{KML}LinearRing")
        if outer is None:
            raise FileError("KML Polygon is missing its outer boundary.")
        rings = [coordinates(outer)] + [
            coordinates(ring) for ring in element.findall(f"{KML}innerBoundaryIs/{KML}LinearRing")
        ]
        for ring in rings:
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise FileError("KML polygon rings must be closed and have at least four points.")
        return {"type": kind, "coordinates": rings}
    points = coordinates(element)
    if kind == "Point" and len(points) != 1:
        raise FileError("KML Point must contain exactly one coordinate.")
    if kind == "LineString" and len(points) < 2:
        raise FileError("KML LineString needs at least two coordinates.")
    return {"type": kind, "coordinates": points[0] if kind == "Point" else points}


def read_kml(path):
    root = ET.parse(path).getroot()
    if root.tag != f"{KML}kml":
        raise FileError("Expected a KML 2.2 document with the standard namespace.")
    for index, placemark in enumerate(root.iter(f"{KML}Placemark")):
        properties = {}
        for name in ("name", "description"):
            text = placemark.findtext(f"{KML}{name}")
            if text is not None:
                properties[name] = text
        for data in placemark.findall(f".//{KML}ExtendedData/{KML}Data"):
            properties[data.get("name", "")] = data.findtext(f"{KML}value")
        for data in placemark.findall(f".//{KML}SimpleData"):
            properties[data.get("name", "")] = data.text
        elements = [child for child in placemark if is_geometry(child)]
        if len(elements) > 1:
            raise FileError("Use MultiGeometry for a Placemark with multiple geometries.")
        geometry = kml_geometry(elements[0]) if elements else None
        yield str(placemark.get("id", index)), geometry, properties


def extract_shapefile(path: Path, directory: Path, settings: Settings):
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > settings.max_archive_entries:
            raise FileError("ZIP contains too many entries.", 413)
        if sum(item.file_size for item in entries) > settings.max_uncompressed_bytes:
            raise FileError("ZIP exceeds the uncompressed size limit.", 413)
        seen = set()
        for item in entries:
            name = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in item.filename
                or ":" in item.filename
                or stat.S_ISLNK(mode)
                or item.flag_bits & 1
            ):
                raise FileError("ZIP contains unsafe paths, links, or encrypted entries.")
            if item.filename.casefold() in seen:
                raise FileError("ZIP contains duplicate filenames.")
            seen.add(item.filename.casefold())
        shapefiles = [
            item for item in entries if not item.is_dir() and item.filename.lower().endswith(".shp")
        ]
        if len(shapefiles) != 1:
            raise FileError("ZIP must contain exactly one Shapefile dataset.")
        selected = PurePosixPath(shapefiles[0].filename)
        components = {
            PurePosixPath(item.filename).suffix.lower(): item
            for item in entries
            if PurePosixPath(item.filename).parent == selected.parent
            and PurePosixPath(item.filename).stem.casefold() == selected.stem.casefold()
        }
        if not {".shp", ".shx", ".dbf", ".prj"} <= components.keys():
            raise FileError("Shapefile requires matching .shp, .shx, .dbf and .prj files.")
        # Copy only the dataset components under generated, trusted filenames.
        for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            if extension in components:
                item = components[extension]
                written = 0
                with (
                    archive.open(item) as source,
                    (directory / ("dataset" + extension)).open("wb") as target,
                ):
                    while chunk := source.read(64 * 1024):
                        written += len(chunk)
                        if written > item.file_size or written > settings.max_uncompressed_bytes:
                            raise FileError("ZIP component exceeds the size limit.", 413)
                        target.write(chunk)
        return directory / "dataset.shp"


def coordinate_count(value):
    if isinstance(value, dict):
        return sum(coordinate_count(value.get(key, [])) for key in ("coordinates", "geometries"))
    if isinstance(value, (tuple, list)):
        if value and isinstance(value[0], (float, int)):
            if not all(math.isfinite(v) for v in value):
                raise FileError("Coordinates must be finite numbers.")
            return 1
        return sum(coordinate_count(child) for child in value)
    return 0


def collect(features, crs, settings):
    results = []
    count = 0
    for index, (feature_id, geometry, properties) in enumerate(features):
        if index >= settings.max_features:
            raise FileError("File exceeds the feature limit.", 413)
        count += coordinate_count(geometry)
        if count > settings.max_coordinates:
            raise FileError("File exceeds the coordinate limit.", 413)
        results.append(measure(index, feature_id, geometry, properties, crs))
    if not results:
        raise FileError("File contains no features.")
    return results


def process(path: Path, extension: str, settings: Settings):
    try:
        if extension == ".kml":
            crs = CRS.from_epsg(4326)
            results = collect(read_kml(path), crs, settings)
        else:
            with TemporaryDirectory(prefix="geo-shape-") as temporary:
                shp = extract_shapefile(path, Path(temporary), settings)
                with fiona.open(shp) as source:
                    if not source.crs_wkt:
                        raise FileError("Shapefile CRS is missing or unreadable.")
                    crs = CRS.from_wkt(source.crs_wkt)
                    features = (
                        (
                            str(feature.id),
                            dict(feature.geometry) if feature.geometry else None,
                            dict(feature.properties),
                        )
                        for feature in source
                    )
                    results = collect(features, crs, settings)
        # Check JSON representation before persisting any record.
        json.dumps(results, allow_nan=False)
        return crs.to_string(), results
    except FileError:
        raise
    except (
        BadZipFile,
        ET.ParseError,
        DefusedXmlException,
        fiona.errors.FionaError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        RuntimeError,
        EOFError,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise FileError("Malformed or unreadable geospatial file.") from exc
