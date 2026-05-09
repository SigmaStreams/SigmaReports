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
from bot.providers import default_provider, enabled_providers, get_provider


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


def _tv_selector_providers() -> list[dict]:
    configured = enabled_providers()
    if configured:
        return [
            provider
            for provider in configured
            if selector_dataset_available(provider_id=str(provider.get("id") or ""))
        ]

    provider = default_provider()
    if provider and selector_dataset_available(provider_id=str(provider.get("id") or "")):
        return [provider]

    return []


def _tv_selector_enabled(*, provider_id: str | None = None) -> bool:
    if provider_id:
        return selector_dataset_available(provider_id=provider_id)
    return bool(_tv_selector_providers())


def _provider_context(provider_id: str | None = None, provider_name: str | None = None) -> tuple[str, str]:
    resolved_id = str(provider_id or "").strip()
    resolved_name = str(provider_name or "").strip()

    provider = get_provider(resolved_id) if resolved_id else None
    if provider:
        resolved_id = str(provider.get("id") or resolved_id).strip()
        resolved_name = str(provider.get("name") or resolved_name).strip()

    return resolved_id, resolved_name


def _visible_provider_name(provider_id: str | None = None, provider_name: str | None = None) -> str:
    _, resolved_name = _provider_context(provider_id, provider_name)
    if not resolved_name:
        return ""
    if len(_tv_selector_providers()) > 1:
        return resolved_name
    return ""


def _provider_line(provider_id: str | None = None, provider_name: str | None = None) -> str:
    visible_name = _visible_provider_name(provider_id, provider_name)
    if not visible_name:
        return ""
    return f"Provider: **{visible_name}**\n"


def _with_provider(payload: dict, provider_id: str | None = None, provider_name: str | None = None) -> dict:
    resolved_id, resolved_name = _provider_context(provider_id, provider_name)
    if resolved_id:
        payload["provider_id"] = resolved_id
    if resolved_name:
        payload["provider_name"] = resolved_name
    return payload


def _tv_selector_entry_message(*, provider_id: str | None = None, provider_name: str | None = None) -> str:
    prefix = _provider_line(provider_id, provider_name)
    if _tv_selector_enabled(provider_id=provider_id):
        return (
            f"{prefix}"
            "**Recommended:** Start with **Search Channel** to find the channel fastest.\n"
            "If you are not sure of the channel name, use **Browse by Category**."
        )
    return (
        f"{prefix}"
        "IPTV channel lists are not configured on this deployment. Use manual entry to submit an IPTV report."
    )


async def _edit_launcher_or_respond(
    interaction: discord.Interaction,
    *,
    launcher_interaction: discord.Interaction | None,
    content: str,
    view: discord.ui.View,
) -> None:
    if launcher_interaction is not None:
        await interaction.response.defer(ephemeral=True)
        await launcher_interaction.edit_original_response(content=content, view=view)
        return

    await interaction.response.send_message(content, view=view, ephemeral=True)


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
    def __init__(self, db, cfg, *, provider_id: str | None = None, provider_name: str | None = None):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        if not _tv_selector_enabled(provider_id=self.provider_id):
            self.remove_item(self.search_channel)
            self.remove_item(self.browse_category)

    @discord.ui.button(label="Search Channel (Recommended)", style=discord.ButtonStyle.primary)
    async def search_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        if not _tv_selector_enabled(provider_id=self.provider_id):
            from bot.modals import TVReportModal

            return await interaction.response.send_modal(
                TVReportModal(
                    self.db,
                    self.cfg,
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                    launcher_interaction=interaction,
                )
            )
        await interaction.response.send_modal(
            _TVGlobalChannelSearchModal(
                self.db,
                self.cfg,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
            )
        )

    @discord.ui.button(label="Browse by Category", style=discord.ButtonStyle.secondary)
    async def browse_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        if not _tv_selector_enabled(provider_id=self.provider_id):
            return await interaction.response.edit_message(
                content=_tv_selector_entry_message(provider_id=self.provider_id, provider_name=self.provider_name),
                view=_TVSelectorEntryView(self.db, self.cfg, provider_id=self.provider_id, provider_name=self.provider_name),
            )

        categories = selector_categories(provider_id=self.provider_id)
        if not categories:
            return await interaction.response.edit_message(
                content=_tv_selector_entry_message(provider_id=self.provider_id, provider_name=self.provider_name),
                view=_TVSelectorEntryView(self.db, self.cfg, provider_id=self.provider_id, provider_name=self.provider_name),
            )

        await interaction.response.edit_message(
            content=_TVCategoryResultsView(
                self.db,
                self.cfg,
                categories,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
            )._message_content(),
            view=_TVCategoryResultsView(
                self.db,
                self.cfg,
                categories,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
            ),
        )


