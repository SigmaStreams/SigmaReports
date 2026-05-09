from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDERS_PATH = REPO_ROOT / "providers.json"
LEGACY_IPTV_EXPORT_PATH = REPO_ROOT / "data" / "iptv_channels.json"
LEGACY_SELECTOR_DATASET_PATH = REPO_ROOT / "data" / "iptv_channels_selector.json"
LEGACY_PROVIDER_ID = "default"
LEGACY_PROVIDER_NAME = "Default Provider"


def _resolve_config_path(path: str | Path | None) -> Path:
    candidate = Path(path) if path is not None else DEFAULT_PROVIDERS_PATH
    if candidate.is_absolute():
        return candidate
    return REPO_ROOT / candidate


def _resolve_data_path(value: Any, fallback: Path) -> str:
    raw_value = str(value or "").strip()
    candidate = Path(raw_value) if raw_value else fallback
    if candidate.is_absolute():
        return str(candidate)
    return str((REPO_ROOT / candidate).resolve())


def _legacy_provider() -> dict[str, Any]:
    return {
        "id": LEGACY_PROVIDER_ID,
        "name": LEGACY_PROVIDER_NAME,
        "enabled": True,
        "normalize_event_channels": False,
        "refresh_url_env": "",
        "raw_export": str(LEGACY_IPTV_EXPORT_PATH.resolve()),
        "selector_dataset": str(LEGACY_SELECTOR_DATASET_PATH.resolve()),
        "m3u_source": "",
        "is_legacy": True,
    }


def load_provider_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = _resolve_config_path(path)
    if not registry_path.exists():
        return {"providers": []}

    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"providers": []}

    if not isinstance(payload, dict):
        return {"providers": []}

    providers = payload.get("providers")
    if not isinstance(providers, list):
        payload["providers"] = []
    return payload


def configured_providers(path: str | Path | None = None) -> list[dict[str, Any]]:
    payload = load_provider_registry(path)
    providers = payload.get("providers")
    if not isinstance(providers, list):
        return []

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(providers, start=1):
        if not isinstance(item, dict):
            continue

        provider_id = str(item.get("id") or "").strip()
        if not provider_id:
            continue

        normalized_id = provider_id.lower()
        if normalized_id in seen_ids:
            continue
        seen_ids.add(normalized_id)

        name = str(item.get("name") or provider_id).strip() or provider_id
        normalized.append(
            {
                "id": provider_id,
                "name": name,
                "enabled": bool(item.get("enabled", True)),
                "normalize_event_channels": bool(item.get("normalize_event_channels", False)),
                "refresh_url_env": str(item.get("refresh_url_env") or "").strip(),
                "raw_export": _resolve_data_path(item.get("raw_export"), LEGACY_IPTV_EXPORT_PATH),
                "selector_dataset": _resolve_data_path(item.get("selector_dataset"), LEGACY_SELECTOR_DATASET_PATH),
                "m3u_source": _resolve_data_path(item.get("m3u_source"), Path("")) if item.get("m3u_source") else "",
                "is_legacy": False,
                "order": index,
            }
        )

    return normalized


def enabled_providers(path: str | Path | None = None) -> list[dict[str, Any]]:
    return [provider for provider in configured_providers(path) if provider.get("enabled")]


def default_provider(path: str | Path | None = None) -> dict[str, Any] | None:
    providers = enabled_providers(path)
    if not providers:
        if configured_providers(path):
            return None
        return _legacy_provider()

    payload = load_provider_registry(path)
    requested_default = str(payload.get("default_provider_id") or "").strip().lower()
    if requested_default:
        for provider in providers:
            if str(provider.get("id") or "").strip().lower() == requested_default:
                return provider

    if len(providers) == 1:
        return providers[0]

    return providers[0]


def get_provider(provider_id: str | None, path: str | Path | None = None) -> dict[str, Any] | None:
    normalized_id = str(provider_id or "").strip().lower()
    if not normalized_id:
        return default_provider(path)

    for provider in enabled_providers(path):
        if str(provider.get("id") or "").strip().lower() == normalized_id:
            return provider

    if normalized_id == LEGACY_PROVIDER_ID:
        configured = configured_providers(path)
        if not configured:
            return _legacy_provider()

    return None


def get_configured_provider(provider_id: str | None, path: str | Path | None = None) -> dict[str, Any] | None:
    normalized_id = str(provider_id or "").strip().lower()
    if not normalized_id:
        return default_provider(path)

    for provider in configured_providers(path):
        if str(provider.get("id") or "").strip().lower() == normalized_id:
            return provider

    if normalized_id == LEGACY_PROVIDER_ID:
        configured = configured_providers(path)
        if not configured:
            return _legacy_provider()

    return None


def resolve_raw_export_path(provider_id: str | None = None, *, path: str | Path | None = None) -> Path:
    provider = get_configured_provider(provider_id, path)
    raw_path = (provider or _legacy_provider()).get("raw_export") or str(LEGACY_IPTV_EXPORT_PATH.resolve())
    return Path(str(raw_path))


def resolve_selector_dataset_path(provider_id: str | None = None, *, path: str | Path | None = None) -> Path:
    provider = get_configured_provider(provider_id, path)
    selector_path = (provider or _legacy_provider()).get("selector_dataset") or str(LEGACY_SELECTOR_DATASET_PATH.resolve())
    return Path(str(selector_path))


def resolve_m3u_source_path(provider_id: str | None = None, *, path: str | Path | None = None) -> Path:
    provider = get_configured_provider(provider_id, path)
    m3u_source = (provider or {}).get("m3u_source") or "channels.m3u"
    candidate = Path(str(m3u_source))
    if candidate.is_absolute():
        return candidate
    return (REPO_ROOT / candidate).resolve()


def provider_display_name(provider_id: str | None, *, path: str | Path | None = None) -> str:
    provider = get_provider(provider_id, path)
    if not provider:
        return ""
    return str(provider.get("name") or "").strip()


def provider_normalizes_event_channels(provider_id: str | None, *, path: str | Path | None = None) -> bool:
    provider = get_configured_provider(provider_id, path)
    if not provider:
        return False
    return bool(provider.get("normalize_event_channels", False))


def provider_refresh_url_env(provider_id: str | None, *, path: str | Path | None = None) -> str:
    provider = get_configured_provider(provider_id, path)
    if not provider:
        return ""

    configured_name = str(provider.get("refresh_url_env") or "").strip()
    if configured_name:
        return configured_name

    normalized_id = re.sub(r"[^A-Za-z0-9]+", "_", str(provider.get("id") or "").strip().upper()).strip("_")
    if not normalized_id:
        return ""
    return f"IPTV_REFRESH_URL_{normalized_id}"