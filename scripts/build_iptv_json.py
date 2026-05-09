from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.providers import resolve_m3u_source_path, resolve_raw_export_path


ATTR_PATTERN = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a raw IPTV JSON export from an M3U playlist.",
    )
    parser.add_argument(
        "--provider",
        help="Provider ID from providers.json.",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Path to the source M3U playlist.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write the parsed IPTV JSON export.",
    )
    return parser.parse_args()


def split_extinf_payload(line: str) -> tuple[str, str]:
    in_quotes = False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ',' and not in_quotes:
            return line[:index], line[index + 1 :]
    return line, ""


def build_export(source_path: Path) -> dict:
    lines = source_path.read_text(encoding="utf-8").splitlines()
    entries = []
    index = 0

    while index < len(lines):
        line = lines[index].strip()
        if not line or line == "#EXTM3U":
            index += 1
            continue
        if not line.startswith("#EXTINF:"):
            index += 1
            continue

        metadata, display_name = split_extinf_payload(line)
        attrs = dict(ATTR_PATTERN.findall(metadata))

        url = ""
        next_index = index + 1
        while next_index < len(lines):
            candidate = lines[next_index].strip()
            if candidate and not candidate.startswith("#"):
                url = candidate
                break
            next_index += 1

        entries.append(
            {
                "name": display_name.strip() or attrs.get("tvg-name", "").strip(),
                "url": url,
                "category": attrs.get("group-title", "").strip(),
                "tvg_id": attrs.get("tvg-id", "").strip(),
                "tvg_name": attrs.get("tvg-name", "").strip(),
                "tvg_logo": attrs.get("tvg-logo", "").strip(),
                "attributes": attrs,
            }
        )
        index = next_index + 1

    category_counts = Counter(entry["category"] or "Uncategorized" for entry in entries)
    unique_names = []
    seen = set()
    for entry in entries:
        name = entry["name"].strip()
        lowered = name.lower()
        if not name or lowered in seen:
            continue
        seen.add(lowered)
        unique_names.append(name)

    return {
        "source_file": source_path.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channel_count": len(entries),
        "category_count": len(category_counts),
        "presence_names": unique_names,
        "categories": [
            {"name": name, "count": count}
            for name, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0].lower()))
        ],
        "channels": entries,
    }


def main() -> None:
    args = parse_args()
    source_path = Path(args.input) if args.input else resolve_m3u_source_path(args.provider)
    output_path = Path(args.output) if args.output else resolve_raw_export_path(args.provider)
    payload = build_export(source_path)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(
        "Built raw IPTV export:",
        {
            "provider": args.provider or "legacy",
            "source": str(source_path),
            "output": str(output_path),
            "channel_count": payload["channel_count"],
            "category_count": payload["category_count"],
        },
    )


if __name__ == "__main__":
    main()