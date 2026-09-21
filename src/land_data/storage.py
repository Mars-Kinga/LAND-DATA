"""The only persistence contract used by pipeline and export code."""
from typing import List, Optional, Protocol, Type, TypeVar

from .models import POI

T = TypeVar("T")


class Storage(Protocol):
    def put(self, item: object) -> None: ...

    def get(self, model: Type[T], item_id: str) -> Optional[T]: ...

    def all(self, model: Type[T]) -> List[T]: ...

    def pois_in_city(self, city: str) -> List[POI]: ...

    def recommendation_counts(self, poi_id: str) -> List[dict]: ...

    def purge_expired_raw(self, now: str) -> int: ...
