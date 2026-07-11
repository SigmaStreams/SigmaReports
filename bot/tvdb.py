import json
import urllib.request
from urllib.parse import quote


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


def search_tvdb_series(api_key: str, query: str, limit: int = 12) -> list[dict]:
    """
    Search TVDB series by title.
    Returns: [{id, title, year, content_type, source_db, reference_link}]
    """
    q = (query or "").strip()
    if not api_key or not q:
        return []

    token = _tvdb_login(api_key)
    if not token:
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
                "year": year,
                "content_type": "tv",
                "source_db": "tvdb",
                "reference_link": ref,
            }
        )
        if len(out) >= max(1, int(limit)):
            break

    return out
