from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.providers import LEGACY_IPTV_EXPORT_PATH, LEGACY_SELECTOR_DATASET_PATH, resolve_selector_dataset_path


_MALFORMED_NAME_MARKERS = ("tvg-name=", "tvg-logo=", "group-title=")
_EVENT_SUFFIX_PATTERNS = (
    re.compile(r"^(?P<base>[A-Za-z0-9+&.'()\-/ ]+\s\d{1,3})\s*:\s+.+$"),
    re.compile(r"^(?P<base>[A-Za-z0-9+&.'()\-/ ]+\sALT\s\d{1,3})\s*:\s+.+$"),
    re.compile(r"^(?P<base>[A-Za-z0-9+&.'()\-/ ]+\s\d{1,3})\s+\d{1,2}:\d{2}\s*\|?\s+.+$"),
    re.compile(r"^(?P<base>[A-Za-z0-9+&.'()\-/ ]+\sALT\s\d{1,3})\s+\d{1,2}:\d{2}\s*\|?\s+.+$"),
)
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IPTV_EXPORT_PATH = LEGACY_IPTV_EXPORT_PATH
DEFAULT_SELECTOR_DATASET_PATH = LEGACY_SELECTOR_DATASET_PATH


def _empty_selector_dataset() -> dict[str, Any]:
    return {
        "source_file": "",
        "generated_at": "",
        "selector_generated_at": "",
        "category_count": 0,
        "channel_count": 0,
        "stats": {
            "input_channels": 0,
            "skipped_empty": 0,
            "skipped_malformed": 0,
            "deduplicated": 0,
        },
        "categories": [],
    }


def load_iptv_export(path: str | Path) -> dict[str, Any]:
    export_path = Path(path)
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"IPTV export must be a JSON object: {export_path}")
    return payload


def build_selector_dataset(
    source: str | Path | dict[str, Any],
    max_label_length: int = 100,
    *,
    normalize_event_channels: bool = False,
) -> dict[str, Any]:
    payload = load_iptv_export(source) if isinstance(source, (str, Path)) else source
    raw_channels = payload.get("channels")
    if not isinstance(raw_channels, list):
        raise ValueError("IPTV export is missing a 'channels' list")

    cleaned_channels: dict[tuple[str, str], dict[str, Any]] = {}
    skipped_empty = 0
    skipped_malformed = 0
    duplicate_count = 0

    for raw_channel in raw_channels:
        if not isinstance(raw_channel, dict):
            skipped_malformed += 1
            continue

        raw_name = _normalize_text(raw_channel.get("name"))
        name = _selector_channel_name(raw_name, normalize_event_channels=normalize_event_channels)
        category = _normalize_text(raw_channel.get("category"))
        url = _normalize_text(raw_channel.get("url"))
        tvg_id = _normalize_text(raw_channel.get("tvg_id"))
        tvg_name = _normalize_text(raw_channel.get("tvg_name"))
        tvg_logo = _normalize_text(raw_channel.get("tvg_logo"))

        if not name or not category or not url:
            skipped_empty += 1
            continue

        if _is_malformed_name(name):
            skipped_malformed += 1
            continue

        normalized = {
            "name": name,
            "raw_name": raw_name,
            "category": category,
            "url": url,
            "tvg_id": tvg_id,
            "tvg_name": tvg_name,
            "tvg_logo": tvg_logo,
            "display_name": _truncate_label(name, max_label_length),
            "selector_key": _build_selector_key(category, name, url),
            "search_text": f"{category} {name} {raw_name}".lower(),
        }

        key = (category.lower(), name.lower())
        current = cleaned_channels.get(key)
        if current is None:
            cleaned_channels[key] = normalized
            continue

        duplicate_count += 1
        if _prefer_candidate(current, normalized):
            cleaned_channels[key] = normalized

    grouped: dict[str, list[dict[str, Any]]] = {}
    for channel in cleaned_channels.values():
        grouped.setdefault(channel["category"], []).append(channel)

    categories = []
    for category_name in sorted(grouped, key=str.lower):
        channels = sorted(grouped[category_name], key=lambda item: item["name"].lower())
        categories.append(
            {
                "name": category_name,
                "channel_count": len(channels),
                "channels": channels,
            }
        )

    return {
        "source_file": payload.get("source_file") or "",
        "generated_at": payload.get("generated_at") or "",
        "selector_generated_at": datetime.now(timezone.utc).isoformat(),
        "category_count": len(categories),
        "channel_count": len(cleaned_channels),
        "stats": {
            "input_channels": len(raw_channels),
            "skipped_empty": skipped_empty,
            "skipped_malformed": skipped_malformed,
            "deduplicated": duplicate_count,
        },
        "categories": categories,
    }


