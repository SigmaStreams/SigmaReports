from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.providers import configured_providers, provider_refresh_url_env, resolve_m3u_source_path


DEFAULT_ENV_FILE = PROJECT_ROOT / ".iptv-refresh.env"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download IPTV playlists from provider-specific URLs and rebuild raw + selector datasets.",
    )
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="Provider ID to refresh. Repeat for multiple providers. Defaults to enabled providers.",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Optional dotenv file containing IPTV refresh URL variables.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout in seconds for playlist downloads.",
    )
    parser.add_argument(
        "--skip-disabled",
        action="store_true",
        help="Skip configured providers that are disabled. This is the default when --provider is not specified.",
    )
    return parser.parse_args()


def load_refresh_env(env_file: str) -> None:
    candidate = Path(env_file)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    if candidate.exists():
        load_dotenv(candidate, override=False)


def selected_providers(provider_ids: list[str] | None, *, skip_disabled: bool) -> list[dict]:
    providers = configured_providers()
    if provider_ids:
        requested = {provider_id.strip().lower() for provider_id in provider_ids if provider_id.strip()}
        return [provider for provider in providers if str(provider.get("id") or "").strip().lower() in requested]
    if skip_disabled or True:
        return [provider for provider in providers if provider.get("enabled")]
    return providers


def download_playlist(url: str, destination: Path, timeout: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "SigmaReports IPTV Refresh"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()

    with tempfile.NamedTemporaryFile(dir=str(destination.parent), delete=False) as temp_file:
        temp_file.write(payload)
        temp_path = Path(temp_file.name)

    temp_path.replace(destination)


def run_build(provider_id: str) -> None:
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_iptv_json.py"), "--provider", provider_id],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_iptv_selector_json.py"), "--provider", provider_id],
        check=True,
    )


def main() -> None:
    args = parse_args()
    load_refresh_env(args.env_file)

    providers = selected_providers(args.providers, skip_disabled=args.skip_disabled or not bool(args.providers))
    if not providers:
        raise SystemExit("No providers matched the refresh request.")

    failures: list[str] = []
    for provider in providers:
        provider_id = str(provider.get("id") or "").strip()
        env_name = provider_refresh_url_env(provider_id)
        playlist_url = os.getenv(env_name, "").strip()
        if not playlist_url:
            failures.append(f"{provider_id}: missing env var {env_name}")
            continue

        playlist_path = resolve_m3u_source_path(provider_id)
        print(f"Refreshing {provider_id} from {env_name} -> {playlist_path}")
        download_playlist(playlist_url, playlist_path, args.timeout)
        run_build(provider_id)

    if failures:
        raise SystemExit("Refresh completed with errors:\n- " + "\n- ".join(failures))


if __name__ == "__main__":
    main()