"""One-shot, user-triggered city bootstrap for the terminal collector."""
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .util import utc_now


CITY_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COUNTRY = re.compile(r"^[A-Za-z]{2}$")
DEFAULT_GEOCODER = "https://nominatim.openstreetmap.org/search"
DEFAULT_QUERY_TOPICS = {
    "entry": ("入境 海关", "签证 入境"),
    "arrival": ("机场 到市区", "落地 第一天"),
    "packing": ("行前准备", "天气 穿衣 插头"),
    "money": ("支付 信用卡", "现金 ATM 换汇", "小费"),
    "food": ("餐厅", "咖啡馆", "酒吧", "点餐 攻略", "过敏 素食"),
    "transport": ("地铁 公交", "打车", "租车 自驾 停车"),
    "stay": ("酒店", "酒店 入住 退房", "行李寄存"),
    "culture": ("文化 礼仪", "当地 禁忌", "社交 餐桌礼仪", "拍照 礼仪"),
    "safety": ("安全 防骗", "紧急 求助", "护照 丢失 领事馆"),
    "connectivity": ("SIM eSIM", "WiFi 上网"),
    "sightseeing": ("景点", "展览 活动", "博物馆", "夜生活"),
    "shopping": ("商场 购物", "退税", "退换货"),
    "health": ("看病 医院", "药房 买药", "旅行保险"),
    "departure": ("机场 值机 安检", "返程 转机"),
    "special-needs": ("亲子", "无障碍", "长者", "宠物"),
}


def default_query_groups(search_name):
    return {group: ["{} {}".format(search_name, topic) for topic in topics]
            for group, topics in DEFAULT_QUERY_TOPICS.items()}


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp-" + secrets.token_hex(6))
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(str(temporary), str(path))


def resolve_city(name, country, *, endpoint=None, timeout=15, opener=urlopen):
    """Resolve one named city through one bounded Nominatim request."""
    name = (name or "").strip()
    country = (country or "").strip().upper()
    if not name:
        raise ValueError("--city-name is required when creating a city")
    if not COUNTRY.fullmatch(country):
        raise ValueError("--country must be a two-letter ISO country code")
    endpoint = endpoint or os.environ.get("LAND_CITY_GEOCODER_URL", DEFAULT_GEOCODER)
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("LAND_CITY_GEOCODER_URL must be an HTTPS endpoint")
    query = urlencode({
        "q": name,
        "countrycodes": country.lower(),
        "featuretype": "city",
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 1,
        "accept-language": "en",
    })
    request = Request(endpoint + ("&" if "?" in endpoint else "?") + query, headers={
        "Accept": "application/json",
        "User-Agent": "LAND-DATA/1.0 (personal local city bootstrap)",
    })
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(256 * 1024 + 1)
    except Exception as exc:
        raise ValueError("city lookup failed: {}".format(type(exc).__name__)) from exc
    if len(body) > 256 * 1024:
        raise ValueError("city lookup response is too large")
    try:
        rows = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("city lookup returned invalid JSON") from exc
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError("city lookup found no matching city")
    row = rows[0]
    address = row.get("address") if isinstance(row.get("address"), dict) else {}
    returned_country = str(address.get("country_code", "")).upper()
    if returned_country and returned_country != country:
        raise ValueError("city lookup returned a different country")
    try:
        latitude, longitude = float(row["lat"]), float(row["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("city lookup returned invalid coordinates") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("city lookup returned out-of-range coordinates")
    return {
        "display_name": name,
        "country": country,
        "latitude": latitude,
        "longitude": longitude,
        "setup_provenance": {
            "provider": "OpenStreetMap Nominatim",
            "resolved_name": row.get("display_name"),
            "osm_type": row.get("osm_type"),
            "osm_id": str(row.get("osm_id")) if row.get("osm_id") is not None else None,
            "resolved_at": utc_now(),
            "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        },
    }


def ensure_city(root, city_id, city_name=None, country=None, *, resolver=resolve_city):
    """Create city metadata and its XHS query bank once; existing config wins."""
    if not CITY_ID.fullmatch(city_id or ""):
        raise ValueError("--city must use lowercase letters, numbers and hyphens")
    root = Path(root)
    cities_path = root / "config" / "cities.json"
    cities = json.loads(cities_path.read_text(encoding="utf-8"))
    query_relative = "config/xiaohongshu-queries-{}.json".format(city_id)
    query_path = root / query_relative
    city_created = False
    query_file_created = False
    if city_id not in cities:
        city = resolver(city_name, country)
        cities[city_id] = city
        _atomic_json(cities_path, cities)
        city_created = True
    search_name = (city_name or cities[city_id].get("display_name") or city_id).strip()
    if not query_path.exists():
        _atomic_json(query_path, {
            "city": city_id,
            "coverage_version": 2,
            "query_groups": default_query_groups(search_name),
            "limits": {"posts_per_query": 30, "scrolls_per_query": 4,
                       "pause_seconds": 1.5},
        })
        query_file_created = True
    return {"city": city_id, "city_created": city_created,
            "query_file_created": query_file_created,
            "query_file": query_relative, "city_config": cities[city_id]}
