"""Bounded access to Meta's official Threads Graph API.

This transport emits SourceItems only. It does not resolve places or decide
whether a post recommends a place.
"""
import hashlib
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .adapters import AdapterCapability, SourceItem
from .auth import AuthRequired


API_ROOT = "https://graph.threads.net/v1.0"
USER_AGENT = "LAND-Data/2D-2 (bounded official Threads API validation)"
POST_FIELDS = ("id,permalink,text,username,owner,timestamp,media_type,"
               "thumbnail_url,shortcode,topic_tag,location_id")
SEARCH_FIELDS = "id,permalink,text,username,timestamp,media_type,shortcode"
LOCATION_FIELDS = "id,name,address,city,country,latitude,longitude,postal_code"
MAX_BYTES = 1_000_000


class ThreadsAPIError(ValueError):
    """An API failure without token, response body, or private URL details."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def location_level(location):
    """Conservative shape assessment; never infer a LAND category."""
    name = str(location.get("name") or "").strip()
    city = str(location.get("city") or "").strip()
    country = str(location.get("country") or "").strip()
    if name and (name.casefold() == city.casefold() or name.casefold() == country.casefold()):
        return "AREA"
    if name and location.get("address") and city and country and isinstance(location.get("latitude"), (int, float)) and isinstance(location.get("longitude"), (int, float)):
        return "POI"
    return "UNKNOWN"


def parse_post(post, location=None, location_status="NOT_TAGGED"):
    if not isinstance(post, dict):
        raise ThreadsAPIError("Threads post must be an object")
    post_id = post.get("id")
    if not isinstance(post_id, str) or not post_id.strip():
        raise ThreadsAPIError("Threads post is missing id")
    permalink = post.get("permalink")
    if not isinstance(permalink, str):
        raise ThreadsAPIError("Threads post is missing permalink")
    parsed = urlsplit(permalink)
    if parsed.scheme != "https" or parsed.hostname not in ("www.threads.com", "threads.com", "www.threads.net", "threads.net"):
        raise ThreadsAPIError("Threads post has invalid permalink")
    location_id = post.get("location_id")
    if location_id is not None and (not isinstance(location_id, str) or not location_id.strip()):
        raise ThreadsAPIError("Threads post has invalid location_id")
    if location is not None and (not isinstance(location, dict) or str(location.get("id")) != location_id):
        raise ThreadsAPIError("Threads location does not match post")
    text = post.get("text") if isinstance(post.get("text"), str) else ""
    owner = post.get("owner")
    author = post.get("username") or (owner.get("id") if isinstance(owner, dict) else None)
    payload = {
        "content_type": "SOCIAL_POST", "text": text,
        "published_at": post.get("timestamp") if isinstance(post.get("timestamp"), str) else None,
        "author_identifier": str(author) if author is not None else None,
        "hashtags": [post["topic_tag"]] if isinstance(post.get("topic_tag"), str) and post["topic_tag"] else [],
        "media_metadata": ([{"type": post["media_type"]}]
                           if isinstance(post.get("media_type"), str) else []),
        "source_metadata": {"threads_post_id": post_id, "threads_location_id": location_id,
                            "location_status": location_status,
                            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
        "content_status": "ACTIVE",
        "always_mention": True,
    }
    if location_id:
        tag = {"id": location_id, "level": location_level(location or {}), "category": "unknown"}
        if location:
            tag.update({key: location[key] for key in ("name", "address", "city", "country",
                                                     "latitude", "longitude", "postal_code")
                        if key in location and location[key] is not None})
        payload["geo_tag"] = tag
    return SourceItem("threads", post_id, permalink, payload)


class ThreadsAdapter:
    name = "threads"
    capability = AdapterCapability("AUTHENTICATED", supports_geo_tag=True, supports_search=True,
                                   supports_content_text=True, supports_media_metadata=True,
                                   supports_text=True, supports_hashtags=True, supports_author=True)

    def __init__(self, token, query=None, search_type="RECENT", limit=5, pages=1,
                 timeout=15, post_id=None, opener=None, sleeper=time.sleep,
                 api_root=API_ROOT):
        if not isinstance(token, str) or not token.strip():
            raise AuthRequired("AUTH_REQUIRED: threads; set LAND_THREADS_ACCESS_TOKEN after official OAuth")
        if bool(query) == bool(post_id):
            raise ValueError("Threads requires exactly one of query or post_id")
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 120):
            raise ValueError("Threads query must be 1-120 characters")
        if post_id is not None and (not isinstance(post_id, str) or not post_id.isdigit()):
            raise ValueError("Threads post_id must be numeric")
        if search_type not in ("TOP", "RECENT"):
            raise ValueError("Threads search type must be TOP or RECENT")
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("Threads result limit must be 1-5")
        if type(pages) is not int or not 1 <= pages <= 2:
            raise ValueError("Threads pages must be 1-2")
        if type(timeout) is not int or not 2 <= timeout <= 30:
            raise ValueError("Threads timeout must be 2-30 seconds")
        if api_root.rstrip("/") != API_ROOT and opener is None:
            raise ValueError("Threads API host cannot be overridden for live requests")
        self.token = token.strip()
        self.query = query.strip() if query else None
        self.post_id = post_id
        self.search_type = search_type
        self.limit = limit
        self.pages = pages
        self.timeout = timeout
        self.opener = opener or build_opener(_NoRedirect()).open
        self.sleeper = sleeper
        self.api_root = api_root.rstrip("/")
        self.stats = {"posts": 0, "with_location_id": 0, "location_details": 0}

    def _get(self, path, params=None, allow_missing=False):
        url = self.api_root + "/" + path.lstrip("/")
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Authorization": "Bearer " + self.token,
                                        "User-Agent": USER_AGENT, "Accept": "application/json"})
        for attempt in range(2):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ThreadsAPIError("Threads response exceeds 1 MB")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ThreadsAPIError("Threads response must be an object")
                if "error" in data:
                    raise ThreadsAPIError("Threads API returned an error; check OAuth permissions")
                return data
            except HTTPError as exc:
                if allow_missing and exc.code in (404, 410):
                    return None
                if exc.code in (401, 403):
                    raise AuthRequired("Threads OAuth token invalid or permission denied (HTTP {})".format(exc.code)) from None
                if exc.code == 429:
                    raise ThreadsAPIError("Threads rate limit reached (HTTP 429); retry later") from None
                if attempt == 0 and exc.code in (502, 503, 504):
                    self.sleeper(1)
                    continue
                raise ThreadsAPIError("Threads HTTP {}".format(exc.code)) from None
            except (URLError, TimeoutError) as exc:
                if attempt == 0:
                    self.sleeper(1)
                    continue
                raise ThreadsAPIError("Threads network or timeout error") from None
            except (UnicodeError, json.JSONDecodeError):
                raise ThreadsAPIError("Threads returned invalid JSON") from None

    def validate_token(self):
        data = self._get("me", {"fields": "id,username"})
        if not isinstance(data.get("id"), str):
            raise ThreadsAPIError("Threads token validation returned no user id")
        return {"status": "VALID", "user_id": data["id"],
                "permissions": "NEEDS_VERIFICATION"}

    def _location(self, location_id):
        if not location_id:
            return None, "NOT_TAGGED"
        if not isinstance(location_id, str) or not location_id.isdigit():
            raise ThreadsAPIError("Threads post has invalid location_id")
        data = self._get(location_id, {"fields": LOCATION_FIELDS}, allow_missing=True)
        if data is None:
            return None, "UNAVAILABLE"
        if str(data.get("id")) != location_id:
            raise ThreadsAPIError("Threads location id mismatch")
        self.stats["location_details"] += 1
        return data, "RETRIEVED"

    def collect(self, city):
        if not isinstance(city, str) or not city.strip():
            raise ValueError("Threads collection key is required")
        self.stats = {"posts": 0, "with_location_id": 0, "location_details": 0}
        if self.post_id:
            data = self._get(self.post_id, {"fields": POST_FIELDS}, allow_missing=True)
            if data is None:
                yield SourceItem("threads", self.post_id, None,
                                 {"content_type": "SOCIAL_POST", "content_status": "UNAVAILABLE",
                                  "source_metadata": {"threads_post_id": self.post_id}})
                return
            posts = [data]
        else:
            posts = []
            after = None
            for _page in range(self.pages):
                params = {"q": self.query, "search_type": self.search_type,
                          "fields": SEARCH_FIELDS, "limit": self.limit - len(posts)}
                if after:
                    params["after"] = after
                data = self._get("keyword_search", params)
                rows = data.get("data")
                if not isinstance(rows, list):
                    raise ThreadsAPIError("Threads search response missing data array")
                posts.extend(rows)
                if len(posts) >= self.limit:
                    break
                paging = data.get("paging") or {}
                cursors = paging.get("cursors") if isinstance(paging, dict) else None
                next_after = cursors.get("after") if isinstance(cursors, dict) else None
                if not isinstance(next_after, str) or not next_after or next_after == after or not rows:
                    break
                after = next_after
        seen = set()
        for post in posts[:self.limit]:
            if not isinstance(post, dict) or not isinstance(post.get("id"), str):
                raise ThreadsAPIError("Threads search returned malformed post")
            if post["id"] in seen:
                continue
            seen.add(post["id"])
            if not self.post_id:
                post = self._get(post["id"], {"fields": POST_FIELDS}, allow_missing=True)
                if post is None:
                    continue
            location_id = post.get("location_id")
            if location_id:
                self.stats["with_location_id"] += 1
            location, location_status = self._location(location_id)
            item = parse_post(post, location, location_status)
            self.stats["posts"] += 1
            yield item
