import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _csv_ids(raw: str) -> list[int]:
    out: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


@dataclass(frozen=True)
class Config:
    token: str
    staff_channel_id: int
    support_channel_id: int
    tickets_category_id: int
    reports_channel_ids: list[int]

    # ✅ NEW: split pings
    tv_staff_ping_user_ids: list[int]
    vod_staff_ping_user_ids: list[int]

    # (kept for backwards compatibility / fallback)
    staff_ping_user_ids: list[int]

    public_updates: bool
    double_confirmation: bool
    db_path: str
    tmdb_bearer_token: str
    tvdb_key: str
    staff_role_id: int
    ss_vod_remux_role_id: int
    modlogs_channel_id: int
    responses_channel_id: int
    transcripts_channel_id: int


def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN in .env")

    staff_channel_id = int(os.getenv("STAFF_CHANNEL_ID", "0"))
    if staff_channel_id <= 0:
        raise RuntimeError("Missing STAFF_CHANNEL_ID in .env")

    support_channel_id = int(os.getenv("SUPPORT_CHANNEL_ID", "0"))
    tickets_category_id = int(os.getenv("TICKETS_CATEGORY_ID", "0"))

    reports_channel_ids = _csv_ids(os.getenv("REPORTS_CHANNEL_IDS", "").strip())
    if not reports_channel_ids:
        legacy = os.getenv("REPORTS_CHANNEL_ID", "").strip()
        if legacy.isdigit():
            reports_channel_ids = [int(legacy)]
    if not reports_channel_ids:
        raise RuntimeError("Missing REPORTS_CHANNEL_IDS (or REPORTS_CHANNEL_ID) in .env")

    # old single list (fallback)
    staff_ping_user_ids = _csv_ids(os.getenv("STAFF_PING_USER_IDS", "").strip())

    # ✅ NEW split lists (fallback to old list if not set)
    tv_staff_ping_user_ids = _csv_ids(os.getenv("TV_STAFF_PING_USER_IDS", "").strip())
    vod_staff_ping_user_ids = _csv_ids(os.getenv("VOD_STAFF_PING_USER_IDS", "").strip())

    if not tv_staff_ping_user_ids:
        tv_staff_ping_user_ids = staff_ping_user_ids
    if not vod_staff_ping_user_ids:
        vod_staff_ping_user_ids = staff_ping_user_ids

    public_updates = _get_bool("PUBLIC_UPDATES", True)
    double_confirmation = _get_bool("DOUBLE_CONFIRMATION", False)
    db_path = os.getenv("DB_PATH", "./data/reports.sqlite3").strip()
    tmdb_bearer_token = os.getenv("TMDB_BEARER_TOKEN", "").strip()
    tvdb_key = os.getenv("TVDB_KEY", "").strip()

    staff_role_id = int(os.getenv("STAFF_ROLE_ID", "0"))
    if staff_role_id <= 0:
        raise RuntimeError("Missing STAFF_ROLE_ID in .env")

    ss_vod_remux_role_id = int(os.getenv("SS_VOD_REMUX_ROLE_ID", "0"))
    if ss_vod_remux_role_id <= 0:
        raise RuntimeError("Missing SS_VOD_REMUX_ROLE_ID in .env")

    modlogs_channel_id = int(os.getenv("MODLOGS_CHANNEL_ID", "0"))
    transcripts_channel_id = int(os.getenv("TRANSCRIPTS_CHANNEL_ID", "0"))

    responses_channel_id = int(os.getenv("RESPONSES_CHANNEL_ID", "0"))
    if public_updates and responses_channel_id <= 0:
        raise RuntimeError("PUBLIC_UPDATES is enabled but RESPONSES_CHANNEL_ID is missing/invalid in .env")

    return Config(
        token=token,
        staff_channel_id=staff_channel_id,
        support_channel_id=support_channel_id,
        tickets_category_id=tickets_category_id,
        reports_channel_ids=reports_channel_ids,
        tv_staff_ping_user_ids=tv_staff_ping_user_ids,
        vod_staff_ping_user_ids=vod_staff_ping_user_ids,
        staff_ping_user_ids=staff_ping_user_ids,
        public_updates=public_updates,
        double_confirmation=double_confirmation,
        db_path=db_path,
        tmdb_bearer_token=tmdb_bearer_token,
        tvdb_key=tvdb_key,
        staff_role_id=staff_role_id,
        ss_vod_remux_role_id=ss_vod_remux_role_id,
        modlogs_channel_id=modlogs_channel_id,
        transcripts_channel_id=transcripts_channel_id,
        responses_channel_id=responses_channel_id,
    )
