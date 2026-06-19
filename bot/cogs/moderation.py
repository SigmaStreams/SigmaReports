import asyncio
import contextlib
import io
import textwrap
import traceback

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

OWNER_ID = 1229271933736976395


class EvalModal(discord.ui.Modal, title="Eval"):
    code = discord.ui.TextInput(
        label="Code",
        style=discord.TextStyle.paragraph,
        placeholder="Enter Python code…",
        required=True,
        max_length=2000,
    )

    def __init__(self, bot, db, cfg):
        super().__init__()
        self._bot = bot
        self._db = db
        self._cfg = cfg

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        code = self.code.value
        env = {
            "bot": self._bot,
            "db": self._db,
            "cfg": self._cfg,
            "interaction": interaction,
            "guild": interaction.guild,
            "channel": interaction.channel,
            "discord": discord,
            "asyncio": asyncio,
        }

        # Wrap in an async function so await works at top level
        wrapped = "async def _eval_body():\n" + textwrap.indent(code, "    ")

        stdout = io.StringIO()
        result = None
        error = None
        try:
            exec(compile(wrapped, "<eval>", "exec"), env)  # noqa: S102
            with contextlib.redirect_stdout(stdout):
                result = await env["_eval_body"]()
        except Exception:
            error = traceback.format_exc()

        output_parts = []
        printed = stdout.getvalue()
        if printed:
            output_parts.append(printed.rstrip())
        if result is not None:
            output_parts.append(repr(result))
        if error:
            output_parts.append(f"ERROR:\n{error.rstrip()}")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        # Truncate to fit in a code block within Discord's 2000-char limit
        max_len = 1950
        if len(output) > max_len:
            output = output[:max_len] + "\n… (truncated)"

        await interaction.followup.send(f"```py\n{output}\n```", ephemeral=True)


def _iso_to_discord_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        return f"<t:{ts}:R>"
    except Exception:
        return iso


class Moderation(commands.Cog):
    def __init__(self, bot, db, cfg):
        self.bot = bot
        self.db = db
        self.cfg = cfg

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return False
        return any(r.id == self.cfg.staff_role_id for r in member.roles)

    async def _send_modlog(self, guild: discord.Guild, embed: discord.Embed):
        cid = getattr(self.cfg, "modlogs_channel_id", 0) or 0
        if cid <= 0:
            return

        ch = guild.get_channel(cid)
        if not ch:
            return

        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass

    @app_commands.command(
        name="reportblock",
        description="Block a user from using /report commands (staff only).",
    )
    @app_commands.describe(
        user="User to block",
        duration_minutes="Minutes to block (leave empty for permanent)",
        reason="Optional reason shown to the user",
    )
    async def reportblock(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        duration_minutes: int | None = None,
        reason: str | None = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)
        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        self.db.block_user(
            guild_id=interaction.guild.id,
            user_id=user.id,
            created_by=interaction.user.id,
            duration_minutes=duration_minutes,
            reason=(reason or "").strip(),
        )

        # Build modlog embed
        embed = discord.Embed(title="Report system block", color=discord.Color.red())
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)

        if duration_minutes is None:
            embed.add_field(name="Duration", value="Permanent", inline=False)
        else:
            blocked, is_perm, expires_at, _ = self.db.is_user_blocked(interaction.guild.id, user.id)
            exp_txt = _iso_to_discord_ts(expires_at) if expires_at else "unknown"
            embed.add_field(name="Duration", value=f"{duration_minutes} minutes (expires {exp_txt})", inline=False)

        if reason and reason.strip():
            embed.add_field(name="Reason", value=reason.strip(), inline=False)

        await self._send_modlog(interaction.guild, embed)

        if duration_minutes is None:
            await interaction.response.send_message(f"✅ Blocked {user.mention} permanently.", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Blocked {user.mention} for {duration_minutes} minutes.", ephemeral=True)

    @app_commands.command(
        name="reportunblock",
        description="Remove a report-system block from a user (staff only).",
    )
    @app_commands.describe(user="User to unblock")
    async def reportunblock(self, interaction: discord.Interaction, user: discord.User):
        if not interaction.guild:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)
        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        removed = self.db.unblock_user(interaction.guild.id, user.id)

        embed = discord.Embed(title="Report system unblock", color=discord.Color.green())
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="Result", value="Unblocked" if removed else "User was not blocked", inline=False)
        await self._send_modlog(interaction.guild, embed)

        if removed:
            await interaction.response.send_message(f"✅ Unblocked {user.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {user.mention} wasn’t blocked.", ephemeral=True)

    @app_commands.command(
        name="reportblocks",
        description="List users currently blocked from using the report system (staff only).",
    )
    async def reportblocks(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)
        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        blocks = self.db.list_blocks(interaction.guild.id)
        if not blocks:
            return await interaction.response.send_message("No blocked users right now.", ephemeral=True)

        lines = []
        for b in blocks[:20]:
            user_id = b["user_id"]
            if b["is_permanent"]:
                status = "Permanent"
            else:
                status = f"Until {_iso_to_discord_ts(b['expires_at'])}" if b.get("expires_at") else "Temporary"
            reason_txt = f" — {b['reason']}" if b.get("reason") else ""
            lines.append(f"<@{user_id}> (`{user_id}`) — **{status}**{reason_txt}")

        extra = f"\n…and {len(blocks) - 20} more." if len(blocks) > 20 else ""

        embed = discord.Embed(
            title=f"Blocked users ({len(blocks)})",
            description="\n".join(lines) + extra,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------
    # Owner: eval
    # ----------------------------

    @app_commands.command(
        name="eval",
        description="Run arbitrary Python code (owner only).",
    )
    async def eval_cmd(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        await interaction.response.send_modal(EvalModal(self.bot, self.db, self.cfg))


async def setup(bot):
    await bot.add_cog(Moderation(bot, bot.db, bot.cfg))
