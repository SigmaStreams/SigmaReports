import io
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord

from bot.db import ReportDB
from bot.tmdb import search_tmdb_movies
from bot.tvdb import search_tvdb_series
from bot.utils import build_staff_embed, report_subject, try_dm
from bot.views import ReportActionView


def build_staff_ping(ping_ids: list[int]) -> str:
    if not ping_ids:
        return ""
    return " ".join(f"<@{uid}>" for uid in ping_ids)


def _get_ping_ids_for_report(cfg, report_kind: str) -> list[int]:
    """
    report_kind:
      - "tv"  -> tv_staff_ping_user_ids
      - "vod" -> vod_staff_ping_user_ids

    Falls back to staff_ping_user_ids if split lists aren't present or empty.
    """
    fallback = list(getattr(cfg, "staff_ping_user_ids", []) or [])

    if report_kind == "tv":
        ids = list(getattr(cfg, "tv_staff_ping_user_ids", []) or [])
        return ids if ids else fallback

    if report_kind == "vod":
        ids = list(getattr(cfg, "vod_staff_ping_user_ids", []) or [])
        return ids if ids else fallback

    return fallback


# ----------------------------
# Public updates (responses channel)
# ----------------------------

def _get_responses_channel_id_from_bot(interaction: discord.Interaction) -> int:
    """
    Pull RESPONSES_CHANNEL_ID from the bot config if available.
    Keeps modals.py independent from direct env reads.
    """
    cfg = getattr(interaction.client, "cfg", None)
    return int(getattr(cfg, "responses_channel_id", 0) or 0)


