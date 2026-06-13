from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "providers.json"
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manage IPTV providers in providers.json and scaffold the matching channels/ and data/providers/ layout."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the provider registry JSON file. Defaults to providers.json in the repo root.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List configured providers.")
    list_parser.add_argument("--json", action="store_true", help="Print the raw registry JSON.")
    list_parser.set_defaults(handler=handle_list)

    add_parser = subparsers.add_parser("add", help="Add a provider and scaffold its directories.")
    add_parser.add_argument("provider_id", help="Stable provider ID such as ss-tv.")
    add_parser.add_argument("--name", help="Display name shown in the bot.")
    add_parser.add_argument("--refresh-url-env", help="Override the playlist refresh env var name.")
    add_parser.add_argument("--m3u-source", help="Override the provider M3U path.")
    add_parser.add_argument("--raw-export", help="Override the raw export JSON path.")
    add_parser.add_argument("--selector-dataset", help="Override the selector dataset JSON path.")
    add_parser.add_argument("--enable", dest="enabled", action="store_true", help="Enable the provider.")
    add_parser.add_argument("--disable", dest="enabled", action="store_false", help="Disable the provider.")
    add_parser.set_defaults(enabled=True)
    add_parser.add_argument(
        "--normalize-event-channels",
        dest="normalize_event_channels",
        action="store_true",
        help="Normalize event-based channel names in the selector dataset.",
    )
    add_parser.add_argument(
        "--no-normalize-event-channels",
        dest="normalize_event_channels",
        action="store_false",
        help="Do not normalize event-based channel names.",
    )
    add_parser.set_defaults(normalize_event_channels=False)
    add_parser.add_argument("--set-default", action="store_true", help="Set the provider as default_provider_id.")
    add_parser.add_argument(
        "--no-scaffold",
        action="store_true",
        help="Update the registry only without creating directories or a placeholder M3U file.",
    )
    add_parser.add_argument(
        "--build",
        action="store_true",
        help="Run the raw and selector dataset build scripts after writing the provider.",
    )
    add_parser.set_defaults(handler=handle_add)

    update_parser = subparsers.add_parser(
        "update",
        aliases=["edit"],
        help="Edit an existing provider and optionally rename its conventional files.",
    )
    update_parser.add_argument("provider_id", help="Existing provider ID.")
    update_parser.add_argument("--new-id", help="Rename the provider ID.")
    update_parser.add_argument("--name", help="Update the provider display name.")
    update_parser.add_argument("--refresh-url-env", help="Set a refresh env var name.")
    update_parser.add_argument(
        "--clear-refresh-url-env",
        action="store_true",
        help="Clear the explicit refresh env var so it falls back to the derived default.",
    )
    update_parser.add_argument("--m3u-source", help="Update the provider M3U path.")
    update_parser.add_argument("--raw-export", help="Update the raw export JSON path.")
    update_parser.add_argument("--selector-dataset", help="Update the selector dataset JSON path.")
    update_parser.add_argument("--enable", dest="enabled", action="store_const", const=True, help="Enable the provider.")
    update_parser.add_argument(
        "--disable",
        dest="enabled",
        action="store_const",
        const=False,
        help="Disable the provider.",
    )
    update_parser.set_defaults(enabled=None)
    update_parser.add_argument(
        "--normalize-event-channels",
        dest="normalize_event_channels",
        action="store_const",
        const=True,
        help="Normalize event-based channel names in the selector dataset.",
    )
    update_parser.add_argument(
        "--no-normalize-event-channels",
        dest="normalize_event_channels",
        action="store_const",
        const=False,
        help="Do not normalize event-based channel names.",
    )
    update_parser.set_defaults(normalize_event_channels=None)
    update_parser.add_argument("--set-default", action="store_true", help="Set the provider as default_provider_id.")
    update_parser.add_argument(
        "--clear-default",
        action="store_true",
        help="Clear default_provider_id if it currently points at this provider.",
    )
    update_parser.add_argument(
        "--resync-paths",
        action="store_true",
        help="Reset provider paths to the conventional channels/ and data/providers/ layout for the final provider ID.",
    )
    update_parser.add_argument(
        "--no-scaffold",
        action="store_true",
        help="Update the registry only without creating directories or a placeholder M3U file.",
    )
    update_parser.add_argument(
        "--build",
        action="store_true",
        help="Run the raw and selector dataset build scripts after writing the provider.",
    )
    update_parser.set_defaults(handler=handle_update)

    remove_parser = subparsers.add_parser("remove", help="Remove a provider from the registry.")
    remove_parser.add_argument("provider_id", help="Provider ID to remove.")
    remove_parser.add_argument(
        "--delete-files",
        action="store_true",
        help="Delete the configured M3U file and provider dataset files when removing the provider.",
    )
    remove_parser.set_defaults(handler=handle_remove)

    default_parser = subparsers.add_parser("set-default", help="Set the default provider ID.")
    default_parser.add_argument("provider_id", help="Provider ID to mark as the default.")
    default_parser.set_defaults(handler=handle_set_default)

    return parser.parse_args(argv)


