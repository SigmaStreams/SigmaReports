import io
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord

from bot.db import ReportDB
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
        f"✅ Submitted TV report **#{report_id}** for **{payload['channel_name']}**"
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

class TVReportModal(discord.ui.Modal, title="Report TV Issue"):
    channel_name = discord.ui.TextInput(label="Channel name", max_length=100)
    channel_category = discord.ui.TextInput(label="Channel category", max_length=100)
    issue = discord.ui.TextInput(label="What’s the issue?", style=discord.TextStyle.paragraph)

    def __init__(self, db: ReportDB, cfg, *, provider_id: str | None = None, provider_name: str | None = None):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.provider_id = str(provider_id or "").strip()
        self.provider_name = str(provider_name or "").strip()

    async def on_submit(self, interaction: discord.Interaction):
        payload = _with_tv_provider({
            "channel_name": str(self.channel_name),
            "channel_category": str(self.channel_category),
            "issue": str(self.issue),
        }, self.provider_id, self.provider_name)

        await submit_tv_report_with_feedback(interaction, self.db, self.cfg, payload)


class TVIssueModal(discord.ui.Modal, title="Report TV Issue"):
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
    ):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.channel_name = str(channel_name).strip()
        self.channel_category = str(channel_category).strip()
        self.provider_id = str(provider_id or "").strip()
        self.provider_name = str(provider_name or "").strip()

    async def on_submit(self, interaction: discord.Interaction):
        payload = _with_tv_provider({
            "channel_name": self.channel_name,
            "channel_category": self.channel_category,
            "issue": str(self.issue),
        }, self.provider_id, self.provider_name)

        await submit_tv_report_with_feedback(interaction, self.db, self.cfg, payload)


# ----------------------------
# VOD one-question-at-a-time flow
# ----------------------------

def _new_vod_state() -> dict:
    return {
        "requested_via_bot": "",
        "title": "",
        "language": "",
        "reference_link": "",
        "is_4k": "",
        "content_type": "",
        "issue": "",
    }


def _vod_title_placeholder() -> str:
    return "Example: 2012 or Family Guy S02E03"


def _vod_reference_placeholder(content_type: str) -> str:
    normalized_type = _normalize_vod_content_type(content_type)
    if normalized_type == "tv":
        return "Example: https://www.thetvdb.com/series/family-guy"
    if normalized_type == "movie":
        return "Example: https://www.themoviedb.org/movie/14161-2012"
    return "Example: TMDB movie or TVDB series link"


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


class _VODCombinedTextModal(discord.ui.Modal, title="VOD Text Questions"):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict, launcher_interaction: discord.Interaction):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.requester_id = int(requester_id)
        self.state = dict(state)
        self.launcher_interaction = launcher_interaction

        self.title_name = discord.ui.TextInput(
            label="Movie or TV show name",
            placeholder=_vod_title_placeholder(),
            max_length=150,
            default=(self.state.get("title") or None),
        )
        self.reference_link = discord.ui.TextInput(
            label="TMDB or TVDB link",
            placeholder=_vod_reference_placeholder(self.state.get("content_type") or ""),
            max_length=300,
            default=(self.state.get("reference_link") or None),
        )
        self.issue = discord.ui.TextInput(
            label="What is the issue?",
            placeholder="Please include as much detail as possible.",
            max_length=1000,
            style=discord.TextStyle.paragraph,
            default=(self.state.get("issue") or None),
        )

        self.add_item(self.title_name)
        self.add_item(self.reference_link)
        self.add_item(self.issue)

    async def on_submit(self, interaction: discord.Interaction):
        self.state["title"] = str(self.title_name).strip()
        self.state["reference_link"] = str(self.reference_link).strip()
        self.state["issue"] = str(self.issue).strip()

        link_error = _validate_vod_reference_link(self.state["content_type"], self.state["reference_link"])
        if link_error:
            await interaction.response.defer(ephemeral=True)
            await self.launcher_interaction.edit_original_response(
                content=(
                    f"{link_error}\n\n"
                    "Click Continue to review and finish your report."
                ),
                view=_VODReviewTextQuestionsView(self.db, self.cfg, self.requester_id, self.state),
            )
            return

        payload = {
            "requested_via_bot": self.state["requested_via_bot"],
            "title": self.state["title"],
            "language": self.state["language"],
            "reference_link": self.state["reference_link"],
            "is_4k": self.state["is_4k"],
            "content_type": self.state["content_type"],
            "quality": "4K" if self.state["is_4k"] == "Yes" else "Non-4K",
            "issue": self.state["issue"],
        }
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
                _VODCombinedTextModal(
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
        await interaction.response.edit_message(
            content="Is this a movie, or TV show?",
            view=_VODContentTypeQuestionView(self.db, self.cfg, self.requester_id, self.state),
        )


class _VODContentTypeQuestionView(_VODStepView):
    def __init__(self, db: ReportDB, cfg, requester_id: int, state: dict):
        super().__init__(db, cfg, requester_id, state)
        self.add_item(
            _VODSelect(
                placeholder="Is this a movie, or TV show?",
                options=[
                    discord.SelectOption(label="Movie", value="movie"),
                    discord.SelectOption(label="TV Show", value="tv"),
                ],
                custom_id="vodstep:type",
            )
        )

    async def handle_selection(self, interaction: discord.Interaction, value: str):
        self.state["content_type"] = _normalize_vod_content_type(value)
        await interaction.response.send_modal(
            _VODCombinedTextModal(
                self.db,
                self.cfg,
                self.requester_id,
                self.state,
                interaction,
            )
        )


class VODQuestionnaireView(_VODRequestedQuestionView):
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
