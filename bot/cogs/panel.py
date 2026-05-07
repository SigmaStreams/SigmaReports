import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from bot.iptv import (
    find_selector_category,
    find_selector_channel,
    search_all_selector_channels,
    search_selector_categories,
    search_selector_channels,
    selector_dataset_available,
    selector_categories,
)


PAGE_SIZE = 25
COMMON_TV_ISSUES = [
    ("Channel offline", "__offline__"),
    ("Buffering or freezing", "__buffering__"),
    ("Wrong content", "Wrong channel / wrong content"),
    ("No audio", "No audio"),
    ("Black screen / no video", "No video / black screen"),
    ("Audio out of sync", "Audio / video de-sync"),
    ("Guide / EPG issue", "EPG / guide issue"),
    ("Something else", "__other__"),
]
FOLLOW_UP_TV_ISSUES = {
    "__offline__": {
        "title": "What best describes the offline issue?",
        "options": [
            ("Won’t open", "Channel offline / not loading"),
            ("Shows an error immediately", "Channel offline / playback error on start"),
            ("Stuck on loading", "Channel offline / stuck loading"),
        ],
    },
    "__buffering__": {
        "title": "What best describes the buffering issue?",
        "options": [
            ("Constant buffering", "Buffering / freezing constantly"),
            ("Starts then freezes", "Starts playing then freezes"),
            ("Poor quality or unstable", "Buffering / unstable stream quality"),
            ("Only during live events", "Buffering during live events"),
        ],
    },
}


def _tv_selector_enabled() -> bool:
    return selector_dataset_available()


def _tv_selector_entry_message() -> str:
    if _tv_selector_enabled():
        return (
            "**Recommended:** start with **Search Channel** to find the channel fastest.\n"
            "If you are not sure of the channel name, use **Browse by Category**."
        )
    return "IPTV channel lists are not configured on this deployment. Use manual entry to submit a Live TV report."


def _iso_to_discord_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except Exception:
        return iso


def _page_slice(items: list[dict], page: int) -> list[dict]:
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    return items[start:end]


def _page_count(items: list[dict]) -> int:
    return max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)


def _page_indicator(items: list[dict], page: int) -> str:
    total_pages = _page_count(items)
    return f"Page **{page + 1} of {total_pages}**"


class _TVSelectorEntryView(discord.ui.View):
    def __init__(self, db, cfg):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        if not _tv_selector_enabled():
            self.remove_item(self.search_channel)
            self.remove_item(self.browse_category)

    @discord.ui.button(label="Search Channel (Recommended)", style=discord.ButtonStyle.primary)
    async def search_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        if not _tv_selector_enabled():
            from bot.modals import TVReportModal

            return await interaction.response.send_modal(TVReportModal(self.db, self.cfg))
        await interaction.response.send_modal(_TVGlobalChannelSearchModal(self.db, self.cfg))

    @discord.ui.button(label="Browse by Category", style=discord.ButtonStyle.secondary)
    async def browse_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        if not _tv_selector_enabled():
            return await interaction.response.edit_message(
                content=_tv_selector_entry_message(),
                view=_TVSelectorEntryView(self.db, self.cfg),
            )

        categories = selector_categories()
        if not categories:
            return await interaction.response.edit_message(
                content=_tv_selector_entry_message(),
                view=_TVSelectorEntryView(self.db, self.cfg),
            )

        await interaction.response.edit_message(
            content=_TVCategoryResultsView(self.db, self.cfg, categories)._message_content(),
            view=_TVCategoryResultsView(self.db, self.cfg, categories),
        )

class _TVCategorySearchModal(discord.ui.Modal, title="Find TV Category"):
    search = discord.ui.TextInput(
        label="Category search",
        required=False,
        max_length=100,
        placeholder="Leave blank to browse the first 25 categories",
    )

    def __init__(self, db, cfg):
        super().__init__()
        self.db = db
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.search).strip()
        matches = search_selector_categories(query, limit=500)
        if not matches:
            return await interaction.response.send_message(
                "No IPTV categories matched that search. Try a broader term.",
                view=_TVSelectorEntryView(self.db, self.cfg),
                ephemeral=True,
            )

        view = _TVCategoryResultsView(self.db, self.cfg, matches, query=query)

        await interaction.response.send_message(
            view._message_content(),
            view=view,
            ephemeral=True,
        )