class _TVProviderSelect(discord.ui.Select):
    def __init__(self, providers: list[dict]):
        options = [
            discord.SelectOption(
                label=str(provider.get("name") or provider.get("id") or "Provider")[:100],
                value=str(provider.get("id") or ""),
            )
            for provider in providers[:25]
        ]
        super().__init__(
            placeholder="Choose your provider",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_provider_selection(interaction, self.values[0])


class _TVProviderChoiceView(discord.ui.View):
    def __init__(self, db, cfg, providers: list[dict]):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.providers = list(providers)
        self.add_item(_TVProviderSelect(self.providers))

    async def handle_provider_selection(self, interaction: discord.Interaction, provider_id: str):
        provider = get_provider(provider_id)
        if not provider or not _tv_selector_enabled(provider_id=provider_id):
            return await interaction.response.edit_message(
                content="That provider is not available right now. Try again or use manual entry.",
                view=self,
            )

        await interaction.response.edit_message(
            content=_tv_selector_entry_message(
                provider_id=str(provider.get("id") or ""),
                provider_name=str(provider.get("name") or ""),
            ),
            view=_TVSelectorEntryView(
                self.db,
                self.cfg,
                provider_id=str(provider.get("id") or ""),
                provider_name=str(provider.get("name") or ""),
            ),
        )

class _TVCategorySearchModal(discord.ui.Modal, title="Find TV Category"):
    search = discord.ui.TextInput(
        label="Category search",
        required=False,
        max_length=100,
        placeholder="Leave blank to browse the first 25 categories",
    )

    def __init__(
        self,
        db,
        cfg,
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
        launcher_interaction: discord.Interaction | None = None,
    ):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.launcher_interaction = launcher_interaction

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.search).strip()
        matches = search_selector_categories(query, limit=500, provider_id=self.provider_id)
        if not matches:
            return await _edit_launcher_or_respond(
                interaction,
                launcher_interaction=self.launcher_interaction,
                content="No IPTV categories matched that search. Try a broader term.",
                view=_TVSelectorEntryView(self.db, self.cfg, provider_id=self.provider_id, provider_name=self.provider_name),
            )

        view = _TVCategoryResultsView(
            self.db,
            self.cfg,
            matches,
            query=query,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )

        await _edit_launcher_or_respond(
            interaction,
            launcher_interaction=self.launcher_interaction,
            content=view._message_content(),
            view=view,
        )


