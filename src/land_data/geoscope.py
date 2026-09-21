"""Explicit geographic collection boundary, independent of POI.city."""
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass(frozen=True)
class GeoScope:
    name: str
    country: str
    bbox: Tuple[float, float, float, float]  # south, west, north, east
    scope_type: str = "CUSTOM"
    city: str = ""
    center: Optional[Tuple[float, float]] = None
    radius_meters: Optional[int] = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", self.name) or not self.country.strip():
            raise ValueError("GeoScope requires a safe name and country")
        if self.scope_type not in ("CITY", "NEIGHBORHOOD", "REGION", "CUSTOM"):
            raise ValueError("unsupported GeoScope type")
        if len(self.bbox) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v)
                                          for v in self.bbox):
            raise ValueError("GeoScope requires a finite bounding box")
        south, west, north, east = self.bbox
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ValueError("invalid GeoScope bounding box")
        if (self.center is None) != (self.radius_meters is None):
            raise ValueError("GeoScope center and radius must be supplied together")
        if self.center is not None:
            if len(self.center) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v)
                                                 for v in self.center):
                raise ValueError("invalid GeoScope center")
            lat, lon = self.center
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not 0 < self.radius_meters <= 50000:
                raise ValueError("invalid GeoScope center or radius")


def load_scope(path: str) -> GeoScope:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    value["bbox"] = tuple(value["bbox"])
    if value.get("center") is not None:
        value["center"] = tuple(value["center"])
    return GeoScope(**value)
