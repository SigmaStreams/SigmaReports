import json
import urllib.request
from urllib.parse import quote, urlparse


def _tvdb_poster_url(item: dict) -> str:
    for key in ("image_url", "image", "thumbnail"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _tvdb_request(url: str, *, method: str = "GET", payload: dict | None = None, token: str | None = None, timeout: int = 15) -> dict:
    data = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def _tvdb_login(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""

    data = _tvdb_request(
        "https://api4.thetvdb.com/v4/login",
        method="POST",
        payload={"apikey": key},
    )
    return str(data.get("data", {}).get("token") or "").strip()


def _tvdb_english_title(item: dict) -> str:
    translations = item.get("translations")
    if not isinstance(translations, dict):
        return ""

    for language in ("eng", "en", "en-US", "en-GB"):
        value = str(translations.get(language) or "").strip()
        if value:
            return value
    return ""


def _search_tvdb_series(token: str, query: str, limit: int) -> list[dict]:
    q = (query or "").strip()
    if not token or not q:
        return []

    url = f"https://api4.thetvdb.com/v4/search?query={quote(q)}&type=series"
    data = _tvdb_request(url, token=token)

    out: list[dict] = []
    for item in (data.get("data") or []):
        sid = item.get("tvdb_id") or item.get("id")
        sid_text = str(sid or "").strip()
        title = str(item.get("name") or "").strip()
        if not sid_text or not title:
            continue
        english_title = _tvdb_english_title(item)
        if english_title.casefold() == title.casefold():
            english_title = ""

        year = ""
        year_value = item.get("year") or item.get("firstAired") or ""
        year_text = str(year_value).strip()
        if len(year_text) >= 4 and year_text[:4].isdigit():
            year = year_text[:4]

        slug = str(item.get("slug") or "").strip().strip("/")
        if slug:
            ref = f"https://www.thetvdb.com/series/{slug}"
        else:
            ref = f"https://www.thetvdb.com/dereferrer/series/{sid_text}"

        out.append(
            {
                "id": sid_text,
                "title": title,
                "english_title": english_title,
                "year": year,
                "content_type": "tv",
                "source_db": "tvdb",
                "reference_link": ref,
                "poster_url": _tvdb_poster_url(item),
            }
        )
        if len(out) >= max(1, int(limit)):
            break

    return out


def search_tvdb_series(api_key: str, query: str, limit: int = 12) -> list[dict]:
    """
    Search TVDB series by title.
    Returns title as the original name and english_title when TVDB provides one.
    """
    q = (query or "").strip()
    if not api_key or not q:
        return []

    token = _tvdb_login(api_key)
    return _search_tvdb_series(token, q, limit) if token else []


def _fetch_tvdb_english_title(token: str, series_id: str) -> str:
    try:
        data = _tvdb_request(
            f"https://api4.thetvdb.com/v4/series/{quote(str(series_id))}/translations/eng",
            token=token,
        )
    except Exception:
        return ""
    translation = data.get("data") or {}
    return str(translation.get("name") or "").strip() if isinstance(translation, dict) else ""


def _extract_tvdb_slug(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "thetvdb.com":
        return ""

    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) < 2 or parts[0] != "series":
        return ""
    return parts[1].strip()


def resolve_tvdb_series_link(api_key: str, url: str) -> dict | None:
    """
    Resolve a TVDB series URL into a title record used by the VOD workflow.
    """
    slug = _extract_tvdb_slug(url)
    if not api_key or not slug:
        return None

    token = _tvdb_login(api_key)
    if not token:
        return None

    candidates = _search_tvdb_series(token, slug.replace("-", " "), limit=25)
    slug_lower = slug.lower()

    selected = None
    for item in candidates:
        ref = str(item.get("reference_link") or "").strip().lower().rstrip("/")
        if ref.endswith(f"/series/{slug_lower}"):
            selected = item
            break

    if selected is None:
        for item in candidates:
            ref = str(item.get("reference_link") or "").strip().lower().rstrip("/")
            if f"/series/{slug_lower}" in ref:
                selected = item
                break

    if selected is None:
        selected = candidates[0] if candidates else None
    if selected is None:
        return None

    if not str(selected.get("english_title") or "").strip():
        english_title = _fetch_tvdb_english_title(token, str(selected.get("id") or ""))
        if english_title and english_title.casefold() != str(selected.get("title") or "").strip().casefold():
            selected["english_title"] = english_title
    return selected


def lookup_tvdb_episode(api_key: str, series_id: str, season: int, episode: int) -> dict | None:
    """Optionally enrich user-entered numbers using TVDB's official episode order."""
    if not api_key or not str(series_id).isdigit():
        return None
    token = _tvdb_login(api_key)
    if not token:
        return None
    data = _tvdb_request(
        f"https://api4.thetvdb.com/v4/series/{series_id}/episodes/official"
        f"?page=0&season={season}&episodeNumber={episode}", token=token, timeout=5,
    )
    for item in (data.get("data") or {}).get("episodes") or []:
        if item.get("seasonNumber") == season and item.get("number") == episode:
            return {"episode_tvdb_id": str(item.get("id") or ""),
                    "episode_title": str(item.get("name") or "").strip()[:200]}
    return None
