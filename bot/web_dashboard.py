"""Optional Discord-authenticated staff dashboard, hosted by the existing bot."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
import discord

from .web_actions import ReportActions, snowflake, CLOSED
from .web_users import UserIdentities
from .utils import report_subject

ASSETS = Path(__file__).parent / "web"
COOKIE = "sigmareports_session"
STATE_COOKIE = "sigmareports_oauth_state"
LIFETIME = 4 * 3600
CAPACITY = 1000
log = logging.getLogger(__name__)


@dataclass
class DashboardConfig:
    public_url: str
    client_id: str
    client_secret: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8767

    def __post_init__(self):
        self.public_url = self.public_url.rstrip("/")
        parsed = urlsplit(self.public_url)
        parsed.port  # Validate malformed port values before serving.
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query
                or parsed.fragment or parsed.scheme not in {"http", "https"}
                or parsed.scheme == "http" and not local):
            raise ValueError("WEB_DASHBOARD_URL must be an HTTPS origin or a loopback HTTP origin.")
        if not self.client_id.isdigit() or not self.client_secret:
            raise ValueError("Configure the dashboard's Discord OAuth client ID and secret.")
        if not 1 <= self.port <= 65535:
            raise ValueError("WEB_DASHBOARD_PORT must be between 1 and 65535.")

    @property
    def secure(self):
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, bot):
        return cls(public_url=os.getenv("WEB_DASHBOARD_URL", ""),
                   client_id=os.getenv("WEB_DASHBOARD_CLIENT_ID", "").strip() or str(bot.application_id or ""),
                   client_secret=os.getenv("WEB_DASHBOARD_CLIENT_SECRET", ""),
                   host=os.getenv("WEB_DASHBOARD_HOST", "127.0.0.1"),
                   port=int(os.getenv("WEB_DASHBOARD_PORT", "8767")))


class Dashboard:
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self.actions = ReportActions(bot)
        self.users = UserIdentities(bot)
        self.sessions = {}
        self.states = {}
        self.reviews = {}
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.http = None
        self.runner = None
        self.app = web.Application(client_max_size=16 * 1024, middlewares=[self.guard])
        self.app.cleanup_ctx.append(self.http_context)
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/assets/{asset}", self.asset)
        self.app.router.add_get("/auth/login", self.login)
        self.app.router.add_get("/auth/callback", self.callback)
        self.app.router.add_post("/auth/logout", self.logout)
        self.app.router.add_get("/api/bootstrap", self.bootstrap)
        self.app.router.add_get("/api/reports", self.inventory)
        self.app.router.add_get("/api/report", self.report)
        self.app.router.add_get("/api/review", self.review_status)
        self.app.router.add_post("/api/preview", self.preview)
        self.app.router.add_post("/api/confirm", self.confirm)
        self.app.router.add_post("/api/discard", self.discard)

    async def http_context(self, app):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
            self.http = client
            cleanup = asyncio.create_task(self.prune_loop())
            try:
                yield
            finally:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup

    async def prune_loop(self):
        while True:
            await asyncio.sleep(30)
            self.prune()

    def prune(self):
        now = time.monotonic()
        for collection in (self.sessions, self.states, self.reviews):
            for key, item in list(collection.items()):
                if item["expires"] <= now and item.get("state") != "running":
                    del collection[key]
        for key, review in list(self.reviews.items()):
            if review["session_id"] not in self.sessions and review["state"] != "running":
                del self.reviews[key]

    @web.middleware
    async def guard(self, request, handler):
        self.prune()
        try:
            if request.host != urlsplit(self.config.public_url).netloc:
                raise web.HTTPForbidden(reason="Unexpected dashboard host.")
            if request.method not in {"GET", "HEAD"}:
                session = self.session(request)
                if (request.headers.get("Origin") != self.config.public_url or
                        not secrets.compare_digest(request.headers.get("X-Session-Token", ""), session["csrf"])):
                    raise web.HTTPForbidden(reason="Invalid dashboard origin or session token.")
                if request.content_type != "application/json":
                    raise web.HTTPBadRequest(reason="Expected a JSON request.")
            response = await handler(request)
        except (ValueError, json.JSONDecodeError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException as exc:
            response = exc if 300 <= exc.status < 400 else web.json_response({"error": exc.reason}, status=exc.status)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
            response = web.json_response({"error": "Discord is unavailable. Check the report and delivery state before retrying an action."}, status=503)
        except (sqlite3.Error, OSError):
            response = web.json_response({"error": "The database is unavailable. Check state before retrying an action."}, status=503)
        except Exception:
            # Deliberately omit exceptions/tracebacks, which can contain secrets.
            log.error("SigmaReports dashboard request failed; inspect state before retrying.")
            response = web.json_response({"error": "The action could not be confirmed. Check state before retrying."}, status=500)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                 "img-src 'self' https://cdn.discordapp.com; "
                                 "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})
        return response

    def session(self, request):
        session = self.sessions.get(request.cookies.get(COOKIE, ""))
        if not session or session["expires"] <= time.monotonic():
            raise web.HTTPUnauthorized(reason="Sign in with Discord to use SigmaReports.")
        return session

    async def actor(self, guild, user_id):
        try:
            member = await guild.fetch_member(user_id)
            if not any(role.id == self.bot.cfg.staff_role_id for role in member.roles):
                raise web.HTTPForbidden(reason="SigmaReports staff access is required.")
            channel = guild.get_channel(self.bot.cfg.staff_channel_id)
            if not isinstance(channel, discord.TextChannel) or not channel.permissions_for(member).view_channel:
                raise web.HTTPForbidden(reason="Access to the staff reports channel is required.")
            return member
        except discord.NotFound:
            raise web.HTTPForbidden(reason="You must be a current member of this server.") from None

    async def allowed(self, user_id):
        for guild in self.bot.guilds:
            try:
                await self.actor(guild, user_id)
                return True
            except web.HTTPForbidden:
                pass
        return False

    async def staff(self, request, guild_id=None):
        session = self.session(request)
        guild_id = snowflake(guild_id or request.query.get("guild_id"), "server")
        guild = self.bot.get_guild(guild_id)
        if guild is None or not self.bot.is_ready():
            raise web.HTTPServiceUnavailable(reason="This server is unavailable while the bot reconnects.")
        actor = await self.actor(guild, session["user_id"])
        if self.session(request) is not session:
            raise web.HTTPUnauthorized(reason="This session changed. Sign in again.")
        return session, guild, actor

    async def json(self, request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    async def index(self, request):
        if request.cookies.get(COOKIE, "") not in self.sessions:
            return web.FileResponse(ASSETS / "signin.html")
        if not await self.allowed(self.session(request)["user_id"]):
            raise web.HTTPForbidden(reason="SigmaReports staff access is required.")
        return web.FileResponse(ASSETS / "dashboard.html")

    async def asset(self, request):
        name = request.match_info["asset"]
        if name not in {"dashboard.css", "dashboard.js"}:
            raise web.HTTPNotFound()
        return web.FileResponse(ASSETS / name)

    async def login(self, request):
        if len(self.states) >= CAPACITY:
            raise web.HTTPTooManyRequests(reason="Too many pending sign-ins. Try again later.")
        state = secrets.token_urlsafe(32)
        self.states[state] = {"expires": time.monotonic() + 600}
        params = {"client_id": self.config.client_id, "response_type": "code", "scope": "identify",
                  "redirect_uri": self.config.public_url + "/auth/callback", "state": state}
        response = web.Response(status=302, headers={"Location": "https://discord.com/oauth2/authorize?" + urlencode(params)})
        response.set_cookie(STATE_COOKIE, state, max_age=600, secure=self.config.secure,
                            httponly=True, samesite="Lax", path="/auth")
        return response

    async def exchange_code(self, code):
        try:
            async with self.http.post("https://discord.com/api/v10/oauth2/token", data={
                    "client_id": self.config.client_id, "client_secret": self.config.client_secret,
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": self.config.public_url + "/auth/callback"}, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError("Discord sign-in failed. Start again.")
                tokens = await response.json()
            async with self.http.get("https://discord.com/api/v10/users/@me", headers={
                    "Authorization": "Bearer " + tokens["access_token"]}, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError("Discord identity verification failed.")
                user = await response.json()
            return snowflake(user["id"], "Discord identity")
        except (KeyError, TypeError):
            raise ValueError("Discord sign-in failed. Start again.") from None

    async def callback(self, request):
        state = request.query.get("state", "")
        record = self.states.get(state)
        if (not record or not secrets.compare_digest(state, request.cookies.get(STATE_COOKIE, ""))):
            raise web.HTTPForbidden(reason="Invalid or expired Discord sign-in. Start again.")
        del self.states[state]
        code = request.query.get("code")
        if not code:
            raise ValueError("Sign-in was cancelled. Return to SigmaReports to sign in.")
        user_id = await self.exchange_code(code)
        if not await self.allowed(user_id):
            raise web.HTTPForbidden(reason="SigmaReports staff access is required.")
        if len(self.sessions) >= CAPACITY:
            raise web.HTTPTooManyRequests(reason="Too many dashboard sessions. Try again later.")
        old_id = request.cookies.get(COOKIE, "")
        self.sessions.pop(old_id, None)
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = {"user_id": user_id, "csrf": secrets.token_urlsafe(32),
                                     "expires": time.monotonic() + LIFETIME}
        response = web.Response(status=302, headers={"Location": "/"})
        response.set_cookie(COOKIE, session_id, max_age=LIFETIME, secure=self.config.secure,
                            httponly=True, samesite="Lax", path="/")
        response.del_cookie(STATE_COOKIE, path="/auth")
        return response

    async def logout(self, request):
        self.sessions.pop(request.cookies.get(COOKIE, ""), None)
        self.prune()
        response = web.json_response({"signed_out": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def bootstrap(self, request):
        session = self.session(request)
        guilds = []
        for guild in self.bot.guilds:
            try:
                await self.actor(guild, session["user_id"])
                guilds.append({"id": str(guild.id), "name": guild.name})
            except web.HTTPForbidden:
                pass
        if not guilds:
            raise web.HTTPForbidden(reason="SigmaReports staff access is required.")
        return web.json_response({"token": session["csrf"], "user_id": str(session["user_id"]), "guilds": guilds})

    def page(self, request, rows):
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            raise ValueError("Choose a valid page.") from None
        if page < 1 or page > 100000:
            raise ValueError("Choose a valid page.")
        return {"items": rows[(page - 1) * 50:page * 50], "total": len(rows), "page": page, "pages": max(1, (len(rows) + 49) // 50)}

    async def report_data(self, guild, report):
        row = dict(report)
        for key in ("id", "guild_id", "reporter_id", "source_channel_id", "staff_message_id", "ticket_channel_id", "claimed_by_user_id", "resolved_by"):
            row[key] = str(row[key]) if row.get(key) else None
        row["subject"] = report_subject(report["report_type"], report["payload"])
        row["reporter"], row["claimant"], row["resolver"] = await asyncio.gather(
            self.users.get(guild, report["reporter_id"]),
            self.users.get(guild, report.get("claimed_by_user_id")),
            self.users.get(guild, report.get("resolved_by")))
        row["closing"] = self.actions.pending(guild, report["id"])
        row["staff_url"] = f"https://discord.com/channels/{guild.id}/{self.bot.cfg.staff_channel_id}/{report['staff_message_id']}" if report.get("staff_message_id") else None
        row["ticket_url"] = f"https://discord.com/channels/{guild.id}/{report['ticket_channel_id']}" if report.get("ticket_channel_id") else None
        return row

    async def inventory(self, request):
        _, guild, actor = await self.staff(request)
        rows = self.bot.db.list_active_reports(guild.id)
        counts = {"active": sum(r["status"] not in CLOSED for r in rows), "claimed": sum(r["status"] == "Claimed" for r in rows), "tickets": sum(bool(r["ticket_channel_id"]) for r in rows), "closed": sum(r["status"] in CLOSED for r in rows)}
        status = request.query.get("status", "active")
        kind = request.query.get("type", "")
        assigned = request.query.get("assigned", "")
        provider = request.query.get("provider", "")
        query = request.query.get("q", "").strip().casefold()
        if status not in {"active", "all", "Open", "Claimed", "Ticket Open", "Resolved", "Not Resolved"} or kind not in {"", "TV", "VOD"} or assigned not in {"", "mine", "unassigned"}:
            raise ValueError("Choose a valid report filter.")
        providers = sorted({str(r["payload"].get("provider_name") or r["payload"].get("provider_id") or "") for r in rows} - {""})
        result = []
        for row in rows:
            if status == "active" and row["status"] in CLOSED or status not in {"active", "all"} and row["status"] != status:
                continue
            if kind and row["report_type"] != kind:
                continue
            if assigned == "mine" and row.get("claimed_by_user_id") != actor.id or assigned == "unassigned" and row.get("claimed_by_user_id"):
                continue
            if provider and str(row["payload"].get("provider_name") or row["payload"].get("provider_id") or "") != provider:
                continue
            result.append(row)
        if query:
            ids = {r["reporter_id"] for r in result}
            identities = dict(zip(ids, await asyncio.gather(*(self.users.get(guild, uid) for uid in ids))))
            result = [r for r in result if query in " ".join((
                str(r["id"]), report_subject(r["report_type"], r["payload"]),
                identities[r["reporter_id"]]["username"], identities[r["reporter_id"]]["display_name"],
                str(r["reporter_id"]), json.dumps(r["payload"], ensure_ascii=False))).casefold()]
        page = self.page(request, result)
        page["items"] = await asyncio.gather(*(self.report_data(guild, r) for r in page["items"]))
        return web.json_response({**page, "counts": counts, "providers": providers})

    async def report(self, request):
        _, guild, _ = await self.staff(request)
        return web.json_response(await self.report_data(guild, self.actions.get_report(guild, request.query.get("report_id"))))

    async def preview(self, request):
        data = await self.json(request)
        session, guild, actor = await self.staff(request, data.get("guild_id"))
        if len(self.reviews) >= CAPACITY:
            raise web.HTTPTooManyRequests(reason="Too many pending reviews. Discard old reviews first.")
        clean, revision, preview = await self.actions.prepare(guild, actor, data)
        review_id = secrets.token_urlsafe(32)
        self.reviews[review_id] = {"session_id": request.cookies[COOKIE], "expires": min(session["expires"], time.monotonic() + 300), "data": clean, "revision": revision, "state": "open", "preview": preview}
        return web.json_response({"review_id": review_id, "preview": preview})

    def review(self, request, rid):
        review = self.reviews.get(rid)
        if not review or review["session_id"] != request.cookies.get(COOKIE):
            raise web.HTTPNotFound(reason="This review expired or belongs to another session. Review again.")
        return review

    async def discard(self, request):
        data = await self.json(request)
        review = self.review(request, data.get("review_id"))
        if review["state"] == "open":
            del self.reviews[data["review_id"]]
        return web.json_response({"discarded": True})

    def outcome(self, review):
        return {"state": review["state"], **review.get("outcome", {})}

    async def review_status(self, request):
        review = self.review(request, request.query.get("review_id"))
        await self.staff(request, review["data"]["guild_id"])
        return web.json_response(self.outcome(review))

    async def run_action(self, request, review, guild, actor):
        async def authorize():
            _, _, current_actor = await self.staff(request, guild.id)
            return current_actor
        try:
            outcome = await self.actions.execute(guild, actor, {**review["data"], "revision": review["revision"]}, authorize)
        except web.HTTPException as exc:
            outcome = {"error": exc.reason}
        except Exception:
            log.error("SigmaReports action could not be confirmed; check report and delivery state.")
            outcome = {"error": "The action could not be confirmed. Check report, ticket, and delivery state before a new review."}
        else:
            log.info("SigmaReports web action completed: actor=%s guild=%s action=%s report=%s", actor.id, guild.id, review["data"]["action"], review["data"]["report_id"])
        review.update(state="done", outcome=outcome)
        # Keep completed feedback pollable during the remaining session.
        session = self.sessions.get(review["session_id"])
        review["expires"] = session["expires"] if session else time.monotonic() + 300

    async def confirm(self, request):
        data = await self.json(request)
        async with self.lock:
            review = self.review(request, data.get("review_id"))
            _, guild, actor = await self.staff(request, review["data"]["guild_id"])
            if review["state"] != "open":
                return web.json_response(self.outcome(review))
            if review["expires"] <= time.monotonic():
                raise web.HTTPConflict(reason="This review expired. Review again.")
            if data.get("confirmation") != "yes":
                raise ValueError("Type yes to confirm this reviewed action.")
            _, revision, preview = await self.actions.prepare(guild, actor, review["data"])
            if revision != review["revision"] or preview != review["preview"]:
                raise web.HTTPConflict(reason="The reviewed state changed. Reload and review again.")
            # A logout/discard may occur while fresh authorization is being fetched.
            self.session(request)
            if self.reviews.get(data["review_id"]) is not review:
                raise web.HTTPConflict(reason="This review was discarded. Review again.")
            review["state"] = "running"
            task = asyncio.create_task(self.run_action(request, review, guild, actor))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return web.json_response(self.outcome(review), status=202)

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, self.config.host, self.config.port).start()
        except Exception:
            await self.runner.cleanup()
            self.runner = None
            raise

    async def close(self):
        await self.users.close()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self.sessions.clear()
        self.states.clear()
        self.reviews.clear()


async def start_dashboard(bot):
    if os.getenv("WEB_DASHBOARD_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        return
    if getattr(bot, "web_dashboard", None) is not None:
        return
    dashboard = Dashboard(bot, DashboardConfig.from_env(bot))
    await dashboard.start()
    bot.web_dashboard = dashboard
    log.info("SigmaReports dashboard started.")