class _TVGlobalChannelSearchModal(discord.ui.Modal, title="Find TV Channel"):
    search = discord.ui.TextInput(
        label="Channel search",
        required=False,
        max_length=100,
        placeholder="Enter part of the channel name",
    )

    def __init__(
        self,
        db,
        cfg,
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
        launcher_interaction: discord.Interaction | None = None,
    ):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.launcher_interaction = launcher_interaction

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.search).strip()
        matches = search_all_selector_channels(query, limit=2000, provider_id=self.provider_id)
        if not matches:
            return await _edit_launcher_or_respond(
                interaction,
                launcher_interaction=self.launcher_interaction,
                content="No channels matched that search. Try a broader term, or browse by category.",
                view=_TVSelectorEntryView(self.db, self.cfg, provider_id=self.provider_id, provider_name=self.provider_name),
            )

        view = _TVGlobalChannelResultsView(
            self.db,
            self.cfg,
            matches,
            query=query,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )

        await _edit_launcher_or_respond(
            interaction,
            launcher_interaction=self.launcher_interaction,
            content=view._message_content(),
            view=view,
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
    def __init__(
        self,
        db,
        cfg,
        matches: list[dict],
        *,
        page: int = 0,
        query: str = "",
        provider_id: str | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.matches = list(matches)
        self.page = max(0, min(page, _page_count(self.matches) - 1))
        self.query = str(query).strip()
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.add_item(_TVCategorySelect(self.matches, page=self.page))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= (_page_count(self.matches) - 1)

    def _message_content(self) -> str:
        header = "Choose the IPTV category."
        if self.query:
            header = f"Choose the IPTV category for `{self.query}`."
        return (
            f"{_provider_line(self.provider_id, self.provider_name)}"
            f"{header}\n"
            "**Recommended:** Use **Search Categories (Recommended)** if you know part of the name.\n"
            f"**Showing up to {PAGE_SIZE} categories per page.** {_page_indicator(self.matches, self.page)}."
        )

    async def handle_category_selection(self, interaction: discord.Interaction, category_name: str):
        category = find_selector_category(category_name, provider_id=self.provider_id)
        channels = [channel for channel in (category or {}).get("channels", []) if isinstance(channel, dict)]
        if not channels:
            return await interaction.response.edit_message(
                content=f"No channels are available in **{category_name}** right now. Try a different category.",
                view=_TVSelectorEntryView(self.db, self.cfg, provider_id=self.provider_id, provider_name=self.provider_name),
            )

        view = _TVChannelResultsView(
            self.db,
            self.cfg,
            category_name,
            channels,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )

        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Previous Page", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVCategoryResultsView(
            self.db,
            self.cfg,
            self.matches,
            page=self.page - 1,
            query=self.query,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVCategoryResultsView(
            self.db,
            self.cfg,
            self.matches,
            page=self.page + 1,
            query=self.query,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Search Categories (Recommended)", style=discord.ButtonStyle.primary, row=1)
    async def search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(
            _TVCategorySearchModal(
                self.db,
                self.cfg,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                launcher_interaction=interaction,
            )
        )


class _TVChannelSearchModal(discord.ui.Modal, title="Find TV Channel"):
    search = discord.ui.TextInput(
        label="Channel search",
        required=False,
        max_length=100,
        placeholder="Leave blank to browse the first 25 channels in the category",
    )

    def __init__(
        self,
        db,
        cfg,
        *,
        category_name: str,
        provider_id: str | None = None,
        provider_name: str | None = None,
        launcher_interaction: discord.Interaction | None = None,
    ):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.category_name = str(category_name).strip()
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.launcher_interaction = launcher_interaction

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.search).strip()
        matches = search_selector_channels(self.category_name, query, limit=2000, provider_id=self.provider_id)
        if not matches:
            return await _edit_launcher_or_respond(
                interaction,
                launcher_interaction=self.launcher_interaction,
                content=f"No channels matched that search in **{self.category_name}**. Try a broader term or change category.",
                view=_TVChannelResultsView(
                    self.db,
                    self.cfg,
                    self.category_name,
                    _all_channels_for_category(self.category_name, provider_id=self.provider_id),
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                ),
            )

        view = _TVChannelResultsView(
            self.db,
            self.cfg,
            self.category_name,
            matches,
            query=query,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )

        await _edit_launcher_or_respond(
            interaction,
            launcher_interaction=self.launcher_interaction,
            content=view._message_content(),
            view=view,
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
    def __init__(
        self,
        db,
        cfg,
        *,
        channel_name: str,
        channel_category: str,
        provider_id: str | None = None,
        provider_name: str | None = None,
        launcher_interaction: discord.Interaction | None = None,
    ):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.channel_name = str(channel_name).strip()
        self.channel_category = str(channel_category).strip()
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.launcher_interaction = launcher_interaction
        self.add_item(_TVIssueOptionSelect(COMMON_TV_ISSUES, placeholder="Choose the issue"))

    async def handle_issue_selection(self, interaction: discord.Interaction, issue_value: str):
        from bot.modals import TVIssueModal, present_tv_report_confirmation

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
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                    launcher_interaction=self.launcher_interaction or interaction,
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
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                    launcher_interaction=self.launcher_interaction or interaction,
                )
            )
            return

        payload = _with_provider({
            "channel_name": self.channel_name,
            "channel_category": self.channel_category,
            "issue": issue_value,
        }, self.provider_id, self.provider_name)
        await present_tv_report_confirmation(
            interaction,
            self.db,
            self.cfg,
            payload,
            launcher_interaction=self.launcher_interaction or interaction,
        )


class _TVIssueFollowupView(discord.ui.View):
    def __init__(
        self,
        db,
        cfg,
        *,
        channel_name: str,
        channel_category: str,
        parent_issue: str,
        provider_id: str | None = None,
        provider_name: str | None = None,
        launcher_interaction: discord.Interaction | None = None,
    ):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.channel_name = str(channel_name).strip()
        self.channel_category = str(channel_category).strip()
        self.parent_issue = str(parent_issue).strip()
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.launcher_interaction = launcher_interaction
        follow_up = FOLLOW_UP_TV_ISSUES[self.parent_issue]
        self.add_item(_TVIssueOptionSelect(follow_up["options"], placeholder=follow_up["title"]))

    async def handle_issue_selection(self, interaction: discord.Interaction, issue_value: str):
        from bot.modals import TVIssueModal, present_tv_report_confirmation

        if issue_value == "__other__":
            await interaction.response.send_modal(
                TVIssueModal(
                    self.db,
                    self.cfg,
                    channel_name=self.channel_name,
                    channel_category=self.channel_category,
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                    launcher_interaction=self.launcher_interaction or interaction,
                )
            )
            return

        payload = _with_provider({
            "channel_name": self.channel_name,
            "channel_category": self.channel_category,
            "issue": issue_value,
        }, self.provider_id, self.provider_name)
        await present_tv_report_confirmation(
            interaction,
            self.db,
            self.cfg,
            payload,
            launcher_interaction=self.launcher_interaction or interaction,
        )

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
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                launcher_interaction=self.launcher_interaction,
            ),
        )