def write_selector_dataset(
    source_path: str | Path,
    output_path: str | Path,
    max_label_length: int = 100,
    *,
    normalize_event_channels: bool = False,
) -> dict[str, Any]:
    selector_dataset = build_selector_dataset(
        source_path,
        max_label_length=max_label_length,
        normalize_event_channels=normalize_event_channels,
    )
    destination = Path(output_path)
    destination.write_text(json.dumps(selector_dataset, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return selector_dataset


def _selector_dataset_path(path: str | Path | None = None, *, provider_id: str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return resolve_selector_dataset_path(provider_id)


def load_selector_dataset(
    path: str | Path | None = None,
    *,
    provider_id: str | None = None,
) -> dict[str, Any]:
    dataset_path = _selector_dataset_path(path, provider_id=provider_id)
    if not dataset_path.exists():
        return _empty_selector_dataset()
    try:
        stat = dataset_path.stat()
        return _load_selector_dataset_cached(str(dataset_path.resolve()), stat.st_mtime_ns)
    except (OSError, ValueError, json.JSONDecodeError):
        return _empty_selector_dataset()


def selector_dataset_available(
    path: str | Path | None = None,
    *,
    provider_id: str | None = None,
) -> bool:
    try:
        return bool(selector_categories(path, provider_id=provider_id))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def selector_categories(
    path: str | Path | None = None,
    *,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    payload = load_selector_dataset(path, provider_id=provider_id)
    categories = payload.get("categories")
    if not isinstance(categories, list):
        raise ValueError("Selector dataset is missing a 'categories' list")
    return [category for category in categories if isinstance(category, dict)]


def search_selector_categories(
    query: str,
    *,
    limit: int = 25,
    path: str | Path | None = None,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(query).lower()
    categories = selector_categories(path, provider_id=provider_id)

    if not normalized_query:
        return categories[:limit]

    starts_with = []
    contains = []
    for category in categories:
        name = _normalize_text(category.get("name"))
        lowered = name.lower()
        if lowered.startswith(normalized_query):
            starts_with.append(category)
        elif normalized_query in lowered:
            contains.append(category)

    return (starts_with + contains)[:limit]


def search_selector_channels(
    category_name: str,
    query: str,
    *,
    limit: int = 25,
    path: str | Path | None = None,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    category = find_selector_category(category_name, path=path, provider_id=provider_id)
    if not category:
        return []

    normalized_query = _normalize_text(query).lower()
    channels = [channel for channel in category.get("channels", []) if isinstance(channel, dict)]
    if not normalized_query:
        return channels[:limit]

    starts_with = []
    contains = []
    for channel in channels:
        name = _normalize_text(channel.get("name"))
        display_name = _normalize_text(channel.get("display_name"))
        search_text = _normalize_text(channel.get("search_text"))
        lowered_name = name.lower()
        if lowered_name.startswith(normalized_query):
            starts_with.append(channel)
            continue
        if normalized_query in lowered_name or normalized_query in display_name.lower() or normalized_query in search_text:
            contains.append(channel)

    return (starts_with + contains)[:limit]


def all_selector_channels(
    path: str | Path | None = None,
    *,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    for category in selector_categories(path, provider_id=provider_id):
        channels.extend(channel for channel in category.get("channels", []) if isinstance(channel, dict))
    return channels


def search_all_selector_channels(
    query: str,
    *,
    limit: int = 25,
    path: str | Path | None = None,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(query).lower()
    channels = all_selector_channels(path, provider_id=provider_id)
    if not normalized_query:
        return channels[:limit]

    starts_with = []
    contains = []
    for channel in channels:
        name = _normalize_text(channel.get("name"))
        display_name = _normalize_text(channel.get("display_name"))
        search_text = _normalize_text(channel.get("search_text"))
        lowered_name = name.lower()
        if lowered_name.startswith(normalized_query):
            starts_with.append(channel)
            continue
        if normalized_query in lowered_name or normalized_query in display_name.lower() or normalized_query in search_text:
            contains.append(channel)

    return (starts_with + contains)[:limit]


def find_selector_category(
    category_name: str,
    *,
    path: str | Path | None = None,
    provider_id: str | None = None,
) -> dict[str, Any] | None:
    normalized_name = _normalize_text(category_name).lower()
    if not normalized_name:
        return None

    for category in selector_categories(path, provider_id=provider_id):
        if _normalize_text(category.get("name")).lower() == normalized_name:
            return category
    return None


def find_selector_channel(
    selector_key: str,
    *,
    category_name: str | None = None,
    path: str | Path | None = None,
    provider_id: str | None = None,
) -> dict[str, Any] | None:
    normalized_key = _normalize_text(selector_key)
    if not normalized_key:
        return None

    categories = []
    if category_name:
        category = find_selector_category(category_name, path=path, provider_id=provider_id)
        if category:
            categories = [category]
    else:
        categories = selector_categories(path, provider_id=provider_id)

    for category in categories:
        for channel in category.get("channels", []):
            if not isinstance(channel, dict):
                continue
            if _normalize_text(channel.get("selector_key")) == normalized_key:
                return channel
    return None


@lru_cache(maxsize=8)
def _load_selector_dataset_cached(path_str: str, mtime_ns: int) -> dict[str, Any]:
    del mtime_ns
    return load_iptv_export(path_str)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _selector_channel_name(value: Any, *, normalize_event_channels: bool = False) -> str:
    name = _normalize_text(value)
    if not name:
        return ""

    if not normalize_event_channels:
        return name

    for pattern in _EVENT_SUFFIX_PATTERNS:
        match = pattern.match(name)
        if match:
            return _normalize_text(match.group("base"))

    return name


def _is_malformed_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _MALFORMED_NAME_MARKERS)


def _truncate_label(value: str, max_length: int) -> str:
    if max_length <= 3 or len(value) <= max_length:
        return value[:max_length]
    return value[: max_length - 3].rstrip() + "..."


def _build_selector_key(category: str, name: str, url: str) -> str:
    digest = hashlib.sha1(f"{category}\0{name}\0{url}".encode("utf-8")).hexdigest()
    return digest[:16]


def _prefer_candidate(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    current_tvg_id = _normalize_text(current.get("tvg_id"))
    candidate_tvg_id = _normalize_text(candidate.get("tvg_id"))
    if not current_tvg_id and candidate_tvg_id:
        return True
    if current_tvg_id and not candidate_tvg_id:
        return False
    return candidate["url"] < current["url"]