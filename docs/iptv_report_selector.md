# IPTV Report Selector Plan

This note describes how to consume `data/iptv_channels.json` and `data/iptv_channels_selector.json` when the TV report flow moves from free-text channel entry to guided selection.

## Data Inputs

- `data/iptv_channels.json` remains the raw parsed playlist export.
- `data/iptv_channels_selector.json` is the selector-ready derivative built by `scripts/build_iptv_selector_json.py`.
- `bot/iptv.py` is the normalization layer that filters malformed rows, drops empty values, deduplicates `(category, name)` pairs, and truncates labels for Discord UI use.

## Recommended Discord Flow

1. Open the TV report entry point.
2. Show a category picker sourced from `categories[].name`.
3. After category selection, show a channel picker scoped to that category only.
4. Use `display_name` for the visible channel label and `selector_key` for the interaction value.
5. Resolve `selector_key` back to the selected channel record server-side.
6. Submit the existing TV payload fields with plain strings:
   - `channel_name = selected_channel["name"]`
   - `channel_category = selected_channel["category"]`

## UI Constraints

- Do not load all channels into a single static select menu.
- Some channel names exceed 100 characters, so the UI should show `display_name`, not raw `name`.
- Category selection is small enough for a filtered or paged select.
- Channel selection should be category-scoped and is a good fit for autocomplete or a paged select.

## Loader Rules

- Skip rows with empty `name`, `category`, or `url`.
- Skip rows whose `name` still contains `tvg-name=`, `tvg-logo=`, or `group-title=`.
- Deduplicate on `(category, name)`.
- Prefer a duplicate row that has `tvg_id` populated.
- Keep the raw `url`, `tvg_id`, `tvg_name`, and `tvg_logo` fields available for later staff tooling.