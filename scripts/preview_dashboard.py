"""Loopback-only fictional dashboard; cannot sign in to Discord or execute changes."""
import argparse
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord
from aiohttp import web
from bot.closure import TicketClosureManager
from bot.db import ReportDB
from bot.web_dashboard import Dashboard, DashboardConfig, COOKIE


class PreviewDashboard(Dashboard):
    async def index(self, request):
        response = web.FileResponse(Path(__file__).resolve().parents[1] / 'bot/web/dashboard.html')
        response.set_cookie(COOKIE, 'fictional-preview', httponly=True, samesite='Lax')
        return response

    async def bootstrap(self, request):
        return web.json_response({'token':'fictional-preview-token','user_id':'55','guilds':[{'id':'1','name':'SigmaStreams · fictional preview'}], 'preview':True})

    async def confirm(self, request):
        raise web.HTTPForbidden(reason='This fictional preview cannot execute actions or contact Discord.')

    async def login(self, request):
        raise web.HTTPForbidden(reason='Discord sign-in is unavailable in this fictional preview.')

    async def callback(self, request):
        raise web.HTTPForbidden(reason='Discord sign-in is unavailable in this fictional preview.')


def build(port):
    db = ReportDB(':memory:')
    member = Mock(spec=discord.Member)
    member.id, member.name, member.mention = 55, 'staff.example', '<@55>'
    member.display_name = 'Example Staff'
    member.display_avatar.with_size.return_value.url = 'https://cdn.discordapp.com/embed/avatars/0.png'
    member.roles = [SimpleNamespace(id=99)]
    reporter = Mock(spec=discord.Member)
    reporter.id, reporter.name, reporter.mention = 11, 'member.example', '<@11>'
    reporter.display_name = 'Example Member'
    reporter.display_avatar.with_size.return_value.url = 'https://cdn.discordapp.com/embed/avatars/1.png'
    staff = Mock(spec=discord.TextChannel)
    staff.id, staff.name, staff.mention = 22, 'staff-reports', '<#22>'
    staff.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    guild = SimpleNamespace(id=1,name='SigmaStreams · fictional preview',fetch_member=AsyncMock(return_value=member),get_member=lambda uid:member if uid==55 else reporter,get_channel=lambda cid:staff if cid in {22,44,77} else None,text_channels=[staff],me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True)))
    cfg=SimpleNamespace(staff_role_id=99,staff_channel_id=22,support_channel_id=0,tickets_category_id=0,public_updates=True,responses_channel_id=44,transcripts_channel_id=0,ticket_close_delay_minutes=10)
    bot=SimpleNamespace(db=db,cfg=cfg,guilds=[guild],get_guild=lambda gid:guild if gid==1 else None,get_user=lambda uid:member if uid==55 else reporter,fetch_user=AsyncMock(return_value=reporter),is_ready=lambda:True,user=SimpleNamespace(id=100))
    bot.ticket_closures=TicketClosureManager(bot)
    bot.get_cog=lambda name:None
    samples=[('TV',{'channel_name':'BBC One HD','provider_name':'Example TV','device':'Android TV · TiviMate','issue':'No audio since this morning. The picture plays normally; other channels have sound.'},'Open'),('TV',{'channel_name':'Sky Sports Main Event','provider_name':'Example TV+','device':'Fire TV Stick','issue':'Frequent buffering during the match, roughly every 30 seconds.'},'Claimed'),('VOD',{'title':'The Example Show','english_title':'The Example Show','content_type':'tv','season_number':2,'episode_number':7,'episode_title':'The Long Way Home','language':'Standard','is_4k':'No','device':'Apple TV · Plex','requested_via_bot':'Yes','issue':'Audio drifts out of sync halfway through this episode.'},'Ticket Open'),('VOD',{'title':'An Example Film','content_type':'movie','language':'Foreign','is_4k':'Yes','device':'Samsung TV','issue':'Subtitles are missing for the foreign-language scenes.'},'Open'),('TV',{'channel_name':'Discovery HD','provider_name':'Example TV','device':'TiviMate','issue':'The guide shows the wrong programme.'},'Not Resolved'),('VOD',{'title':'Sample Adventure','content_type':'movie','device':'Plex Web','issue':'Playback fails before the opening credits.'},'Resolved')]
    for index,(kind,payload,status) in enumerate(samples):
        rid=db.create_report(kind,11,1,22,payload)
        db.set_staff_message_id(rid,330+index)
        if status=='Claimed': db.claim_report(rid,55)
        elif status=='Ticket Open':
            db.mark_claimed(rid,55,'2026-10-10T20:00:00+00:00'); db.set_ticket_channel_id(rid,77); db.update_status(rid,status)
        elif status in {'Resolved','Not Resolved'}: db.mark_closed(rid,status,55,'Fictional staff closure details.')
    dashboard=PreviewDashboard(bot,DashboardConfig(f'http://127.0.0.1:{port}','123','fictional-secret',port=port))
    import time
    dashboard.sessions['fictional-preview']={'user_id':55,'csrf':'fictional-preview-token','expires':time.monotonic()+14400}
    async def close_db(app): db.conn.close()
    dashboard.app.on_cleanup.append(close_db)
    return dashboard


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18867)
    args=parser.parse_args()
    dashboard=build(args.port)
    print(f'Fictional preview: http://127.0.0.1:{args.port}; changes and Discord traffic disabled.',flush=True)
    web.run_app(dashboard.app,host='127.0.0.1',port=args.port,access_log=None)
