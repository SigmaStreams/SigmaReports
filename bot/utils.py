from __future__ import annotations

import discord
from datetime import datetime, timezone
from typing import Optional


def report_subject(report_type: str, payload: dict) -> str:
    rt = (report_type or "").lower()

    if rt == "tv":
        name = (payload or {}).get("channel_name") or "TV report"
        return str(name)

    if rt == "vod":
        title = (payload or {}).get("title") or "VOD report"
        return str(title)

    return "Report"


def _safe_channel_name(ch) -> str:
    try:
        return ch.mention  # type: ignore
    except Exception:
        try:
            return f"#{ch.name}"  # type: ignore
        except Exception:
            return "Unknown"


def _as_user_label(user: discord.abc.User) -> str:
    return f"{user.mention} ({user.id})"


def _normalize_report_type(rt: str) -> str:
    rt = (rt or "").strip().lower()
    if rt == "tv":
        return "TV"
    if rt == "vod":
        return "VOD"
    return rt.upper() if rt else "REPORT"


def _ref_link_field(payload: dict) -> tuple[str, str] | None:
    link = (payload or {}).get("reference_link")
    if not link:
        return None

    link_str = str(link).strip()
    if not link_str:
        return None

    label = "Reference"
    lower = link_str.lower()
    if "thetvdb" in lower:
        label = "TheTVDB"
    elif "themoviedb" in lower or "tmdb" in lower:
        label = "TMDB"
    elif "imdb" in lower:
        label = "IMDb"

    return ("Reference", f"[{label}]({link_str})")


def _vod_type_label(payload: dict) -> str:
    raw = str((payload or {}).get("content_type") or "").strip().lower()
    if raw == "movie":
        return "Movie"
    if raw == "tv":
        return "TV Show"

    ref = str((payload or {}).get("reference_link") or "").strip().lower()
    if "thetvdb" in ref:
        return "TV Show"
    if "themoviedb" in ref or "tmdb" in ref:
        return "Movie"

    return "Not provided"


def _vod_language_label(payload: dict) -> str:
    language = str((payload or {}).get("language") or "").strip()
    return language or "Not provided"


def _vod_requested_label(payload: dict) -> str:
    requested = str((payload or {}).get("requested_via_bot") or "").strip()
    return requested or "Not provided"


def _vod_device_label(payload: dict) -> str:
    device = str((payload or {}).get("device") or "").strip()
    return device or "Not provided"


def _vod_4k_label(payload: dict) -> str:
    value = str((payload or {}).get("is_4k") or "").strip()
    if value:
        return value

    quality = str((payload or {}).get("quality") or "").strip().lower()
    if quality == "4k":
        return "Yes"
    if quality:
        return "No"
    return "Not provided"


def _iso_to_discord_ts(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except Exception:
        return None


async def try_dm(user: discord.abc.User, message: str) -> bool:
    try:
        await user.send(message)
        return True
    except Exception:
        return False


def build_staff_embed(
    report_id: int,
    report_type: str,
    reporter: discord.abc.User,
    source_channel,
    payload: dict,
    status: str,
    ticket_channel_id: int | None = None,
    claimed_by_user_id: int | None = None,
    claimed_at: str | None = None,
    resolved_by_id: int | None = None,
    resolved_note: str | None = None,
) -> discord.Embed:
    rt = _normalize_report_type(report_type)
    subject = report_subject(report_type, payload)

    title = f"Report #{report_id} — {rt} — {subject}"
    embed = discord.Embed(title=title)

    status_txt = str(status or "Open").strip()
    status_low = status_txt.lower()

    embed.add_field(name="Status", value=status_txt, inline=False)

    # Claim info
    if claimed_by_user_id:
        claim_line = f"<@{int(claimed_by_user_id)}>"
        ts = _iso_to_discord_ts(claimed_at)
        if ts:
            claim_line += f" • {ts}"
        embed.add_field(name="Claimed by", value=claim_line, inline=False)

    # Outcome info (Resolved / Not Resolved)
    if status_low in ("resolved", "not resolved") and resolved_by_id:
        field_name = "Resolved by" if status_low == "resolved" else "Closed by"
        embed.add_field(name=field_name, value=f"<@{int(resolved_by_id)}>", inline=False)

    if status_low in ("resolved", "not resolved") and resolved_note:
        field_name = "Resolution details" if status_low == "resolved" else "Closure details"
        embed.add_field(name=field_name, value=str(resolved_note)[:1024], inline=False)

    embed.add_field(name="Reporter", value=_as_user_label(reporter), inline=False)
    embed.add_field(name="Reported from", value=_safe_channel_name(source_channel), inline=False)

    if rt == "TV":
        provider = (payload or {}).get("provider_name") or (payload or {}).get("provider_id") or ""
        ch_name = (payload or {}).get("channel_name") or "Unknown"
        ch_cat = (payload or {}).get("channel_category") or "Unknown"
        issue = (payload or {}).get("issue") or "—"

        if str(provider).strip():
            embed.add_field(name="Provider", value=str(provider), inline=True)
        embed.add_field(name="Channel", value=str(ch_name), inline=True)
        embed.add_field(name="Category", value=str(ch_cat), inline=True)
        embed.add_field(name="Issue", value=str(issue), inline=False)

    if rt == "VOD":
        vod_title = (payload or {}).get("title") or "Unknown"
        requested = _vod_requested_label(payload)
        language = _vod_language_label(payload)
        device = _vod_device_label(payload)
        is_4k = _vod_4k_label(payload)
        content_type = _vod_type_label(payload)
        issue = (payload or {}).get("issue") or "—"

        embed.add_field(name="Title", value=str(vod_title), inline=False)
        embed.add_field(name="Requested Through Bot", value=str(requested), inline=True)
        embed.add_field(name="English or Foreign", value=str(language), inline=True)
        embed.add_field(name="Device", value=str(device), inline=True)

        ref = _ref_link_field(payload)
        if ref:
            embed.add_field(name=ref[0], value=ref[1], inline=True)

        embed.add_field(name="4K Title", value=str(is_4k), inline=True)
        embed.add_field(name="Movie or TV Show", value=str(content_type), inline=True)

        embed.add_field(name="Issue", value=str(issue), inline=False)

    # Ticket link (hide once closed)
    if ticket_channel_id and status_low not in ("resolved", "not resolved"):
        embed.add_field(name="Ticket", value=f"<#{int(ticket_channel_id)}>", inline=False)

    embed.add_field(
        name="Staff actions",
        value=(
            "✅ **Resolved** — mark the report as resolved and notify the reporter\n"
            "❌ **Not Resolved** — close the report with required details explaining why "
            "(e.g., issue cannot be replicated)\n"
            "🎫 **Open ticket** — create a private ticket channel for staff + the reporter\n\n"
            "When working inside a ticket, use **Resolve** or **Not Resolved** there to finish and close it."
        ),
        inline=False,
    )

    return embed