class _TVGlobalChannelSearchModal(discord.ui.Modal, title="Find TV Channel"):
    search = discord.ui.TextInput(
        label="Channel search",
        required=False,
        max_length=100,
        placeholder="Enter part of the channel name",
    )

    def __init__(self, db, cfg):
        super().__init__()
        self.db = db
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.search).strip()
        matches = search_all_selector_channels(query, limit=2000)
        if not matches:
            return await interaction.response.send_message(
                "No channels matched that search. Try a broader term, or browse by category.",
                view=_TVSelectorEntryView(self.db, self.cfg),
                ephemeral=True,
            )

        view = _TVGlobalChannelResultsView(self.db, self.cfg, matches, query=query)

        await interaction.response.send_message(
            view._message_content(),
            view=view,
            ephemeral=True,
        )


class _TVCategorySelect(discord.ui.Select):
    def __init__(self, matches: list[dict], *, page: int):
        options = [
            discord.SelectOption(
                label=str(item.get("name") or "Unknown")[:100],
                value=str(item.get("name") or ""),
                description=f"{int(item.get('channel_count') or 0)} channels"[:100],
            )
            for item in _page_slice(matches, page)
        ]
        super().__init__(
            placeholder=f"Choose a TV category - Page {page + 1}/{_page_count(matches)}",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_category_selection(interaction, self.values[0])


class _TVCategoryResultsView(discord.ui.View):
    def __init__(self, db, cfg, matches: list[dict], *, page: int = 0, query: str = ""):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.matches = list(matches)
        self.page = max(0, min(page, _page_count(self.matches) - 1))
        self.query = str(query).strip()
        self.add_item(_TVCategorySelect(self.matches, page=self.page))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= (_page_count(self.matches) - 1)

    def _message_content(self) -> str:
        header = "Choose the IPTV category."
        if self.query:
            header = f"Choose the IPTV category for `{self.query}`."
        return (
            f"{header}\n"
            "**Recommended:** use **Search Categories (Recommended)** if you know part of the name.\n"
            f"**Showing up to {PAGE_SIZE} categories per page.** {_page_indicator(self.matches, self.page)}."
        )

    async def handle_category_selection(self, interaction: discord.Interaction, category_name: str):
        category = find_selector_category(category_name)
        channels = [channel for channel in (category or {}).get("channels", []) if isinstance(channel, dict)]
        if not channels:
            return await interaction.response.send_message(
                f"No channels are available in **{category_name}** right now. Try a different category.",
                view=_TVSelectorEntryView(self.db, self.cfg),
                ephemeral=True,
            )

        await interaction.response.send_message(
            _TVChannelResultsView(self.db, self.cfg, category_name, channels)._message_content(),
            view=_TVChannelResultsView(self.db, self.cfg, category_name, channels),
            ephemeral=True,
        )

    @discord.ui.button(label="Previous Page", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVCategoryResultsView(self.db, self.cfg, self.matches, page=self.page - 1, query=self.query)
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVCategoryResultsView(self.db, self.cfg, self.matches, page=self.page + 1, query=self.query)
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Search Categories (Recommended)", style=discord.ButtonStyle.primary, row=1)
    async def search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(_TVCategorySearchModal(self.db, self.cfg))


class _TVChannelSearchModal(discord.ui.Modal, title="Find TV Channel"):
    search = discord.ui.TextInput(
        label="Channel search",
        required=False,
        max_length=100,
        placeholder="Leave blank to browse the first 25 channels in the category",
    )

    def __init__(self, db, cfg, *, category_name: str):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.category_name = str(category_name).strip()

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.search).strip()
        matches = search_selector_channels(self.category_name, query, limit=2000)
        if not matches:
            return await interaction.response.send_message(
                f"No channels matched that search in **{self.category_name}**. Try a broader term or change category.",
                view=_TVChannelResultsView(self.db, self.cfg, self.category_name, _all_channels_for_category(self.category_name)),
                ephemeral=True,
            )

        prompt = f"Choose the affected channel in **{self.category_name}**."
        if query:
            prompt = f"Choose the affected channel in **{self.category_name}** for `{query}`."

        await interaction.response.send_message(
            _TVChannelResultsView(self.db, self.cfg, self.category_name, matches, query=query)._message_content(),
            view=_TVChannelResultsView(self.db, self.cfg, self.category_name, matches, query=query),
            ephemeral=True,
        )


class _TVChannelSelect(discord.ui.Select):
    def __init__(self, matches: list[dict], *, page: int, show_category: bool = False):
        options = [
            discord.SelectOption(
                label=str(item.get("display_name") or item.get("name") or "Unknown")[:100],
                value=str(item.get("selector_key") or ""),
                description=(str(item.get("category") or "Unknown")[:100] if show_category else None),
            )
            for item in _page_slice(matches, page)
        ]
        super().__init__(
            placeholder=f"Choose a TV channel - Page {page + 1}/{_page_count(matches)}",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_channel_selection(interaction, self.values[0])


class _TVIssueOptionSelect(discord.ui.Select):
    def __init__(self, options_source: list[tuple[str, str]], *, placeholder: str):
        options = [
            discord.SelectOption(label=label, value=value)
            for label, value in options_source
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_issue_selection(interaction, self.values[0])


class _TVIssueChoiceView(discord.ui.View):
    def __init__(self, db, cfg, *, channel_name: str, channel_category: str):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.channel_name = str(channel_name).strip()
        self.channel_category = str(channel_category).strip()
        self.add_item(_TVIssueOptionSelect(COMMON_TV_ISSUES, placeholder="Choose the issue"))

    async def handle_issue_selection(self, interaction: discord.Interaction, issue_value: str):
        from bot.modals import TVIssueModal, submit_tv_report_with_feedback

        follow_up = FOLLOW_UP_TV_ISSUES.get(issue_value)
        if follow_up is not None:
            await interaction.response.edit_message(
                content=(
                    f"{follow_up['title']} for **{self.channel_name}**"
                    f" in **{self.channel_category}**."
                ),
                view=_TVIssueFollowupView(
                    self.db,
                    self.cfg,
                    channel_name=self.channel_name,
                    channel_category=self.channel_category,
                    parent_issue=issue_value,
                ),
            )
            return

        if issue_value == "__other__":
            await interaction.response.send_modal(
                TVIssueModal(
                    self.db,
                    self.cfg,
                    channel_name=self.channel_name,
                    channel_category=self.channel_category,
                )
            )
            return

        payload = {
            "channel_name": self.channel_name,
            "channel_category": self.channel_category,
            "issue": issue_value,
        }
        await submit_tv_report_with_feedback(interaction, self.db, self.cfg, payload)


class _TVIssueFollowupView(discord.ui.View):
    def __init__(self, db, cfg, *, channel_name: str, channel_category: str, parent_issue: str):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.channel_name = str(channel_name).strip()
        self.channel_category = str(channel_category).strip()
        self.parent_issue = str(parent_issue).strip()
        follow_up = FOLLOW_UP_TV_ISSUES[self.parent_issue]
        self.add_item(_TVIssueOptionSelect(follow_up["options"], placeholder=follow_up["title"]))

    async def handle_issue_selection(self, interaction: discord.Interaction, issue_value: str):
        from bot.modals import TVIssueModal, submit_tv_report_with_feedback

        if issue_value == "__other__":
            await interaction.response.send_modal(
                TVIssueModal(
                    self.db,
                    self.cfg,
                    channel_name=self.channel_name,
                    channel_category=self.channel_category,
                )
            )
            return

        payload = {
            "channel_name": self.channel_name,
            "channel_category": self.channel_category,
            "issue": issue_value,
        }
        await submit_tv_report_with_feedback(interaction, self.db, self.cfg, payload)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.edit_message(
            content=(
                f"Choose the issue for **{self.channel_name}**"
                f" in **{self.channel_category}**."
            ),
            view=_TVIssueChoiceView(
                self.db,
                self.cfg,
                channel_name=self.channel_name,
                channel_category=self.channel_category,
            ),
        )


def _all_channels_for_category(category_name: str) -> list[dict]:
    category = find_selector_category(category_name)
    return [channel for channel in (category or {}).get("channels", []) if isinstance(channel, dict)]


class _TVChannelResultsView(discord.ui.View):
    def __init__(self, db, cfg, category_name: str, matches: list[dict], *, page: int = 0, query: str = ""):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.category_name = str(category_name).strip()
        self.matches = list(matches)
        self.page = max(0, min(page, _page_count(self.matches) - 1))
        self.query = str(query).strip()
        self.add_item(_TVChannelSelect(self.matches, page=self.page))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= (_page_count(self.matches) - 1)

    def _message_content(self) -> str:
        header = f"Choose the affected channel in **{self.category_name}**."
        if self.query:
            header = f"Choose the affected channel in **{self.category_name}** for `{self.query}`."
        return (
            f"{header}\n"
            "**Recommended:** use **Search Channels (Recommended)** if you know part of the name.\n"
            f"**Showing up to {PAGE_SIZE} channels per page.** {_page_indicator(self.matches, self.page)}."
        )

    @discord.ui.button(label="Previous Page", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVChannelResultsView(
            self.db,
            self.cfg,
            self.category_name,
            self.matches,
            page=self.page - 1,
            query=self.query,
        )
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVChannelResultsView(
            self.db,
            self.cfg,
            self.category_name,
            self.matches,
            page=self.page + 1,
            query=self.query,
        )
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Search Channels (Recommended)", style=discord.ButtonStyle.primary, row=1)
    async def search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(_TVChannelSearchModal(self.db, self.cfg, category_name=self.category_name))

    @discord.ui.button(label="Change Category", style=discord.ButtonStyle.secondary, row=1)
    async def change_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(_TVCategorySearchModal(self.db, self.cfg))

    async def handle_channel_selection(self, interaction: discord.Interaction, selector_key: str):
        selected = find_selector_channel(selector_key, category_name=self.category_name)
        if not selected:
            return await interaction.response.send_message(
                "❌ That channel could not be resolved. Please search again.",
                view=_TVChannelResultsView(self.db, self.cfg, self.category_name, _all_channels_for_category(self.category_name)),
                ephemeral=True,
            )

        await interaction.response.edit_message(
            content=(
                f"Choose the issue for **{str(selected.get('name') or 'Unknown')}**"
                f" in **{str(selected.get('category') or self.category_name)}**."
            ),
            view=_TVIssueChoiceView(
                self.db,
                self.cfg,
                channel_name=str(selected.get("name") or "Unknown"),
                channel_category=str(selected.get("category") or self.category_name),
            ),
        )


class _TVGlobalChannelResultsView(discord.ui.View):
    def __init__(self, db, cfg, matches: list[dict], *, page: int = 0, query: str = ""):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.matches = list(matches)
        self.page = max(0, min(page, _page_count(self.matches) - 1))
        self.query = str(query).strip()
        self.add_item(_TVChannelSelect(self.matches, page=self.page, show_category=True))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= (_page_count(self.matches) - 1)

    def _message_content(self) -> str:
        header = "Choose the affected channel."
        if self.query:
            header = f"Choose the affected channel for `{self.query}`."
        return (
            f"{header}\n"
            "**Recommended:** use **Search Channels (Recommended)** to find the channel fastest.\n"
            f"**Showing up to {PAGE_SIZE} channels per page.** {_page_indicator(self.matches, self.page)}."
        )

    async def handle_channel_selection(self, interaction: discord.Interaction, selector_key: str):
        selected = find_selector_channel(selector_key)
        if not selected:
            return await interaction.response.send_message(
                "❌ That channel could not be resolved. Please search again.",
                view=_TVGlobalChannelResultsView(self.db, self.cfg, self.matches, page=self.page, query=self.query),
                ephemeral=True,
            )

        await interaction.response.edit_message(
            content=(
                f"Choose the issue for **{str(selected.get('name') or 'Unknown')}**"
                f" in **{str(selected.get('category') or 'Unknown')}**."
            ),
            view=_TVIssueChoiceView(
                self.db,
                self.cfg,
                channel_name=str(selected.get("name") or "Unknown"),
                channel_category=str(selected.get("category") or "Unknown"),
            ),
        )

    @discord.ui.button(label="Previous Page", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVGlobalChannelResultsView(self.db, self.cfg, self.matches, page=self.page - 1, query=self.query)
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVGlobalChannelResultsView(self.db, self.cfg, self.matches, page=self.page + 1, query=self.query)
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Search Channels (Recommended)", style=discord.ButtonStyle.primary, row=1)
    async def search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(_TVGlobalChannelSearchModal(self.db, self.cfg))

    @discord.ui.button(label="Browse by Category", style=discord.ButtonStyle.secondary, row=1)
    async def browse_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        categories = selector_categories()
        if not categories:
            return await interaction.response.edit_message(
                content=_tv_selector_entry_message(),
                view=_TVSelectorEntryView(self.db, self.cfg),
            )

        await interaction.response.edit_message(
            content=_TVCategoryResultsView(self.db, self.cfg, categories)._message_content(),
            view=_TVCategoryResultsView(self.db, self.cfg, categories),
        )


class ReportPanelView(discord.ui.View):
    """
    Persistent view for the report panel.
    Uses lazy imports inside button callbacks to avoid circular imports.
    """

    def __init__(self, db, cfg):
        super().__init__(timeout=None)
        self.db = db
        self.cfg = cfg

    def _support_channel_mention(self, interaction: discord.Interaction) -> str:
        if not interaction.guild or not self.cfg.support_channel_id:
            return "the support channel"
        ch = interaction.guild.get_channel(self.cfg.support_channel_id)
        return ch.mention if ch else "the support channel"

    async def _block_gate(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True

        blocked, is_perm, expires_at, reason = self.db.is_user_blocked(
            interaction.guild.id, interaction.user.id
        )
        if not blocked:
            return True

        support = self._support_channel_mention(interaction)
        reason_txt = f"\nReason: {reason}" if reason else ""

        if is_perm:
            msg = (
                f"🚫 {interaction.user.mention} you are blocked from using the report system.\n"
                f"To appeal, please open a ticket in {support}.{reason_txt}"
            )
        else:
            exp = f"\nBlock expires: {_iso_to_discord_ts(expires_at)}" if expires_at else ""
            msg = (
                f"🚫 {interaction.user.mention} you are temporarily blocked from using the report system."
                f"{exp}\nTo appeal, please open a ticket in {support}.{reason_txt}"
            )

        await interaction.response.send_message(msg, ephemeral=True)
        return False

    @discord.ui.button(
        label="Report Live TV",
        style=discord.ButtonStyle.primary,
        emoji="📺",
        custom_id="panel:report_tv",
    )
    async def report_tv_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._block_gate(interaction):
            return

        if not _tv_selector_enabled():
            from bot.modals import TVReportModal

            return await interaction.response.send_modal(TVReportModal(self.db, self.cfg))

        await interaction.response.send_message(
            _tv_selector_entry_message(),
            view=_TVSelectorEntryView(self.db, self.cfg),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Report Movie / TV Show",
        style=discord.ButtonStyle.secondary,
        emoji="🎬",
        custom_id="panel:report_vod",
    )
    async def report_vod_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._block_gate(interaction):
            return

        # Lazy import avoids circular imports
        from bot.modals import VODQuestionnaireView

        await interaction.response.send_message(
            "Was this title requested through the Requests Bot?",
            view=VODQuestionnaireView(self.db, self.cfg, interaction.user.id),
            ephemeral=True,
        )


class ReportPanelCog(commands.Cog):
    def __init__(self, bot, db, cfg):
        self.bot = bot
        self.db = db
        self.cfg = cfg

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return False
        return any(r.id == self.cfg.staff_role_id for r in member.roles)

    @app_commands.command(
        name="reportpanel",
        description="Post a report panel embed with buttons (staff only).",
    )
    @app_commands.describe(channel="Channel to post the report panel in")
    async def reportpanel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)
        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        embed = discord.Embed(
            title="Report an issue",
            description=(
                "Use the buttons below to submit a report.\n\n"
                "📺 **Live TV** — buffering, offline channels, wrong content\n"
                "🎬 **Movies / TV Shows** — playback issues, missing episodes, quality problems\n\n"
                "**What happens next?**\n"
                "Staff will review your report. If we need more details, we may open a **private ticket channel** with you "
                "so we can troubleshoot properly.\n\n"
                "**Tips (the more detail, the faster we can fix it):**\n"
                "• what you expected vs what happened\n"
                "• when it happened\n"
                "• device/app used\n"
                "• any errors/screenshots (if applicable)"
            ),
        )
        embed.set_footer(text="You’ll receive updates via DM and/or in a ticket channel if one is opened.")

        view = ReportPanelView(self.db, self.cfg)

        try:
            await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don’t have permission to post in that channel.",
                ephemeral=True,
            )

        await interaction.response.send_message(f"✅ Posted a report panel in {channel.mention}.", ephemeral=True)


async def setup(bot):
    # Register the persistent view here so buttons keep working after restarts
    bot.add_view(ReportPanelView(bot.db, bot.cfg))
    await bot.add_cog(ReportPanelCog(bot, bot.db, bot.cfg))
