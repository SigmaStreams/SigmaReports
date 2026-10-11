"""Reviewed browser actions reuse the bot's staff callbacks and ticket lifecycle."""
import asyncio
from functools import partial
import hashlib
import json
from types import SimpleNamespace

import discord
from aiohttp import web

from .cogs.reports import OWNER_ID
from .modals import ResolveReportModal, NotResolvedReportModal
from .utils import report_subject, build_staff_embed
from .views import ReportActionView

CLOSED = {"Resolved", "Not Resolved"}
REPORT_ACTIONS = {"claim", "ticket", "resolve", "not_resolved", "reopen", "resend", "reassign"}
ADMIN_ACTIONS = {"block", "unblock", "panel", "liveboard_start", "liveboard_refresh", "liveboard_stop", "bulk_close", "pings", "sync"}
LABELS = {"claim": "Claim report", "ticket": "Open ticket", "resolve": "Resolve report", "not_resolved": "Close as not resolved", "reopen": "Reopen report", "resend": "Send missing staff report", "reassign": "Change reporter", "block": "Block reporting", "unblock": "Unblock reporting", "panel": "Post report panel", "liveboard_start": "Start liveboard", "liveboard_refresh": "Refresh liveboard", "liveboard_stop": "Stop liveboard", "bulk_close": "Resolve all active reports", "pings": "Toggle staff pings", "sync": "Sync slash commands"}


def snowflake(value, label):
    text = str(value or "")
    if not text.isascii() or not text.isdigit() or not 0 < int(text) < 2**63:
        raise ValueError(f"Choose a valid {label} ID.")
    return int(text)


def report_revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class WebReportContext:
    """Transport for private callback feedback; all public sends stay in callbacks."""
    def __init__(self, bot, guild, actor, channel, message=None):
        self.client, self.guild, self.user = bot, guild, actor
        self.guild_id, self.channel_id = guild.id, channel.id
        self.channel, self.message = channel, message
        self.response = self.followup = self
        self.messages = []
        self.done = False

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True

    async def send(self, content=None, **kwargs):
        self.done = True
        if content:
            self.messages.append(str(content))
        return self

    send_message = send
    edit_original_response = send

    async def edit_message(self, **kwargs):
        self.done = True
        if self.message:
            await self.message.edit(**kwargs)