def handle_list(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    payload = load_registry(config_path)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0

    providers = payload.get("providers", [])
    if not providers:
        print(f"No providers configured in {relative_display_path(config_path)}")
        return 0

    default_provider_id = normalize_provider_id(payload.get("default_provider_id"))
    print(f"Config: {relative_display_path(config_path)}")
    for provider in providers:
        provider_id = str(provider.get("id") or "").strip()
        enabled = bool(provider.get("enabled", True))
        name = str(provider.get("name") or provider_id).strip() or provider_id
        marker = " (default)" if normalize_provider_id(provider_id) == default_provider_id else ""
        print(f"- {provider_id}: {name} [{'enabled' if enabled else 'disabled'}]{marker}")
        print(f"  m3u={provider.get('m3u_source')}")
        print(f"  raw={provider.get('raw_export')}")
        print(f"  selector={provider.get('selector_dataset')}")
    return 0


def handle_add(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    payload = load_registry(config_path)
    providers = ensure_provider_list(payload)
    provider_id = validate_provider_id(args.provider_id)
    if find_provider_index(providers, provider_id) is not None:
        raise SystemExit(f"Provider already exists: {provider_id}")

    provider = make_provider_record(
        provider_id=provider_id,
        name=args.name,
        enabled=args.enabled,
        normalize_event_channels=args.normalize_event_channels,
        refresh_url_env=args.refresh_url_env,
        m3u_source=args.m3u_source,
        raw_export=args.raw_export,
        selector_dataset=args.selector_dataset,
    )
    providers.append(provider)

    if args.set_default or len(providers) == 1:
        payload["default_provider_id"] = provider_id

    write_registry(config_path, payload)
    if not args.no_scaffold:
        scaffold_provider_paths(provider)
    if args.build:
        run_build(provider_id, config_path)

    print(f"Added provider {provider_id} to {relative_display_path(config_path)}")
    return 0


def handle_update(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    payload = load_registry(config_path)
    providers = ensure_provider_list(payload)
    provider_id = validate_provider_id(args.provider_id)
    provider_index = find_provider_index(providers, provider_id)
    if provider_index is None:
        raise SystemExit(f"Provider not found: {provider_id}")

    existing = dict(providers[provider_index])
    final_provider_id = validate_provider_id(args.new_id) if args.new_id else provider_id
    if args.new_id:
        conflicting_index = find_provider_index(providers, final_provider_id)
        if conflicting_index is not None and conflicting_index != provider_index:
            raise SystemExit(f"Provider already exists: {final_provider_id}")

    if args.resync_paths:
        updated = make_provider_record(
            provider_id=final_provider_id,
            name=args.name or str(existing.get("name") or final_provider_id),
            enabled=bool(existing.get("enabled", True)) if args.enabled is None else args.enabled,
            normalize_event_channels=(
                bool(existing.get("normalize_event_channels", False))
                if args.normalize_event_channels is None
                else args.normalize_event_channels
            ),
            refresh_url_env=(
                None
                if args.clear_refresh_url_env
                else args.refresh_url_env if args.refresh_url_env is not None else str(existing.get("refresh_url_env") or "")
            ),
        )
    else:
        updated = dict(existing)
        updated["id"] = final_provider_id
        if args.name is not None:
            updated["name"] = args.name
        if args.enabled is not None:
            updated["enabled"] = args.enabled
        if args.normalize_event_channels is not None:
            updated["normalize_event_channels"] = args.normalize_event_channels
        if args.clear_refresh_url_env:
            updated["refresh_url_env"] = ""
        elif args.refresh_url_env is not None:
            updated["refresh_url_env"] = args.refresh_url_env

        if args.new_id:
            old_defaults = conventional_paths(provider_id)
            new_defaults = conventional_paths(final_provider_id)
            if normalize_stored_path(existing.get("m3u_source")) == old_defaults["m3u_source"] and args.m3u_source is None:
                updated["m3u_source"] = new_defaults["m3u_source"]
            if normalize_stored_path(existing.get("raw_export")) == old_defaults["raw_export"] and args.raw_export is None:
                updated["raw_export"] = new_defaults["raw_export"]
            if normalize_stored_path(existing.get("selector_dataset")) == old_defaults["selector_dataset"] and args.selector_dataset is None:
                updated["selector_dataset"] = new_defaults["selector_dataset"]

        if args.m3u_source is not None:
            updated["m3u_source"] = normalize_stored_path(args.m3u_source)
        if args.raw_export is not None:
            updated["raw_export"] = normalize_stored_path(args.raw_export)
        if args.selector_dataset is not None:
            updated["selector_dataset"] = normalize_stored_path(args.selector_dataset)

    updated = normalize_provider_record(updated)

    move_actions: list[str] = []
    if args.new_id:
        move_actions.extend(rename_conventional_files(existing, updated))

    providers[provider_index] = updated

    current_default = normalize_provider_id(payload.get("default_provider_id"))
    if args.set_default:
        payload["default_provider_id"] = final_provider_id
    elif args.clear_default and current_default == normalize_provider_id(provider_id):
        payload.pop("default_provider_id", None)
    elif current_default == normalize_provider_id(provider_id) and final_provider_id != provider_id:
        payload["default_provider_id"] = final_provider_id

    write_registry(config_path, payload)
    if not args.no_scaffold:
        scaffold_provider_paths(updated)
    if args.build:
        run_build(final_provider_id, config_path)

    print(f"Updated provider {provider_id} -> {final_provider_id} in {relative_display_path(config_path)}")
    for action in move_actions:
        print(action)
    return 0


def handle_remove(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    payload = load_registry(config_path)
    providers = ensure_provider_list(payload)
    provider_id = validate_provider_id(args.provider_id)
    provider_index = find_provider_index(providers, provider_id)
    if provider_index is None:
        raise SystemExit(f"Provider not found: {provider_id}")

    provider = providers.pop(provider_index)
    removed_default = normalize_provider_id(payload.get("default_provider_id")) == normalize_provider_id(provider_id)
    if removed_default:
        if providers:
            payload["default_provider_id"] = str(providers[0].get("id") or "").strip()
        else:
            payload.pop("default_provider_id", None)

    write_registry(config_path, payload)

    if args.delete_files:
        for action in delete_provider_files(provider):
            print(action)

    print(f"Removed provider {provider_id} from {relative_display_path(config_path)}")
    return 0


def handle_set_default(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    payload = load_registry(config_path)
    providers = ensure_provider_list(payload)
    provider_id = validate_provider_id(args.provider_id)
    if find_provider_index(providers, provider_id) is None:
        raise SystemExit(f"Provider not found: {provider_id}")
    payload["default_provider_id"] = provider_id
    write_registry(config_path, payload)
    print(f"Set default_provider_id={provider_id} in {relative_display_path(config_path)}")
    return 0


def resolve_config_path(value: str | Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


def load_registry(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {"providers": []}

    try:
        raw_payload = config_path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Failed to read {relative_display_path(config_path)}: {exc}") from exc

    if not raw_payload.strip():
        return {"providers": []}

    try:
        payload = json.loads(raw_payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Failed to read {relative_display_path(config_path)}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit(f"Provider registry must be a JSON object: {relative_display_path(config_path)}")
    return payload


def ensure_provider_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    providers = payload.get("providers")
    if providers is None:
        payload["providers"] = []
        return payload["providers"]
    if not isinstance(providers, list):
        raise SystemExit("Provider registry field 'providers' must be a JSON array")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        provider = normalize_provider_record(entry)
        normalized_id = normalize_provider_id(provider.get("id"))
        if not normalized_id or normalized_id in seen:
            continue
        seen.add(normalized_id)
        normalized.append(provider)
    payload["providers"] = normalized
    return normalized


def write_registry(config_path: Path, payload: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def validate_provider_id(value: str | None) -> str:
    provider_id = str(value or "").strip()
    if not provider_id:
        raise SystemExit("Provider ID is required")
    if not PROVIDER_ID_PATTERN.fullmatch(provider_id):
        raise SystemExit("Provider ID must match ^[A-Za-z0-9][A-Za-z0-9_-]*$")
    return provider_id


def normalize_provider_id(value: Any) -> str:
    return str(value or "").strip().lower()


def find_provider_index(providers: list[dict[str, Any]], provider_id: str) -> int | None:
    normalized_id = normalize_provider_id(provider_id)
    for index, provider in enumerate(providers):
        if normalize_provider_id(provider.get("id")) == normalized_id:
            return index
    return None


def conventional_paths(provider_id: str) -> dict[str, str]:
    return {
        "m3u_source": f"channels/{provider_id}.m3u",
        "raw_export": f"data/providers/{provider_id}/iptv_channels.json",
        "selector_dataset": f"data/providers/{provider_id}/iptv_channels_selector.json",
    }


def default_refresh_env(provider_id: str) -> str:
    normalized_id = re.sub(r"[^A-Za-z0-9]+", "_", provider_id.upper()).strip("_")
    return f"IPTV_REFRESH_URL_{normalized_id}" if normalized_id else ""


def default_provider_name(provider_id: str) -> str:
    parts = re.split(r"[-_]+", provider_id)
    words = [part for part in parts if part]
    return " ".join(word.upper() if len(word) <= 2 else word.capitalize() for word in words) or provider_id


def make_provider_record(
    *,
    provider_id: str,
    name: str | None,
    enabled: bool,
    normalize_event_channels: bool,
    refresh_url_env: str | None,
    m3u_source: str | None = None,
    raw_export: str | None = None,
    selector_dataset: str | None = None,
) -> dict[str, Any]:
    defaults = conventional_paths(provider_id)
    return {
        "id": provider_id,
        "name": (name or default_provider_name(provider_id)).strip(),
        "enabled": bool(enabled),
        "normalize_event_channels": bool(normalize_event_channels),
        "refresh_url_env": str(refresh_url_env if refresh_url_env is not None else default_refresh_env(provider_id)).strip(),
        "m3u_source": normalize_stored_path(m3u_source or defaults["m3u_source"]),
        "raw_export": normalize_stored_path(raw_export or defaults["raw_export"]),
        "selector_dataset": normalize_stored_path(selector_dataset or defaults["selector_dataset"]),
    }


def normalize_provider_record(provider: dict[str, Any]) -> dict[str, Any]:
    provider_id = validate_provider_id(provider.get("id"))
    defaults = conventional_paths(provider_id)
    return {
        "id": provider_id,
        "name": str(provider.get("name") or provider_id).strip() or provider_id,
        "enabled": bool(provider.get("enabled", True)),
        "normalize_event_channels": bool(provider.get("normalize_event_channels", False)),
        "refresh_url_env": str(provider.get("refresh_url_env") or "").strip(),
        "m3u_source": normalize_stored_path(provider.get("m3u_source") or defaults["m3u_source"]),
        "raw_export": normalize_stored_path(provider.get("raw_export") or defaults["raw_export"]),
        "selector_dataset": normalize_stored_path(provider.get("selector_dataset") or defaults["selector_dataset"]),
    }


def normalize_stored_path(value: Any) -> str:
    path_value = str(value or "").strip()
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.is_absolute():
        return path.as_posix()

    try:
        relative = path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return str(path.resolve())
    return relative.as_posix()


def repo_path(value: Any) -> Path:
    path_value = str(value or "").strip()
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


def relative_display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def scaffold_provider_paths(provider: dict[str, Any]) -> None:
    m3u_path = repo_path(provider.get("m3u_source"))
    raw_export_path = repo_path(provider.get("raw_export"))
    selector_dataset_path = repo_path(provider.get("selector_dataset"))

    m3u_path.parent.mkdir(parents=True, exist_ok=True)
    raw_export_path.parent.mkdir(parents=True, exist_ok=True)
    selector_dataset_path.parent.mkdir(parents=True, exist_ok=True)

    if not m3u_path.exists():
        m3u_path.write_text("#EXTM3U\n", encoding="utf-8")


def prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{label}{suffix}: ").strip()
    if value:
        return value
    return default or ""


def prompt_bool(label: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter yes or no.")


def prompt_provider_selection(config_path: Path, action: str) -> dict[str, Any] | None:
    payload = load_registry(config_path)
    providers = ensure_provider_list(payload)
    if not providers:
        print(f"No providers configured in {relative_display_path(config_path)}")
        return None

    print(f"Select a provider to {action}:")
    for index, provider in enumerate(providers, start=1):
        provider_id = str(provider.get("id") or "").strip()
        provider_name = str(provider.get("name") or provider_id).strip() or provider_id
        print(f"{index}. {provider_id} ({provider_name})")

    while True:
        value = input("Choice: ").strip()
        if value.isdigit():
            selected = int(value)
            if 1 <= selected <= len(providers):
                return providers[selected - 1]

        normalized = normalize_provider_id(value)
        if normalized:
            for provider in providers:
                if normalize_provider_id(provider.get("id")) == normalized:
                    return provider
        print("Enter a valid number or provider ID.")


def prompt_provider_config(config_path: Path, *, existing: dict[str, Any] | None = None) -> argparse.Namespace:
    existing = existing or {}
    provider_id = prompt_text("Provider ID", str(existing.get("id") or "")).strip()
    provider_id = validate_provider_id(provider_id)
    current_name = str(existing.get("name") or default_provider_name(provider_id)).strip() or provider_id
    defaults = conventional_paths(provider_id)

    refresh_default = str(existing.get("refresh_url_env") or default_refresh_env(provider_id)).strip()
    current_m3u = normalize_stored_path(existing.get("m3u_source") or defaults["m3u_source"])
    current_raw = normalize_stored_path(existing.get("raw_export") or defaults["raw_export"])
    current_selector = normalize_stored_path(existing.get("selector_dataset") or defaults["selector_dataset"])

    return argparse.Namespace(
        config=str(config_path),
        provider_id=provider_id,
        new_id=None,
        name=prompt_text("Display name", current_name),
        refresh_url_env=prompt_text("Refresh env var", refresh_default),
        clear_refresh_url_env=False,
        m3u_source=prompt_text("M3U source path", current_m3u),
        raw_export=prompt_text("Raw export path", current_raw),
        selector_dataset=prompt_text("Selector dataset path", current_selector),
        enabled=prompt_bool("Enabled", default=bool(existing.get("enabled", True))),
        normalize_event_channels=prompt_bool(
            "Normalize event channels",
            default=bool(existing.get("normalize_event_channels", False)),
        ),
        set_default=prompt_bool(
            "Set as default provider",
            default=(normalize_provider_id(existing.get("id")) == normalize_provider_id(load_registry(config_path).get("default_provider_id"))),
        ),
        clear_default=False,
        resync_paths=False,
        no_scaffold=not prompt_bool("Create missing directories and starter M3U file", default=True),
        build=prompt_bool("Run dataset build after saving", default=False),
    )


def run_interactive() -> int:
    config_path = DEFAULT_CONFIG_PATH
    print(f"Interactive provider manager for {relative_display_path(config_path)}")

    while True:
        print()
        print("1. List providers")
        print("2. Add provider")
        print("3. Edit provider")
        print("4. Remove provider")
        print("5. Set default provider")
        print("6. Quit")
        choice = input("Choose an action: ").strip().lower()

        try:
            if choice in {"1", "list", "l"}:
                handle_list(argparse.Namespace(config=str(config_path), json=False))
            elif choice in {"2", "add", "a"}:
                args = prompt_provider_config(config_path)
                handle_add(args)
            elif choice in {"3", "edit", "update", "u"}:
                provider = prompt_provider_selection(config_path, "edit")
                if provider is None:
                    continue
                args = prompt_provider_config(config_path, existing=provider)
                args.provider_id = str(provider.get("id") or "").strip()
                rename_provider = prompt_bool("Rename provider ID", default=False)
                if rename_provider:
                    args.new_id = validate_provider_id(prompt_text("New provider ID", args.provider_id))
                    args.resync_paths = prompt_bool("Resync paths to the new provider ID", default=True)
                handle_update(args)
            elif choice in {"4", "remove", "r"}:
                provider = prompt_provider_selection(config_path, "remove")
                if provider is None:
                    continue
                provider_id = str(provider.get("id") or "").strip()
                if not prompt_bool(f"Remove provider {provider_id}", default=False):
                    continue
                delete_files = prompt_bool("Delete scaffolded provider files", default=False)
                handle_remove(
                    argparse.Namespace(
                        config=str(config_path),
                        provider_id=provider_id,
                        delete_files=delete_files,
                    )
                )
            elif choice in {"5", "default", "d"}:
                provider = prompt_provider_selection(config_path, "set as default")
                if provider is None:
                    continue
                handle_set_default(
                    argparse.Namespace(
                        config=str(config_path),
                        provider_id=str(provider.get("id") or "").strip(),
                    )
                )
            elif choice in {"6", "quit", "q", "exit"}:
                return 0
            else:
                print("Choose 1-6.")
        except (KeyboardInterrupt, EOFError):
            print()
            return 0
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(exc)


def rename_conventional_files(existing: dict[str, Any], updated: dict[str, Any]) -> list[str]:
    actions: list[str] = []

    old_m3u = repo_path(existing.get("m3u_source"))
    new_m3u = repo_path(updated.get("m3u_source"))
    if old_m3u != new_m3u:
        maybe_move_path(old_m3u, new_m3u)
        if old_m3u.exists() or new_m3u.exists():
            actions.append(f"Moved {relative_display_path(old_m3u)} -> {relative_display_path(new_m3u)}")

    old_raw = repo_path(existing.get("raw_export"))
    new_raw = repo_path(updated.get("raw_export"))
    old_selector = repo_path(existing.get("selector_dataset"))
    new_selector = repo_path(updated.get("selector_dataset"))

    if old_raw.parent == old_selector.parent and new_raw.parent == new_selector.parent and old_raw.parent != new_raw.parent:
        maybe_move_directory(old_raw.parent, new_raw.parent)
        if old_raw.parent.exists() or new_raw.parent.exists():
            actions.append(
                f"Moved {relative_display_path(old_raw.parent)} -> {relative_display_path(new_raw.parent)}"
            )
        return actions

    if old_raw != new_raw:
        maybe_move_path(old_raw, new_raw)
        if old_raw.exists() or new_raw.exists():
            actions.append(f"Moved {relative_display_path(old_raw)} -> {relative_display_path(new_raw)}")
    if old_selector != new_selector:
        maybe_move_path(old_selector, new_selector)
        if old_selector.exists() or new_selector.exists():
            actions.append(f"Moved {relative_display_path(old_selector)} -> {relative_display_path(new_selector)}")
    return actions


def maybe_move_directory(source: Path, destination: Path) -> None:
    if not source.exists() or source == destination:
        return
    if destination.exists():
        raise SystemExit(
            f"Cannot rename {relative_display_path(source)} because {relative_display_path(destination)} already exists"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)


def maybe_move_path(source: Path, destination: Path) -> None:
    if not source.exists() or source == destination:
        return
    if destination.exists():
        raise SystemExit(
            f"Cannot rename {relative_display_path(source)} because {relative_display_path(destination)} already exists"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)


def delete_provider_files(provider: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    m3u_path = repo_path(provider.get("m3u_source"))
    raw_path = repo_path(provider.get("raw_export"))
    selector_path = repo_path(provider.get("selector_dataset"))

    if m3u_path.exists() and m3u_path.is_file():
        m3u_path.unlink()
        actions.append(f"Deleted {relative_display_path(m3u_path)}")
        prune_empty_parents(m3u_path.parent)

    if raw_path.parent == selector_path.parent and raw_path.parent.exists():
        shutil.rmtree(raw_path.parent)
        actions.append(f"Deleted {relative_display_path(raw_path.parent)}")
        prune_empty_parents(raw_path.parent.parent)
        return actions

    for path in (raw_path, selector_path):
        if path.exists() and path.is_file():
            path.unlink()
            actions.append(f"Deleted {relative_display_path(path)}")
            prune_empty_parents(path.parent)
    return actions


def prune_empty_parents(start: Path) -> None:
    current = start
    root = PROJECT_ROOT.resolve()
    while current != root and current.exists() and current.is_dir():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def run_build(provider_id: str, config_path: Path) -> None:
    if config_path != DEFAULT_CONFIG_PATH:
        raise SystemExit(
            "--build only supports the repo's default providers.json because the build scripts do not accept a custom registry path"
        )

    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_iptv_json.py"), "--provider", provider_id],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_iptv_selector_json.py"), "--provider", provider_id],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return run_interactive()

    args = parse_args(argv)
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())