def _all_channels_for_category(category_name: str, *, provider_id: str | None = None) -> list[dict]:
    category = find_selector_category(category_name, provider_id=provider_id)
    return [channel for channel in (category or {}).get("channels", []) if isinstance(channel, dict)]


class _TVChannelResultsView(discord.ui.View):
    def __init__(
        self,
        db,
        cfg,
        category_name: str,
        matches: list[dict],
        *,
        page: int = 0,
        query: str = "",
        provider_id: str | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.category_name = str(category_name).strip()
        self.matches = list(matches)
        self.page = max(0, min(page, _page_count(self.matches) - 1))
        self.query = str(query).strip()
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.add_item(_TVChannelSelect(self.matches, page=self.page))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= (_page_count(self.matches) - 1)

    def _message_content(self) -> str:
        header = f"Choose the affected channel in **{self.category_name}**."
        if self.query:
            header = f"Choose the affected channel in **{self.category_name}** for `{self.query}`."
        return (
            f"{_provider_line(self.provider_id, self.provider_name)}"
            f"{header}\n"
            "**Recommended:** Use **Search Channels (Recommended)** if you know part of the name.\n"
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
            provider_id=self.provider_id,
            provider_name=self.provider_name,
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
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Search Channels (Recommended)", style=discord.ButtonStyle.primary, row=1)
    async def search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(
            _TVChannelSearchModal(
                self.db,
                self.cfg,
                category_name=self.category_name,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                launcher_interaction=interaction,
            )
        )

    @discord.ui.button(label="Change Category", style=discord.ButtonStyle.secondary, row=1)
    async def change_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(
            _TVCategorySearchModal(
                self.db,
                self.cfg,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                launcher_interaction=interaction,
            )
        )

    async def handle_channel_selection(self, interaction: discord.Interaction, selector_key: str):
        selected = find_selector_channel(selector_key, category_name=self.category_name, provider_id=self.provider_id)
        if not selected:
            return await interaction.response.edit_message(
                content="❌ That channel could not be resolved. Please search again.",
                view=_TVChannelResultsView(
                    self.db,
                    self.cfg,
                    self.category_name,
                    _all_channels_for_category(self.category_name, provider_id=self.provider_id),
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                ),
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
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                launcher_interaction=interaction,
            ),
        )


class _TVGlobalChannelResultsView(discord.ui.View):
    def __init__(
        self,
        db,
        cfg,
        matches: list[dict],
        *,
        page: int = 0,
        query: str = "",
        provider_id: str | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(timeout=300)
        self.db = db
        self.cfg = cfg
        self.matches = list(matches)
        self.page = max(0, min(page, _page_count(self.matches) - 1))
        self.query = str(query).strip()
        self.provider_id, self.provider_name = _provider_context(provider_id, provider_name)
        self.add_item(_TVChannelSelect(self.matches, page=self.page, show_category=True))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= (_page_count(self.matches) - 1)

    def _message_content(self) -> str:
        header = "Choose the affected channel."
        if self.query:
            header = f"Choose the affected channel for `{self.query}`."
        return (
            f"{_provider_line(self.provider_id, self.provider_name)}"
            f"{header}\n"
            "**Recommended:** Use **Search Channels (Recommended)** to find the channel fastest.\n"
            f"**Showing up to {PAGE_SIZE} channels per page.** {_page_indicator(self.matches, self.page)}."
        )

    async def handle_channel_selection(self, interaction: discord.Interaction, selector_key: str):
        selected = find_selector_channel(selector_key, provider_id=self.provider_id)
        if not selected:
            return await interaction.response.edit_message(
                content="❌ That channel could not be resolved. Please search again.",
                view=_TVGlobalChannelResultsView(
                    self.db,
                    self.cfg,
                    self.matches,
                    page=self.page,
                    query=self.query,
                    provider_id=self.provider_id,
                    provider_name=self.provider_name,
                ),
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
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                launcher_interaction=interaction,
            ),
        )

    @discord.ui.button(label="Previous Page", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVGlobalChannelResultsView(
            self.db,
            self.cfg,
            self.matches,
            page=self.page - 1,
            query=self.query,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        view = _TVGlobalChannelResultsView(
            self.db,
            self.cfg,
            self.matches,
            page=self.page + 1,
            query=self.query,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
        )
        await interaction.response.edit_message(
            content=view._message_content(),
            view=view,
        )

    @discord.ui.button(label="Search Channels (Recommended)", style=discord.ButtonStyle.primary, row=1)
    async def search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(
            _TVGlobalChannelSearchModal(
                self.db,
                self.cfg,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                launcher_interaction=interaction,
            )
        )

    @discord.ui.button(label="Browse by Category", style=discord.ButtonStyle.secondary, row=1)
    async def browse_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        categories = selector_categories(provider_id=self.provider_id)
        if not categories:
            return await interaction.response.edit_message(
                content=_tv_selector_entry_message(provider_id=self.provider_id, provider_name=self.provider_name),
                view=_TVSelectorEntryView(self.db, self.cfg, provider_id=self.provider_id, provider_name=self.provider_name),
            )

        await interaction.response.edit_message(
            content=_TVCategoryResultsView(
                self.db,
                self.cfg,
                categories,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
            )._message_content(),
            view=_TVCategoryResultsView(
                self.db,
                self.cfg,
                categories,
                provider_id=self.provider_id,
                provider_name=self.provider_name,
            ),
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
        label="Report IPTV",
        style=discord.ButtonStyle.primary,
        emoji="📺",
        custom_id="panel:report_tv",
    )
    async def report_tv_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._block_gate(interaction):
            return

        providers = _tv_selector_providers()

        if not providers:
            from bot.modals import TVReportModal

            return await interaction.response.send_modal(TVReportModal(self.db, self.cfg))

        if len(providers) == 1:
            provider = providers[0]
            return await interaction.response.send_message(
                _tv_selector_entry_message(
                    provider_id=str(provider.get("id") or ""),
                    provider_name=str(provider.get("name") or ""),
                ),
                view=_TVSelectorEntryView(
                    self.db,
                    self.cfg,
                    provider_id=str(provider.get("id") or ""),
                    provider_name=str(provider.get("name") or ""),
                ),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose the provider for this IPTV report.",
            view=_TVProviderChoiceView(self.db, self.cfg, providers),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Report VOD (Plex/Emby/Jellyfin)",
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
            title="Submit a report",
            description=(
                "Choose a report type below.\n\n"
                "📺 **IPTV** — buffering, offline channels, wrong content\n"
                "🎬 **VOD (Plex/Emby/Jellyfin)** — playback issues, missing episodes, quality problems"
            ),
        )
        embed.set_footer(text="Include as much detail as you can.")

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