async def _try_public_update(
    interaction: discord.Interaction,
    responses_channel_id: int,
    reporter: discord.abc.User,
    message: str,
) -> None:
    """
    Best-effort public update in the configured responses channel.
    Pings reporter + posts same message as DM (single post).
    """
    if not interaction.guild:
        return

    cid = int(responses_channel_id or 0)
    if cid <= 0:
        return

    ch = interaction.guild.get_channel(cid)
    if not isinstance(ch, discord.TextChannel):
        return

    try:
        await ch.send(
            content=f"{reporter.mention}\n{message}",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except Exception:
        pass


# ----------------------------
# Ticket transcripts (transcripts channel + DM)
# ----------------------------

def _get_transcripts_channel_id_from_bot(interaction: discord.Interaction) -> int:
    """
    Pull TRANSCRIPTS_CHANNEL_ID from the bot config if available.
    """
    cfg = getattr(interaction.client, "cfg", None)
    return int(getattr(cfg, "transcripts_channel_id", 0) or 0)


def _fmt_ts(dt: datetime) -> str:
    # simple, stable timestamp for text files
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _build_channel_transcript_text(ch: discord.TextChannel, *, limit: int = 500) -> str:
    """
    Builds a plain-text transcript. Best effort.
    """
    lines: list[str] = []
    header = [
        f"Transcript for #{ch.name} ({ch.id})",
        f"Guild: {ch.guild.name} ({ch.guild.id})",
        f"Generated: {_fmt_ts(datetime.now(timezone.utc))}",
        "-" * 72,
        "",
    ]
    lines.extend(header)

    try:
        async for m in ch.history(limit=limit, oldest_first=True):
            created = m.created_at or datetime.now(timezone.utc)
            author = getattr(m.author, "display_name", None) or str(m.author)
            author_id = getattr(m.author, "id", "unknown")
            content = (m.content or "").replace("\r\n", "\n").replace("\r", "\n")

            lines.append(f"[{_fmt_ts(created)}] {author} ({author_id}):")
            if content.strip():
                lines.append(content)
            else:
                lines.append("—")

            # attachments
            if m.attachments:
                lines.append("Attachments:")
                for a in m.attachments:
                    try:
                        lines.append(f"- {a.filename}: {a.url}")
                    except Exception:
                        lines.append("- (attachment)")

            # embeds (keep it lightweight)
            if m.embeds:
                lines.append(f"Embeds: {len(m.embeds)}")

            lines.append("")  # spacer
    except Exception as e:
        lines.append("")
        lines.append(f"[Transcript generation error: {e!r}]")

    return "\n".join(lines)


async def _try_send_transcript(
    interaction: discord.Interaction,
    reporter: discord.abc.User | None,
    report_id: int,
    outcome: str,
    ch: discord.TextChannel | None,
) -> None:
    """
    Best-effort:
      - posts transcript file to TRANSCRIPTS_CHANNEL_ID (if set)
      - DMs the same file to the reporter (if available)
    """
    if not interaction.guild or not ch:
        return

    transcripts_cid = _get_transcripts_channel_id_from_bot(interaction)
    if transcripts_cid <= 0 and reporter is None:
        return

    text = await _build_channel_transcript_text(ch)
    filename = f"report-{int(report_id)}-{outcome.lower().replace(' ', '-')}-transcript.txt"

    data = text.encode("utf-8", errors="replace")
    file_for_channel = discord.File(io.BytesIO(data), filename=filename)
    file_for_dm = discord.File(io.BytesIO(data), filename=filename)

    # Post to transcripts channel
    if transcripts_cid > 0:
        tchan = interaction.guild.get_channel(int(transcripts_cid))
        if isinstance(tchan, discord.TextChannel):
            try:
                await tchan.send(
                    content=(
                        f"Transcript — report **#{int(report_id)}** — **{outcome}**\n"
                        f"Source channel: {ch.mention} ({ch.id})"
                    ),
                    file=file_for_channel,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass

    # DM reporter
    if reporter is not None:
        try:
            await reporter.send(
                content=f"Transcript for your report **#{int(report_id)}** ({outcome}).",
                file=file_for_dm,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass


# ----------------------------
# Reference link validation (TVDB for TV shows, TMDB for movies)
# ----------------------------

def _parse_host_path(url: str) -> tuple[str, str, str] | None:
    u = (url or "").strip()
    if not u:
        return None
    try:
        p = urlparse(u)
    except Exception:
        return None

    if p.scheme not in ("http", "https"):
        return None

    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    path = (p.path or "").strip()
    return (u, host, path)


def _is_tvdb_series_link(url: str) -> bool:
    parsed = _parse_host_path(url)
    if not parsed:
        return False
    _, host, path = parsed

    if host != "thetvdb.com":
        return False

    path = path.strip("/")
    return path.startswith("series/") and len(path.split("/", 1)[-1].strip()) > 0


def _is_tmdb_movie_link(url: str) -> bool:
    parsed = _parse_host_path(url)
    if not parsed:
        return False
    _, host, path = parsed

    if host != "themoviedb.org":
        return False

    path = path.strip("/")
    return path.startswith("movie/") and len(path.split("/", 1)[-1].strip()) > 0


def _normalize_vod_language(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized == "english":
        return "English"
    if normalized == "foreign":
        return "Foreign"
    return "Unknown"


def _normalize_vod_4k(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in ("yes", "true", "4k"):
        return "Yes"
    if normalized in ("no", "false", "fhd", "non-4k"):
        return "No"
    return "Unknown"


def _normalize_vod_content_type(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in ("movie", "movies"):
        return "movie"
    if normalized in ("tv", "tv show", "tv shows", "show"):
        return "tv"
    return "unknown"


def _validate_vod_reference_link(content_type: str, url: str) -> str | None:
    normalized_type = _normalize_vod_content_type(content_type)
    if normalized_type == "movie" and not _is_tmdb_movie_link(url):
        return (
            "❌ That reference link isn’t valid for a **movie**.\n\n"
            "Please re-submit using a **TMDB movie** link like:\n"
            "• <https://www.themoviedb.org/movie/14161-2012>"
        )

    if normalized_type == "tv" and not _is_tvdb_series_link(url):
        return (
            "❌ That reference link isn’t valid for a **TV show**.\n\n"
            "Please re-submit using a **TheTVDB series** link like:\n"
            "• <https://www.thetvdb.com/series/smallville>"
        )

    if normalized_type not in ("movie", "tv"):
        return "❌ Select whether this is a movie or TV show before continuing."

    return None


async def _submit_vod_report(interaction: discord.Interaction, db: ReportDB, cfg, payload: dict) -> int:
    report_id = db.create_report(
        "vod",
        interaction.user.id,
        interaction.guild.id,
        interaction.channel.id,
        payload,
    )

    staff_channel = interaction.guild.get_channel(cfg.staff_channel_id)
    if not isinstance(staff_channel, discord.TextChannel):
        return await interaction.response.send_message("❌ Staff channel not found.", ephemeral=True)

    embed = build_staff_embed(
        report_id,
        "vod",
        interaction.user,
        interaction.channel,
        payload,
        "Open",
    )

    view = ReportActionView(
        db,
        cfg.staff_channel_id,
        cfg.support_channel_id,
        cfg.public_updates,
        cfg.staff_role_id,
        cfg.tickets_category_id,
    )

    ping_text = ""
    if db.get_report_pings_enabled():
        ping_ids = _get_ping_ids_for_report(cfg, "vod")
        ping_text = build_staff_ping(ping_ids)

    msg = await staff_channel.send(content=ping_text, embed=embed, view=view)
    db.set_staff_message_id(report_id, msg.id)
    return report_id


async def _submit_tv_report(interaction: discord.Interaction, db: ReportDB, cfg, payload: dict) -> int:
    report_id = db.create_report(
        "tv",
        interaction.user.id,
        interaction.guild.id,
        interaction.channel.id,
        payload,
    )

    staff_channel = interaction.guild.get_channel(cfg.staff_channel_id)
    if not isinstance(staff_channel, discord.TextChannel):
        return await interaction.response.send_message("❌ Staff channel not found.", ephemeral=True)

    embed = build_staff_embed(
        report_id,
        "tv",
        interaction.user,
        interaction.channel,
        payload,
        "Open",
    )

    view = ReportActionView(
        db,
        cfg.staff_channel_id,
        cfg.support_channel_id,
        cfg.public_updates,
        cfg.staff_role_id,
        cfg.tickets_category_id,
    )

    ping_text = ""
    if db.get_report_pings_enabled():
        ping_ids = _get_ping_ids_for_report(cfg, "tv")
        ping_text = build_staff_ping(ping_ids)

    msg = await staff_channel.send(content=ping_text, embed=embed, view=view)
    db.set_staff_message_id(report_id, msg.id)
    return report_id


async def submit_tv_report_with_feedback(
    interaction: discord.Interaction,
    db: ReportDB,
    cfg,
    payload: dict,
) -> int:
    report_id = await _submit_tv_report(interaction, db, cfg, payload)
    success_message = (
        f"✅ Submitted IPTV report **#{report_id}** for **{payload['channel_name']}**"
        f" in **{payload['channel_category']}**."
    )

    try:
        await interaction.response.edit_message(content=success_message, view=None)
        return int(report_id)
    except Exception:
        pass

    if interaction.response.is_done():
        await interaction.followup.send(success_message, ephemeral=True)
        return int(report_id)

    await interaction.response.send_message(success_message, ephemeral=True)
    return int(report_id)


def _tv_review_message(payload: dict, *, double_confirm_pending: bool = False) -> str:
    provider = str(payload.get("provider_name") or payload.get("provider_id") or "").strip()
    channel_name = str(payload.get("channel_name") or "Unknown").strip()
    channel_category = str(payload.get("channel_category") or "Unknown").strip()
    issue = str(payload.get("issue") or "—").strip()

    lines = ["Review your IPTV report before submitting."]
    if provider:
        lines.append(f"**Provider:** {provider}")
    lines.extend(
        [
            f"**Channel:** {channel_name or 'Unknown'}",
            f"**Category:** {channel_category or 'Unknown'}",
            f"**Issue:** {issue or '—'}",
            "",
            (
                "Click **Submit** again to send it, or **Edit Report** to make changes."
                if double_confirm_pending
                else "Click **Submit** to send it, or **Edit Report** to make changes."
            ),
            "",
            "**Important:** By clicking **Submit**, you acknowledge that all information provided is accurate. If you notice any errors, click **Edit Report** to fix them before submitting.",
        ]
    )
    if double_confirm_pending:
        lines.extend(
            [
                "",
                "**Last Chance:** Review everything carefully now. If anything is wrong, click **Edit Report** before you press **Confirm and Submit**.",
            ]
        )
    return "\n".join(lines)


class TVReportReviewView(discord.ui.View):
    def __init__(self, db: ReportDB, cfg, requester_id: int, payload: dict, *, double_confirm_pending: bool = False):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.requester_id = int(requester_id)
        self.payload = dict(payload)
        self.double_confirm_enabled = bool(getattr(cfg, "double_confirmation", False))
        self.double_confirm_pending = bool(double_confirm_pending)

        if self.double_confirm_pending:
            self.confirm_submit.label = "Confirm and Submit"
            self.confirm_submit.style = discord.ButtonStyle.danger
        else:
            self.confirm_submit.label = "Submit"
            self.confirm_submit.style = discord.ButtonStyle.success

    def message_content(self) -> str:
        return _tv_review_message(self.payload, double_confirm_pending=self.double_confirm_pending)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True

        await interaction.response.send_message(
            "❌ Only the user who opened this IPTV report can use it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success)
    async def confirm_submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        if self.double_confirm_enabled and not self.double_confirm_pending:
            await interaction.response.edit_message(
                content=TVReportReviewView(
                    self.db,
                    self.cfg,
                    self.requester_id,
                    self.payload,
                    double_confirm_pending=True,
                ).message_content(),
                view=TVReportReviewView(
                    self.db,
                    self.cfg,
                    self.requester_id,
                    self.payload,
                    double_confirm_pending=True,
                ),
            )
            return

        await submit_tv_report_with_feedback(interaction, self.db, self.cfg, self.payload)

    @discord.ui.button(label="Edit Report", style=discord.ButtonStyle.secondary)
    async def edit_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(
            TVReviewEditModal(
                self.db,
                self.cfg,
                self.requester_id,
                self.payload,
                interaction,
            )
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.edit_message(
            content="Cancelled IPTV report. Nothing was submitted.",
            view=None,
        )


class TVReviewEditModal(discord.ui.Modal, title="Edit IPTV Report"):
    def __init__(self, db: ReportDB, cfg, requester_id: int, payload: dict, launcher_interaction: discord.Interaction):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.requester_id = int(requester_id)
        self.payload = dict(payload)
        self.launcher_interaction = launcher_interaction

        self.channel_name = discord.ui.TextInput(
            label="Channel name",
            max_length=100,
            default=(str(self.payload.get("channel_name") or "") or None),
        )
        self.channel_category = discord.ui.TextInput(
            label="Channel category",
            max_length=100,
            default=(str(self.payload.get("channel_category") or "") or None),
        )
        self.issue = discord.ui.TextInput(
            label="What's the issue?",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            default=(str(self.payload.get("issue") or "") or None),
        )

        self.add_item(self.channel_name)
        self.add_item(self.channel_category)
        self.add_item(self.issue)

    async def on_submit(self, interaction: discord.Interaction):
        updated_payload = _with_tv_provider(
            {
                "channel_name": str(self.channel_name).strip(),
                "channel_category": str(self.channel_category).strip(),
                "issue": str(self.issue).strip(),
            },
            str(self.payload.get("provider_id") or "").strip(),
            str(self.payload.get("provider_name") or "").strip(),
        )

        view = TVReportReviewView(self.db, self.cfg, self.requester_id, updated_payload)
        await interaction.response.defer(ephemeral=True)
        await self.launcher_interaction.edit_original_response(
            content=view.message_content(),
            view=view,
        )


async def present_tv_report_confirmation(
    interaction: discord.Interaction,
    db: ReportDB,
    cfg,
    payload: dict,
    *,
    launcher_interaction: discord.Interaction | None = None,
) -> None:
    view = TVReportReviewView(db, cfg, interaction.user.id, payload)

    if launcher_interaction is not None:
        await interaction.response.defer(ephemeral=True)
        await launcher_interaction.edit_original_response(
            content=view.message_content(),
            view=view,
        )
        return

    try:
        await interaction.response.edit_message(content=view.message_content(), view=view)
        return
    except Exception:
        pass

    if interaction.response.is_done():
        await interaction.followup.send(view.message_content(), view=view, ephemeral=True)
        return

    await interaction.response.send_message(view.message_content(), view=view, ephemeral=True)


def _with_tv_provider(payload: dict, provider_id: str | None = None, provider_name: str | None = None) -> dict:
    resolved_id = str(provider_id or "").strip()
    resolved_name = str(provider_name or "").strip()
    if resolved_id:
        payload["provider_id"] = resolved_id
    if resolved_name:
        payload["provider_name"] = resolved_name
    return payload


# ----------------------------
# TV Modal
# ----------------------------

class TVReportModal(discord.ui.Modal, title="Report IPTV Issue"):
    channel_name = discord.ui.TextInput(label="Channel name", max_length=100)
    channel_category = discord.ui.TextInput(label="Channel category", max_length=100)
    issue = discord.ui.TextInput(label="What’s the issue?", style=discord.TextStyle.paragraph)

    def __init__(
        self,
        db: ReportDB,
        cfg,
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
        launcher_interaction: discord.Interaction | None = None,
    ):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.provider_id = str(provider_id or "").strip()
        self.provider_name = str(provider_name or "").strip()
        self.launcher_interaction = launcher_interaction

    async def on_submit(self, interaction: discord.Interaction):
        payload = _with_tv_provider({
            "channel_name": str(self.channel_name),
            "channel_category": str(self.channel_category),
            "issue": str(self.issue),
        }, self.provider_id, self.provider_name)

        await present_tv_report_confirmation(
            interaction,
            self.db,
            self.cfg,
            payload,
            launcher_interaction=self.launcher_interaction,
        )


class TVIssueModal(discord.ui.Modal, title="Report IPTV Issue"):
    issue = discord.ui.TextInput(label="What’s the issue?", style=discord.TextStyle.paragraph)

    def __init__(
        self,
        db: ReportDB,
        cfg,
        *,
        channel_name: str,
        channel_category: str,
        provider_id: str | None = None,
        provider_name: str | None = None,
        launcher_interaction: discord.Interaction | None = None,
    ):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.channel_name = str(channel_name).strip()
        self.channel_category = str(channel_category).strip()
        self.provider_id = str(provider_id or "").strip()
        self.provider_name = str(provider_name or "").strip()
        self.launcher_interaction = launcher_interaction

    async def on_submit(self, interaction: discord.Interaction):
        payload = _with_tv_provider({
            "channel_name": self.channel_name,
            "channel_category": self.channel_category,
            "issue": str(self.issue),
        }, self.provider_id, self.provider_name)

        await present_tv_report_confirmation(
            interaction,
            self.db,
            self.cfg,
            payload,
            launcher_interaction=self.launcher_interaction,
        )


# ----------------------------
# VOD one-question-at-a-time flow
# ----------------------------

def _new_vod_state() -> dict:
    return {
        "requested_via_bot": "",
        "title_query": "",
        "title": "",
        "language": "",
        "device": "",
        "reference_link": "",
        "is_4k": "",
        "is_remux": "",
        "content_type": "",
        "source_db": "",
        "source_id": "",
        "title_year": "",
        "issue": "",
    }


def _vod_title_placeholder() -> str:
    return "Example: 2012 or Family Guy S02E03"


def _vod_result_label(item: dict) -> str:
    kind = "Movie" if str(item.get("content_type") or "") == "movie" else "TV"
    year = str(item.get("year") or "").strip()
    suffix = f" ({year})" if year else ""
    return f"{kind}: {str(item.get('title') or 'Unknown').strip()}{suffix}"


VOD_TITLE_PAGE_SIZE = 25
VOD_TITLE_MAX_RESULTS = 100


async def _search_vod_candidates(cfg, query: str) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []

    tmdb_token = str(getattr(cfg, "tmdb_bearer_token", "") or "").strip()
    tvdb_key = str(getattr(cfg, "tvdb_key", "") or "").strip()

    tasks = []
    if tmdb_token:
        tasks.append(asyncio.to_thread(search_tmdb_movies, tmdb_token, q, 50))
    if tvdb_key:
        tasks.append(asyncio.to_thread(search_tvdb_series, tvdb_key, q, 50))

    if not tasks:
        return []

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[dict] = []
    for result in gathered:
        if isinstance(result, Exception):
            continue
        merged.extend(result or [])

    seen = set()
    out: list[dict] = []
    for item in merged:
        key = (str(item.get("source_db") or ""), str(item.get("id") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= VOD_TITLE_MAX_RESULTS:
            break
    return out


def _vod_title_page_count(candidates: list[dict]) -> int:
    return max(1, (len(candidates) + VOD_TITLE_PAGE_SIZE - 1) // VOD_TITLE_PAGE_SIZE)


def _vod_title_page_slice(candidates: list[dict], page: int) -> tuple[int, list[dict]]:
    total_pages = _vod_title_page_count(candidates)
    p = min(max(0, int(page)), total_pages - 1)
    start = p * VOD_TITLE_PAGE_SIZE
    end = start + VOD_TITLE_PAGE_SIZE
    return p, candidates[start:end]


def _build_vod_payload(state: dict) -> dict:
    return {
        "requested_via_bot": state["requested_via_bot"],
        "title": state["title"],
        "title_query": state["title_query"],
        "title_year": state["title_year"],
        "language": state["language"],
        "device": state["device"],
        "reference_link": state["reference_link"],
        "is_4k": state["is_4k"],
        "is_remux": state["is_remux"],
        "content_type": state["content_type"],
        "source_db": state["source_db"],
        "source_id": state["source_id"],
        "quality": "4K" if state["is_4k"] == "Yes" else "Non-4K",
        "issue": state["issue"],
    }


class _VODStepView(discord.ui.View):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(timeout=180)
        self.db = db
        self.cfg = cfg
        self.requester_id = int(requester_id)
        self.state = dict(state)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True

        await interaction.response.send_message(
            "❌ Only the user who opened this VOD form can use it.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class _VODDetailsModal(discord.ui.Modal, title="VOD Report Details"):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict, launcher_interaction: discord.Interaction):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.requester_id = int(requester_id)
        self.state = dict(state)
        self.launcher_interaction = launcher_interaction

        self.device = discord.ui.TextInput(
            label="What device are you using?",
            placeholder="Example: Firestick 4K, iPhone 15, LG WebOS TV",
            max_length=150,
            default=(self.state.get("device") or None),
        )
        self.issue = discord.ui.TextInput(
            label="What is the issue?",
            placeholder="Please include as much detail as possible.",
            max_length=1000,
            style=discord.TextStyle.paragraph,
            default=(self.state.get("issue") or None),
        )

        self.add_item(self.device)
        self.add_item(self.issue)

    async def on_submit(self, interaction: discord.Interaction):
        self.state["device"] = str(self.device).strip()
        self.state["issue"] = str(self.issue).strip()

        payload = _build_vod_payload(self.state)
        report_id = await _submit_vod_report(interaction, self.db, self.cfg, payload)
        await interaction.response.edit_message(
            content=f"✅ Submitted VOD report **#{report_id}** for **{payload['title']}**.",
            view=None,
        )


class _VODOpenModalButton(discord.ui.Button):
    def __init__(self, *, label: str, custom_id: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, emoji="📝", custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        await self.view.open_modal(interaction)


class _VODTextQuestionView(_VODStepView):
    def __init__(
        self,
        db: ReportDB,
        cfg,
        requester_id: int,
        state: dict,
        *,
        button_label: str,
    ):
        super().__init__(db, cfg, requester_id, state)
        self.button_label = button_label
        self.modal_open = False
        self.add_item(_VODOpenModalButton(label=button_label, custom_id="vodstep:textquestions"))

    async def open_modal(self, interaction: discord.Interaction):
        if self.modal_open:
            return await interaction.response.send_message(
                "❌ This report form is already in progress. Finish the open modal.",
                ephemeral=True,
            )

        self.modal_open = True
        try:
            await interaction.response.send_modal(
                _VODDetailsModal(
                    self.db,
                    self.cfg,
                    self.requester_id,
                    self.state,
                    interaction,
                )
            )
        except Exception:
            self.modal_open = False
            raise


class _VODTitleQuestionView(_VODTextQuestionView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(
            db,
            cfg,
            requester_id,
            state,
            button_label="Continue",
        )


class _VODReviewTextQuestionsView(_VODTextQuestionView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(
            db,
            cfg,
            requester_id,
            state,
            button_label="Continue",
        )


class _VODSelect(discord.ui.Select):
    def __init__(self, *, placeholder: str, options: list[discord.SelectOption], custom_id: str):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_selection(interaction, self.values[0])


class _VODRequestedQuestionView(_VODStepView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(db, cfg, requester_id, state)
        self.add_item(
            _VODSelect(
                placeholder="Was this title requested through the Requests Bot?",
                options=[
                    discord.SelectOption(label="Yes", value="Yes"),
                    discord.SelectOption(label="No", value="No"),
                ],
                custom_id="vodstep:requested",
            )
        )

    async def handle_selection(self, interaction: discord.Interaction, value: str):
        self.state["requested_via_bot"] = value
        await interaction.response.edit_message(
            content="English or Foreign?",
            view=_VODLanguageQuestionView(self.db, self.cfg, self.requester_id, self.state),
        )


class _VODLanguageQuestionView(_VODStepView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(db, cfg, requester_id, state)
        self.add_item(
            _VODSelect(
                placeholder="English or Foreign?",
                options=[
                    discord.SelectOption(label="English", value="English"),
                    discord.SelectOption(label="Foreign", value="Foreign"),
                ],
                custom_id="vodstep:language",
            )
        )

    async def handle_selection(self, interaction: discord.Interaction, value: str):
        self.state["language"] = _normalize_vod_language(value)
        await interaction.response.edit_message(
            content="Is this a 4K title?",
            view=_VOD4KQuestionView(self.db, self.cfg, self.requester_id, self.state),
        )


class _VOD4KQuestionView(_VODStepView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(db, cfg, requester_id, state)
        self.add_item(
            _VODSelect(
                placeholder="Is this a 4K title?",
                options=[
                    discord.SelectOption(label="Yes", value="Yes"),
                    discord.SelectOption(label="No", value="No"),
                ],
                custom_id="vodstep:4k",
            )
        )

    async def handle_selection(self, interaction: discord.Interaction, value: str):
        self.state["is_4k"] = _normalize_vod_4k(value)
        if getattr(self.cfg, "remux", False):
            return await interaction.response.edit_message(
                content="Is this title a remux?",
                view=_VODRemuxQuestionView(self.db, self.cfg, self.requester_id, self.state),
            )

        await interaction.response.send_modal(
            _VODDetailsModal(
                self.db,
                self.cfg,
                self.requester_id,
                self.state,
                interaction,
            )
        )


class _VODRemuxButton(discord.ui.Button):
    def __init__(self, *, label: str, value: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style)
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_selection(interaction, self.value)


class _VODRemuxQuestionView(_VODStepView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(db, cfg, requester_id, state)
        self.add_item(_VODRemuxButton(label="Yes", value="Yes", style=discord.ButtonStyle.success))
        self.add_item(_VODRemuxButton(label="No", value="No", style=discord.ButtonStyle.secondary))

    async def handle_selection(self, interaction: discord.Interaction, value: str):
        self.state["is_remux"] = value
        await interaction.response.send_modal(
            _VODDetailsModal(
                self.db,
                self.cfg,
                self.requester_id,
                self.state,
                interaction,
            )
        )


class _VODTitleSearchModal(discord.ui.Modal, title="Find Title"):
    search = discord.ui.TextInput(
        label="Movie or TV show name",
        placeholder=_vod_title_placeholder(),
        max_length=150,
    )

    def __init__(
        self,
        db: ReportDB,
        cfg,
        requester_id: int,
        state: dict,
        launcher_interaction: discord.Interaction | None,
    ):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.requester_id = int(requester_id)
        self.state = dict(state)
        self.launcher_interaction = launcher_interaction

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.search).strip()
        self.state["title_query"] = query

        if not query:
            return await interaction.response.send_message(
                "❌ Enter a title to continue.",
                view=_VODTitleRetryView(self.db, self.cfg, self.requester_id, self.state),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        candidates = await _search_vod_candidates(self.cfg, query)
        if not candidates:
            retry_view = _VODTitleRetryView(self.db, self.cfg, self.requester_id, self.state)
            if self.launcher_interaction is not None:
                try:
                    await self.launcher_interaction.edit_original_response(
                        content=(
                            "❌ No matching titles were found in TMDB/TVDB.\n\n"
                            "Try another search term."
                        ),
                        view=retry_view,
                    )
                    return
                except Exception:
                    pass

            return await interaction.followup.send(
                content=(
                    "❌ No matching titles were found in TMDB/TVDB.\n\n"
                    "Try another search term."
                ),
                view=retry_view,
                ephemeral=True,
            )

        view = _VODTitleResultsView(self.db, self.cfg, self.requester_id, self.state, candidates)
        if self.launcher_interaction is not None:
            try:
                await self.launcher_interaction.edit_original_response(
                    content=view._content_text(),
                    view=view,
                )
                return
            except Exception:
                pass

        await interaction.followup.send(
            content=view._content_text(),
            view=view,
            ephemeral=True,
        )


async def start_vod_title_flow(interaction: discord.Interaction, db: ReportDB, cfg) -> None:
    await interaction.response.send_modal(
        _VODTitleSearchModal(
            db,
            cfg,
            interaction.user.id,
            _new_vod_state(),
            None,
        )
    )


class _VODOpenTitleModalButton(discord.ui.Button):
    def __init__(self, *, label: str, custom_id: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, emoji="🔎", custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        await self.view.open_modal(interaction)


class _VODTitleRetryView(_VODStepView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(db, cfg, requester_id, state)
        self.modal_open = False
        self.add_item(_VODOpenTitleModalButton(label="Search Title", custom_id="vodstep:title_retry"))

    async def open_modal(self, interaction: discord.Interaction):
        if self.modal_open:
            return await interaction.response.send_message(
                "❌ This report form is already in progress. Finish the open modal.",
                ephemeral=True,
            )

        self.modal_open = True
        try:
            await interaction.response.send_modal(
                _VODTitleSearchModal(
                    self.db,
                    self.cfg,
                    self.requester_id,
                    self.state,
                    interaction,
                )
            )
        except Exception:
            self.modal_open = False
            raise


class _VODTitleResultSelect(discord.ui.Select):
    def __init__(self, candidates: list[dict], *, page: int):
        options = []
        page_idx, page_items = _vod_title_page_slice(candidates, page)
        start_index = page_idx * VOD_TITLE_PAGE_SIZE

        for idx, item in enumerate(page_items):
            title = str(item.get("title") or "Unknown").strip()
            year = str(item.get("year") or "").strip()
            source = "TMDB" if str(item.get("source_db") or "") == "tmdb" else "TVDB"
            kind = "Movie" if str(item.get("content_type") or "") == "movie" else "TV Show"

            label = title[:100]
            desc_parts = [kind, source]
            if year:
                desc_parts.insert(1, year)
            description = " • ".join(desc_parts)[:100]
            options.append(discord.SelectOption(label=label, value=str(start_index + idx), description=description))

        super().__init__(
            placeholder="Select the matching title",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="vodstep:title_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_selection(interaction, self.values[0])


class _VODTitleResultsView(_VODStepView):
    def __init__(
        self,
        db: ReportDB,
        cfg,
        requester_id: int,
        state: dict,
        candidates: list[dict],
        *,
        page: int = 0,
    ):
        super().__init__(db, cfg, requester_id, state)
        self.candidates = list(candidates[:VOD_TITLE_MAX_RESULTS])
        self.page = min(max(0, int(page)), _vod_title_page_count(self.candidates) - 1)

        self.add_item(_VODTitleResultSelect(self.candidates, page=self.page))
        self.add_item(_VODOpenTitleModalButton(label="Search Again", custom_id="vodstep:title_again"))

        total_pages = _vod_title_page_count(self.candidates)
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= (total_pages - 1)

    def _content_text(self) -> str:
        total = len(self.candidates)
        total_pages = _vod_title_page_count(self.candidates)
        return (
            "Select the correct title:\n"
            f"Page **{self.page + 1}** of **{total_pages}** • **{total}** results\n"
            "Tip: the dropdown supports type-ahead search on the visible page."
        )

    async def open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _VODTitleSearchModal(
                self.db,
                self.cfg,
                self.requester_id,
                self.state,
                interaction,
            )
        )

    @discord.ui.button(label="Previous Page", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        new_page = max(0, self.page - 1)
        view = _VODTitleResultsView(
            self.db,
            self.cfg,
            self.requester_id,
            self.state,
            self.candidates,
            page=new_page,
        )
        await interaction.response.edit_message(content=view._content_text(), view=view)

    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        max_page = _vod_title_page_count(self.candidates) - 1
        new_page = min(max_page, self.page + 1)
        view = _VODTitleResultsView(
            self.db,
            self.cfg,
            self.requester_id,
            self.state,
            self.candidates,
            page=new_page,
        )
        await interaction.response.edit_message(content=view._content_text(), view=view)

    async def handle_selection(self, interaction: discord.Interaction, value: str):
        try:
            idx = int(value)
            item = self.candidates[idx]
        except Exception:
            return await interaction.response.send_message("❌ Invalid selection.", ephemeral=True)

        title = str(item.get("title") or "").strip()
        if not title:
            return await interaction.response.send_message("❌ Invalid title selection.", ephemeral=True)

        self.state["title"] = title
        self.state["title_year"] = str(item.get("year") or "").strip()
        self.state["content_type"] = _normalize_vod_content_type(str(item.get("content_type") or ""))
        self.state["source_db"] = str(item.get("source_db") or "").strip()
        self.state["source_id"] = str(item.get("id") or "").strip()
        self.state["reference_link"] = str(item.get("reference_link") or "").strip()

        label = _vod_result_label(item)
        source = "TMDB" if self.state["source_db"] == "tmdb" else "TVDB"
        await interaction.response.edit_message(
            content=(
                f"Selected: **{label}**\n"
                f"Source: **{source}**\n"
                "Was this title requested through the Requests Bot?"
            ),
            view=_VODRequestedQuestionView(self.db, self.cfg, self.requester_id, self.state),
        )


class VODQuestionnaireView(_VODTitleRetryView):
    def __init__(self, db: ReportDB, cfg, requester_id: int):
        super().__init__(db, cfg, requester_id, _new_vod_state())


# ----------------------------
# Resolve modal
# ----------------------------

class ResolveReportModal(discord.ui.Modal):
    details = discord.ui.TextInput(
        label="Resolution details (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder="Anything you want the reporter to know (optional)",
    )

    def __init__(
        self,
        db: ReportDB,
        staff_channel_id: int,
        support_channel_id: int,
        public_updates: bool,
        staff_role_id: int,
        tickets_category_id: int,
        report_id: int,
        *,
        delete_current_channel: bool = False,
        close_ticket_channel: bool = False,
    ):
        super().__init__(title=f"Resolve Report #{int(report_id)}")
        self.db = db
        self.staff_channel_id = int(staff_channel_id or 0)
        self.support_channel_id = int(support_channel_id or 0)
        self.public_updates = bool(public_updates)
        self.staff_role_id = int(staff_role_id or 0)
        self.tickets_category_id = int(tickets_category_id or 0)
        self.report_id = int(report_id)
        self.delete_current_channel = bool(delete_current_channel)
        self.close_ticket_channel = bool(close_ticket_channel)

    async def _close_ticket_channel_if_any(self, interaction: discord.Interaction, reporter: discord.abc.User | None):
        ticket_id = None
        try:
            ticket_id = self.db.get_ticket_channel_id(self.report_id)
        except Exception:
            ticket_id = None

        if not ticket_id or not interaction.guild:
            return

        ch = interaction.guild.get_channel(int(ticket_id))
        if isinstance(ch, discord.TextChannel):
            # transcript first
            await _try_send_transcript(interaction, reporter, self.report_id, "Resolved", ch)

            try:
                await ch.delete(reason=f"Report #{self.report_id} resolved")
            except discord.Forbidden:
                try:
                    await ch.edit(name=f"closed-report-{self.report_id}")
                except Exception:
                    pass
            except Exception:
                pass

        try:
            self.db.set_ticket_channel_id(self.report_id, None)
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

        report = self.db.get_report_by_id(self.report_id)
        if not report or int(report.get("guild_id", 0)) != interaction.guild.id:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        resolver_id = int(interaction.user.id)
        note = str(self.details).strip()

        # Pre-fetch reporter for transcripts + DMs
        reporter_u: discord.abc.User | None = None
        try:
            reporter_u = await interaction.client.fetch_user(int(report["reporter_id"]))
        except Exception:
            reporter_u = None

        if self.close_ticket_channel:
            await self._close_ticket_channel_if_any(interaction, reporter_u)

        if hasattr(self.db, "mark_resolved"):
            try:
                self.db.mark_resolved(self.report_id, resolver_id)  # type: ignore[attr-defined]
            except Exception:
                self.db.update_status(self.report_id, "Resolved")
        else:
            self.db.update_status(self.report_id, "Resolved")

        report = self.db.get_report_by_id(self.report_id) or report

        if self.staff_channel_id and report.get("staff_message_id"):
            try:
                staff_channel = interaction.guild.get_channel(self.staff_channel_id)
                if isinstance(staff_channel, discord.TextChannel):
                    staff_msg = await staff_channel.fetch_message(int(report["staff_message_id"]))

                    source = interaction.guild.get_channel(int(report["source_channel_id"])) or staff_channel
                    claimed_by = report.get("claimed_by_user_id")
                    claimed_at = report.get("claimed_at")

                    embed = build_staff_embed(
                        self.report_id,
                        report["report_type"],
                        reporter_u or interaction.user,
                        source,
                        report["payload"],
                        "Resolved",
                        ticket_channel_id=None,
                        claimed_by_user_id=claimed_by,
                        claimed_at=claimed_at,
                        resolved_by_id=resolver_id,
                        resolved_note=note or None,
                    )

                    view = ReportActionView(
                        db=self.db,
                        staff_channel_id=self.staff_channel_id,
                        support_channel_id=self.support_channel_id,
                        public_updates=self.public_updates,
                        staff_role_id=self.staff_role_id,
                        tickets_category_id=self.tickets_category_id,
                    )
                    view.disable_all()

                    await staff_msg.edit(embed=embed, view=view)
            except Exception:
                pass

        reporter = reporter_u
        msg = None
        try:
            if reporter:
                subj = report_subject(report["report_type"], report["payload"])
                msg = f"✅ Update on your report #{self.report_id} ({subj}): **Resolved**."
                if note:
                    msg += f"\n\nDetails: {note}"
                await try_dm(reporter, msg)
        except Exception:
            pass

        if self.public_updates and reporter and msg:
            responses_cid = _get_responses_channel_id_from_bot(interaction)
            await _try_public_update(interaction, responses_cid, reporter, msg)

        try:
            self.db.set_ticket_channel_id(self.report_id, None)
        except Exception:
            pass

        await interaction.response.send_message("✅ Resolved.", ephemeral=True)

        # If this modal is being used inside the ticket channel, transcript + delete it
        if self.delete_current_channel and interaction.channel and isinstance(interaction.channel, discord.TextChannel):
            # transcript first
            await _try_send_transcript(interaction, reporter, self.report_id, "Resolved", interaction.channel)

            try:
                await interaction.channel.delete(reason=f"Resolved ticket for report #{self.report_id}")
            except discord.Forbidden:
                try:
                    await interaction.channel.edit(name=f"closed-report-{self.report_id}")
                except Exception:
                    pass
            except Exception:
                pass


# ----------------------------
# Not Resolved modal
# ----------------------------

class NotResolvedReportModal(discord.ui.Modal):
    details = discord.ui.TextInput(
        label="Why isn’t this resolved?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
        placeholder="Example: couldn’t replicate the issue, no errors found, needs more info (required)",
    )

    def __init__(
        self,
        db: ReportDB,
        staff_channel_id: int,
        support_channel_id: int,
        public_updates: bool,
        staff_role_id: int,
        tickets_category_id: int,
        report_id: int,
        *,
        delete_current_channel: bool = False,
        close_ticket_channel: bool = False,
    ):
        super().__init__(title=f"Not Resolved #{int(report_id)}")
        self.db = db
        self.staff_channel_id = int(staff_channel_id or 0)
        self.support_channel_id = int(support_channel_id or 0)
        self.public_updates = bool(public_updates)
        self.staff_role_id = int(staff_role_id or 0)
        self.tickets_category_id = int(tickets_category_id or 0)
        self.report_id = int(report_id)
        self.delete_current_channel = bool(delete_current_channel)
        self.close_ticket_channel = bool(close_ticket_channel)

    async def _close_ticket_channel_if_any(self, interaction: discord.Interaction, reporter: discord.abc.User | None):
        ticket_id = None
        try:
            ticket_id = self.db.get_ticket_channel_id(self.report_id)
        except Exception:
            ticket_id = None

        if not ticket_id or not interaction.guild:
            return

        ch = interaction.guild.get_channel(int(ticket_id))
        if isinstance(ch, discord.TextChannel):
            # transcript first
            await _try_send_transcript(interaction, reporter, self.report_id, "Not Resolved", ch)

            try:
                await ch.delete(reason=f"Report #{self.report_id} closed as not resolved")
            except discord.Forbidden:
                try:
                    await ch.edit(name=f"closed-report-{self.report_id}")
                except Exception:
                    pass
            except Exception:
                pass

        try:
            self.db.set_ticket_channel_id(self.report_id, None)
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

        report = self.db.get_report_by_id(self.report_id)
        if not report or int(report.get("guild_id", 0)) != interaction.guild.id:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        resolver_id = int(interaction.user.id)
        note = str(self.details).strip()
        if not note:
            return await interaction.response.send_message("❌ Details are required.", ephemeral=True)

        # Pre-fetch reporter for transcripts + DMs
        reporter_u: discord.abc.User | None = None
        try:
            reporter_u = await interaction.client.fetch_user(int(report["reporter_id"]))
        except Exception:
            reporter_u = None

        if self.close_ticket_channel:
            await self._close_ticket_channel_if_any(interaction, reporter_u)

        self.db.update_status(self.report_id, "Not Resolved")
        report = self.db.get_report_by_id(self.report_id) or report

        if self.staff_channel_id and report.get("staff_message_id"):
            try:
                staff_channel = interaction.guild.get_channel(self.staff_channel_id)
                if isinstance(staff_channel, discord.TextChannel):
                    staff_msg = await staff_channel.fetch_message(int(report["staff_message_id"]))

                    source = interaction.guild.get_channel(int(report["source_channel_id"])) or staff_channel
                    claimed_by = report.get("claimed_by_user_id")
                    claimed_at = report.get("claimed_at")

                    embed = build_staff_embed(
                        self.report_id,
                        report["report_type"],
                        reporter_u or interaction.user,
                        source,
                        report["payload"],
                        "Not Resolved",
                        ticket_channel_id=None,
                        claimed_by_user_id=claimed_by,
                        claimed_at=claimed_at,
                        resolved_by_id=resolver_id,
                        resolved_note=note,
                    )

                    view = ReportActionView(
                        db=self.db,
                        staff_channel_id=self.staff_channel_id,
                        support_channel_id=self.support_channel_id,
                        public_updates=self.public_updates,
                        staff_role_id=self.staff_role_id,
                        tickets_category_id=self.tickets_category_id,
                    )
                    view.disable_all()

                    await staff_msg.edit(embed=embed, view=view)
            except Exception:
                pass

        reporter = reporter_u
        msg = None
        try:
            if reporter:
                subj = report_subject(report["report_type"], report["payload"])
                msg = f"⚠️ Update on your report #{self.report_id} ({subj}): **Not resolved**.\n\nDetails: {note}"
                await try_dm(reporter, msg)
        except Exception:
            pass

        if self.public_updates and reporter and msg:
            responses_cid = _get_responses_channel_id_from_bot(interaction)
            await _try_public_update(interaction, responses_cid, reporter, msg)

        try:
            self.db.set_ticket_channel_id(self.report_id, None)
        except Exception:
            pass

        await interaction.response.send_message("✅ Closed as not resolved.", ephemeral=True)

        if self.delete_current_channel and interaction.channel and isinstance(interaction.channel, discord.TextChannel):
            # transcript first
            await _try_send_transcript(interaction, reporter, self.report_id, "Not Resolved", interaction.channel)

            try:
                await interaction.channel.delete(reason=f"Closed (not resolved) ticket for report #{self.report_id}")
            except discord.Forbidden:
                try:
                    await interaction.channel.edit(name=f"closed-report-{self.report_id}")
                except Exception:
                    pass
            except Exception:
                pass