class ReportActions:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.locks = {}

    def get_report(self, guild, rid):
        report = self.db.get_report_by_id(snowflake(rid, "report"))
        if not report or report["guild_id"] != guild.id:
            raise web.HTTPNotFound(reason="Report not found in this server.")
        return report

    def pending(self, guild, rid):
        return (guild.id, rid) in self.bot.ticket_closures.pending

    def revision(self, guild, data):
        action = data["action"]
        if action in REPORT_ACTIONS:
            report = self.get_report(guild, data["report_id"])
            state = {"report": report, "closing": self.pending(guild, report["id"])}
            if action == "resend":
                from .modals import _get_ping_ids_for_report
                state["pings"] = self.db.get_report_pings_enabled()
                state["ping_ids"] = _get_ping_ids_for_report(self.bot.cfg, report["report_type"].lower())
        elif action == "bulk_close":
            state = self.db.list_reports_by_statuses(guild.id, ("Open", "Claimed", "Ticket Open"))
            state = [{"report": r, "closing": self.pending(guild, r["id"])} for r in state]
        elif action in {"block", "unblock"}:
            state = [b for b in self.db.list_blocked_users(guild.id) if b["user_id"] == int(data["user_id"])]
        elif action == "pings":
            state = self.db.get_report_pings_enabled()
        elif action.startswith("liveboard"):
            state = {"board": self.db.get_liveboard(guild.id), "reports": self.db.list_reports_by_statuses(guild.id, ("Open", "Claimed", "Ticket Open"))}
        else:
            state = None
        return report_revision(state)

    def staff_channel(self, guild):
        channel = guild.get_channel(self.bot.cfg.staff_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("The configured staff reports channel is unavailable.")
        return channel

    def destination(self, guild, actor, cid):
        channel = guild.get_channel(snowflake(cid, "channel"))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Choose a text channel in this server.")
        for member in (actor, guild.me):
            if member is None:
                raise ValueError("The bot is reconnecting. Try again later.")
            perms = channel.permissions_for(member)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                raise ValueError("Both you and the bot need View Channel, Send Messages, and Embed Links in the destination.")
        return channel

    async def prepare(self, guild, actor, data):
        action = data.get("action")
        if action not in REPORT_ACTIONS | ADMIN_ACTIONS:
            raise ValueError("Choose a supported action.")
        if action in {"pings", "sync"} and actor.id != OWNER_ID:
            raise web.HTTPForbidden(reason="This control is restricted to the configured owner.")
        clean = {"action": action, "guild_id": str(guild.id)}
        preview = {"title": LABELS[action], "server": guild.name, "details": []}
        note = data.get("note", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("Details must be text of up to 1,000 characters.")
        clean["note"] = note.strip()
        if action in {"block", "unblock", "reassign"}:
            uid = snowflake(data.get("user_id"), "user")
            try:
                target = await guild.fetch_member(uid)
            except discord.NotFound:
                if action != "unblock":
                    raise ValueError("Choose a current server member.") from None
                target = SimpleNamespace(id=uid, name="Former member")
            clean["user_id"] = str(uid)
            preview["details"].append(f"User: {target.name} ({uid})")
        if action in REPORT_ACTIONS:
            report = self.get_report(guild, data.get("report_id"))
            clean["report_id"] = str(report["id"])
            preview.update(report_id=str(report["id"]), subject=report_subject(report["report_type"], report["payload"]), status=report["status"], reporter_id=str(report["reporter_id"]))
            if self.pending(guild, report["id"]):
                raise ValueError("This report already has a closure in progress. Use Cancel in its ticket to keep it open.")
            if action == "reopen":
                if report["status"] not in CLOSED:
                    raise ValueError("Only closed reports can be reopened.")
            elif action != "reassign" and report["status"] in CLOSED:
                raise ValueError("This report is already closed.")
            if action in {"claim", "ticket", "reopen"} and not report.get("staff_message_id"):
                raise ValueError("Send the missing staff report first.")
            if action == "resend" and report.get("staff_message_id"):
                raise ValueError("This report already has a staff message.")
            if action == "resend":
                from .modals import build_staff_ping, _get_ping_ids_for_report
                ch = self.destination(guild, actor, self.bot.cfg.staff_channel_id)
                reporter = self.bot.get_user(int(report["reporter_id"]))
                if reporter is None:
                    reporter = await self.bot.fetch_user(int(report["reporter_id"]))
                source = guild.get_channel(int(report["source_channel_id"])) or ch
                preview["embed"] = build_staff_embed(report["id"], report["report_type"], reporter, source, report["payload"], report["status"], ticket_channel_id=report.get("ticket_channel_id"), claimed_by_user_id=report.get("claimed_by_user_id"), claimed_at=report.get("claimed_at"), resolved_by_id=report.get("resolved_by"), resolved_note=report.get("resolved_note")).to_dict()
                preview["content"] = build_staff_ping(_get_ping_ids_for_report(self.bot.cfg, report["report_type"].lower())) if self.db.get_report_pings_enabled() else ""
                preview["details"].append(f"Destination: {ch.id} (#{ch.name}); embed. Explicit user notifications: {preview['content'] or 'None'}; no role notifications.")
            if action == "claim":
                if report["status"] != "Open" or report.get("claimed_by_user_id") or report.get("ticket_channel_id"):
                    raise ValueError("Only an unclaimed open report without a ticket can be claimed.")
                responses = self.destination(guild, actor, self.bot.cfg.responses_channel_id)
                preview["details"].append(f"Reporter notification: {responses.id} (#{responses.name}); pings only reporter {report['reporter_id']}.")
                preview["details"].append("You've confirmed the issue and already filed it. The reporter will be notified in the responses channel; the report stays active.")
            if action == "ticket":
                if report.get("ticket_channel_id"):
                    raise ValueError("This report already has a ticket.")
                if not guild.me or not guild.me.guild_permissions.manage_channels:
                    raise ValueError("The bot needs Manage Channels to open tickets.")
                preview["details"].append("Create a private ticket for the reporter and staff, and mark this report Ticket Open.")
            if action == "reassign" and report.get("ticket_channel_id"):
                raise ValueError("Close the existing ticket before changing its reporter, so access permissions stay consistent.")
            if action in {"resolve", "not_resolved"}:
                if action == "not_resolved" and not clean["note"]:
                    raise ValueError("Explain why the report is not resolved.")
                preview["details"].append("Send the existing reporter DM and configured public update, then close the report.")
                if report.get("ticket_channel_id"):
                    preview["details"].append(f"Ticket {report['ticket_channel_id']}: keep the {self.bot.cfg.ticket_close_delay_minutes}-minute cancellation grace period, send the configured transcript, then close the ticket. Any ticket message or Cancel keeps it open.")
            if clean["note"]:
                preview["details"].append("Details: " + clean["note"])
        if action == "block":
            raw = data.get("duration_minutes", "")
            if raw in ("", None):
                clean["duration_minutes"] = None
            else:
                minutes = snowflake(raw, "duration in minutes")
                if minutes > 5256000:
                    raise ValueError("Duration must be at most 5,256,000 minutes.")
                clean["duration_minutes"] = minutes
            preview["details"].extend(["Duration: " + (f"{clean['duration_minutes']} minutes from confirmation" if clean["duration_minutes"] else "Permanent"), "Reason shown to user: " + (clean["note"] or "None")])
        if action in {"panel", "liveboard_start"}:
            ch = self.destination(guild, actor, data.get("channel_id"))
            clean["channel_id"] = str(ch.id)
            preview["details"].append(f"Destination: {ch.id} (#{ch.name}); embed; no role notifications.")
            if action == "panel":
                from .cogs.panel import build_report_panel_embed
                preview["embed"] = build_report_panel_embed().to_dict()
                preview["details"].append("Buttons: Report IPTV, Report VOD")
            else:
                cog = self.bot.get_cog("LiveboardCog")
                if not cog:
                    raise ValueError("The liveboard feature is unavailable.")
                rows = self.db.list_reports_by_statuses(guild.id, ("Open", "Claimed", "Ticket Open"))
                preview["embed"] = cog.build_liveboard_embed(guild.id, [r for r in rows if r["report_type"] == "TV"], [r for r in rows if r["report_type"] == "VOD"]).to_dict()
                # The board timestamp reflects posting time, and is not report-state drift.
                description = preview["embed"]["description"]
                preview["embed"]["description"] = description.rsplit("Last update:", 1)[0] + "Last update: set when posted"
        if action == "bulk_close":
            rows = self.db.list_reports_by_statuses(guild.id, ("Open", "Claimed", "Ticket Open"))
            if not rows:
                raise ValueError("There are no active reports to close.")
            if any(self.pending(guild, r["id"]) for r in rows):
                raise ValueError("Wait for pending closures or cancel them in their tickets first.")
            clean["report_ids"] = [str(r["id"]) for r in rows]
            clean["revisions"] = {str(r["id"]): report_revision(r) for r in rows}
            preview["details"].append(f"Resolve {len(rows)} reports using the normal notifications and ticket cancellation grace periods. Cancelled tickets remain open.")
            preview["reports"] = [{"id": str(r["id"]), "subject": report_subject(r["report_type"], r["payload"]), "ticket": str(r.get("ticket_channel_id") or "")} for r in rows]
        if action == "pings":
            preview["details"].append("New-report staff pings will be " + ("OFF" if self.db.get_report_pings_enabled() else "ON") + ".")
        if action == "sync":
            preview["details"].append("Re-register the existing slash commands in this server.")
        if action in {"liveboard_refresh", "liveboard_stop"}:
            board = self.db.get_liveboard(guild.id)
            if not board:
                raise ValueError("There is no liveboard configured in this server.")
            preview["details"].append(f"Board message: {board['message_id']} in channel {board['channel_id']}" + (". Stops updates; the existing message stays." if action == "liveboard_stop" else ". Refresh its active reports."))
        return clean, self.revision(guild, clean), preview

    def view(self):
        cfg = self.bot.cfg
        return ReportActionView(self.db, cfg.staff_channel_id, cfg.support_channel_id, cfg.public_updates, cfg.staff_role_id, cfg.tickets_category_id)

    async def close_report(self, ctx, rid, action, note, authorize, expected):
        cfg = self.bot.cfg
        modal_type = ResolveReportModal if action == "resolve" else NotResolvedReportModal
        modal = modal_type(self.db, cfg.staff_channel_id, cfg.support_channel_id, cfg.public_updates, cfg.staff_role_id, cfg.tickets_category_id, rid, close_ticket_channel=True)
        async def finalize(interaction):
            await authorize()
            current = self.get_report(ctx.guild, rid)
            if report_revision(current) != expected:
                await ctx.send("The report changed during the grace period. It remains open; review again.")
                return
            await modal._finalize(interaction, note=note)
        await self.bot.ticket_closures.run(ctx, self.db, rid, finalize)
        current = self.get_report(ctx.guild, rid)
        if current["status"] not in CLOSED:
            await ctx.send(f"Report #{rid} remains open; its closure was cancelled or could not be completed.")

    def operation_lock(self, guild, key):
        return self.locks.setdefault((guild.id, key), asyncio.Lock())

    async def execute(self, guild, actor, data, authorize):
        key = data.get("report_id", "admin")
        if data["action"] == "bulk_close":
            return await self._execute(guild, actor, data, authorize)
        async with self.operation_lock(guild, key):
            return await self._execute(guild, actor, data, authorize)

    async def _execute(self, guild, actor, data, authorize):
        action = data["action"]
        channel = self.staff_channel(guild)
        ctx = WebReportContext(self.bot, guild, actor, channel)
        report = self.get_report(guild, data["report_id"]) if action in REPORT_ACTIONS else None
        if action in {"claim", "ticket", "reopen"}:
            ctx.message = await channel.fetch_message(int(report["staff_message_id"]))
        # Recheck after network reads, before starting any side effect.
        actor = await authorize()
        ctx.user = actor
        _, revision, _ = await self.prepare(guild, actor, data)
        if revision != data["revision"]:
            raise web.HTTPConflict(reason="The report or bot state changed. Review again.")
        if action == "claim":
            await self.view()._claim_report(ctx, report["id"], ctx.message)
        elif action == "ticket":
            await self.view().open_ticket.callback(ctx)
        elif action in {"resolve", "not_resolved"}:
            await self.close_report(ctx, report["id"], action, data["note"], authorize, report_revision(report))
        elif action == "bulk_close":
            # Individual closures preserve grace periods and avoid sweeping in later reports.
            async def close_one(rid):
                sub = WebReportContext(self.bot, guild, actor, channel)
                try:
                    async with self.operation_lock(guild, rid):
                        await self.close_report(sub, int(rid), "resolve", data["note"], authorize, data["revisions"][rid])
                except Exception:
                    await sub.send(f"Report #{rid}: closure could not be confirmed. Check its state.")
                return {"report_id": rid, "messages": sub.messages}
            results = await asyncio.gather(*(close_one(rid) for rid in data["report_ids"]))
            return {"messages": ["Bulk closure finished. Cancelled or changed reports remain open."], "reports": results}
        elif action in {"block", "unblock"}:
            cog = self.bot.get_cog("Moderation")
            if not cog:
                raise ValueError("Moderation tools are unavailable.")
            target = await self.bot.fetch_user(int(data["user_id"]))
            await authorize()
            if self.revision(guild, data) != data["revision"]:
                raise web.HTTPConflict(reason="The block changed. Review again.")
            if action == "block":
                await cog.reportblock.callback(cog, ctx, target, data["duration_minutes"], data["note"])
            else:
                await cog.reportunblock.callback(cog, ctx, target)
        elif action in {"reopen", "resend", "reassign", "pings", "sync"}:
            cog = self.bot.get_cog("Reports")
            if not cog:
                raise ValueError("Report tools are unavailable.")
            if action == "reassign":
                target = await guild.fetch_member(int(data["user_id"]))
                await authorize()
                if self.revision(guild, data) != data["revision"]:
                    raise web.HTTPConflict(reason="The report changed. Review again.")
                await cog.editreport.callback(cog, ctx, report["id"], target)
            else:
                command = {"reopen": "reportreactivate", "resend": "reportresend", "pings": "reportpings", "sync": "synccommands"}[action]
                args = (report["id"],) if report else ()
                await getattr(cog, command).callback(cog, ctx, *args)
        elif action == "panel":
            cog = self.bot.get_cog("ReportPanelCog")
            if not cog:
                raise ValueError("Report panel tools are unavailable.")
            ch = self.destination(guild, actor, data["channel_id"])
            await cog.reportpanel.callback(cog, ctx, ch)
        else:
            cog = self.bot.get_cog("LiveboardCog")
            if not cog:
                raise ValueError("Liveboard tools are unavailable.")
            command = {"liveboard_start": "liveboardstart", "liveboard_refresh": "liveboardrefresh", "liveboard_stop": "liveboardstop"}[action]
            args = (self.destination(guild, actor, data["channel_id"]),) if action == "liveboard_start" else ()
            await getattr(cog, command).callback(cog, ctx, *args)
        return {"messages": ctx.messages or ["Action finished. Refresh to check the report state."], "report_id": data.get("report_id")}
