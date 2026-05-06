from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.iptv import write_selector_dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a selector-friendly IPTV dataset from the raw IPTV export.",
    )
    parser.add_argument(
        "--input",
        default="data/iptv_channels.json",
        help="Path to the raw IPTV export JSON.",
    )
    parser.add_argument(
        "--output",
        default="data/iptv_channels_selector.json",
        help="Path to write the selector dataset JSON.",
    )
    parser.add_argument(
        "--max-label-length",
        type=int,
        default=100,
        help="Maximum display label length for channel names.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selector_dataset = write_selector_dataset(
        source_path=Path(args.input),
        output_path=Path(args.output),
        max_label_length=args.max_label_length,
    )
    print(
        "Built selector dataset:",
        {
            "category_count": selector_dataset["category_count"],
            "channel_count": selector_dataset["channel_count"],
            "stats": selector_dataset["stats"],
            "output": str(Path(args.output)),
        },
    )


if __name__ == "__main__":
    main()