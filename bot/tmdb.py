import json
import urllib.request
from urllib.parse import quote
from typing import List


def _tmdb_get(url: str, bearer_token: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def fetch_tmdb_titles(bearer_token: str, limit_each: int = 30) -> List[str]:
    """
    Returns a list of titles from TMDB (trending TV + trending movies).
    Uses v3 endpoints with Bearer auth.
    """
    titles: list[str] = []

    tv = _tmdb_get("https://api.themoviedb.org/3/trending/tv/day", bearer_token)
    mv = _tmdb_get("https://api.themoviedb.org/3/trending/movie/day", bearer_token)

    for item in (tv.get("results") or [])[:limit_each]:
        name = item.get("name")
        if name:
            titles.append(str(name))

    for item in (mv.get("results") or [])[:limit_each]:
        name = item.get("title")
        if name:
            titles.append(str(name))

    # de-dupe while preserving order
    seen = set()
    out = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def search_tmdb_movies(bearer_token: str, query: str, limit: int = 12) -> list[dict]:
    """
    Search TMDB movies by title.
    Returns: [{id, title, year, content_type, source_db, reference_link}]
    """
    q = (query or "").strip()
    if not bearer_token or not q:
        return []

    url = f"https://api.themoviedb.org/3/search/movie?query={quote(q)}&include_adult=false"
    data = _tmdb_get(url, bearer_token)
    out: list[dict] = []

    for item in (data.get("results") or []):
        mid = item.get("id")
        title = str(item.get("title") or "").strip()
        if not mid or not title:
            continue

        release_date = str(item.get("release_date") or "").strip()
        year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""

        out.append(
            {
                "id": int(mid),
                "title": title,
                "year": year,
                "content_type": "movie",
                "source_db": "tmdb",
                "reference_link": f"https://www.themoviedb.org/movie/{int(mid)}",
            }
        )
        if len(out) >= max(1, int(limit)):
            break

    return out
