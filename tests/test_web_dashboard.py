import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from bot.closure import TicketClosureManager
from bot.cogs.reports import Reports
from bot.db import ReportDB
from bot.web_actions import ReportActions, WebReportContext, report_revision, snowflake
from bot.web_dashboard import Dashboard, DashboardConfig, COOKIE, STATE_COOKIE, start_dashboard


def body(response):
    return json.loads(response.text)


class BaseDashboardCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = ReportDB(':memory:')
        self.addCleanup(self.db.conn.close)
        self.actor = Mock(spec=discord.Member)
        self.actor.id, self.actor.name, self.actor.mention = 55, 'staff', '<@55>'
        self.actor.roles = [SimpleNamespace(id=99)]
        self.reporter = Mock(spec=discord.Member)
        self.reporter.id, self.reporter.name, self.reporter.mention = 11, 'reporter', '<@11>'
        self.msg = SimpleNamespace(id=33, edit=AsyncMock())
        self.notice = SimpleNamespace(edit=AsyncMock())
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id, self.channel.name, self.channel.mention = 22, 'staff-reports', '<#22>'
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        self.channel.fetch_message = AsyncMock(return_value=self.msg)
        self.channel.send = AsyncMock(return_value=self.notice)
        self.channel.delete = AsyncMock()
        self.guild = SimpleNamespace(id=1, name='Test server', fetch_member=AsyncMock(return_value=self.actor), get_member=lambda uid: self.actor if uid==55 else self.reporter, get_channel=lambda cid: self.channel if cid in {22,44,77} else None, text_channels=[self.channel], me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True)))
        self.cfg = SimpleNamespace(staff_role_id=99, staff_channel_id=22, support_channel_id=0, tickets_category_id=0, public_updates=False, responses_channel_id=44, transcripts_channel_id=0, ticket_close_delay_minutes=10)
        self.bot = SimpleNamespace(db=self.db, cfg=self.cfg, guilds=[self.guild], get_guild=lambda gid: self.guild if gid==1 else None, get_user=lambda uid:self.reporter, fetch_user=AsyncMock(return_value=self.reporter), is_ready=lambda:True, user=SimpleNamespace(id=100), application_id=123)
        self.bot.ticket_closures = TicketClosureManager(self.bot, delay=.01)
        self.cogs = {'Reports':Reports(self.bot,self.db,self.cfg)}
        self.bot.get_cog=lambda name:self.cogs.get(name)
        self.dashboard = Dashboard(self.bot,DashboardConfig('http://127.0.0.1:8767','123','test-secret'))
        self.sid='test-session'
        self.dashboard.sessions[self.sid]={'user_id':55,'csrf':'test-csrf','expires':time.monotonic()+3600}
        self.rid=self.db.create_report('TV',11,1,22,{'channel_name':'News','issue':'No audio','provider_name':'Example TV'})
        self.db.set_staff_message_id(self.rid,33)
        self.authorize=AsyncMock(return_value=self.actor)
        self.addAsyncCleanup(self.dashboard.close)

    def request(self,path='/api/reports?guild_id=1',data=None,method=None,session=True,**headers):
        values={'Host':'127.0.0.1:8767'}
        if session: values['Cookie']=f'{COOKIE}={self.sid}'
        if data is not None: values.update({'Origin':'http://127.0.0.1:8767','X-Session-Token':'test-csrf','Content-Type':'application/json'})
        values.update(headers)
        request=make_mocked_request(method or ('POST' if data is not None else 'GET'),path,headers=values)
        request.json=AsyncMock(return_value=data)
        return request

    async def guarded(self,handler,path='/api/reports?guild_id=1',data=None,**kwargs):
        return await self.dashboard.guard(self.request(path,data,**kwargs),handler)

    async def prepared(self,action='claim',**data):
        clean,revision,preview=await self.dashboard.actions.prepare(self.guild,self.actor,{'action':action,'report_id':str(self.rid),**data})
        return {**clean,'revision':revision},preview


class DashboardTests(BaseDashboardCase):
    def uncached_reporter(self):
        self.guild.get_member = lambda uid: self.actor if uid == 55 else None
        self.bot.get_user = lambda uid: None
        self.bot.fetch_user.return_value = discord.User(state=Mock(), data={
            'id': '11', 'username': 'reporter.account', 'global_name': 'Reporter Name',
            'discriminator': '0', 'avatar': 'abc123'})

    async def test_uncached_reporter_name_avatar_and_search(self):
        self.uncached_reporter()
        response = await self.guarded(self.dashboard.report, f'/api/report?guild_id=1&report_id={self.rid}')
        user = body(response)['reporter']
        self.assertEqual(user['username'], 'reporter.account')
        self.assertEqual(user['display_name'], 'Reporter Name')
        self.assertEqual(user['avatar_url'], 'https://cdn.discordapp.com/avatars/11/abc123.png?size=64')
        for query in ('reporter.account', 'Reporter%20Name'):
            response = await self.guarded(self.dashboard.inventory, '/api/reports?guild_id=1&q=' + query)
            self.assertEqual(body(response)['total'], 1)
        self.bot.fetch_user.assert_awaited_once_with(11)

    async def test_lookup_shared_by_reports_and_concurrent_reads(self):
        self.uncached_reporter()
        self.db.create_report('VOD', 11, 1, 22, {'title': 'Film'})
        responses = await asyncio.gather(*(self.guarded(self.dashboard.inventory) for _ in range(3)))
        for response in responses:
            self.assertEqual(response.status, 200)
            self.assertEqual(body(response)['total'], 2)
        self.bot.fetch_user.assert_awaited_once_with(11)

    async def test_unavailable_identity_does_not_hide_report_and_retries_after_expiry(self):
        self.uncached_reporter()
        self.bot.fetch_user.side_effect = discord.NotFound(Mock(status=404), 'Unknown User')
        response = await self.guarded(self.dashboard.report, f'/api/report?guild_id=1&report_id={self.rid}')
        user = body(response)['reporter']
        self.assertEqual(response.status, 200)
        self.assertEqual(user, {'id': '11', 'username': 'User unavailable', 'display_name': 'User unavailable', 'avatar_url': None})
        await self.dashboard.users.get(self.guild, 11)
        self.bot.fetch_user.assert_awaited_once()
        self.dashboard.users.cache[(1, 11)] = (0, user)
        self.bot.fetch_user.side_effect = None
        self.assertEqual((await self.dashboard.users.get(self.guild, 11))['username'], 'reporter.account')
        self.assertEqual(self.bot.fetch_user.await_count, 2)

    async def test_cached_staff_avatars_and_default_reporter_avatar(self):
        self.uncached_reporter()
        self.bot.fetch_user.return_value = discord.User(state=Mock(), data={
            'id': '11', 'username': 'reporter.account', 'global_name': None,
            'discriminator': '0', 'avatar': None})
        self.actor.display_name = 'Staff Name'
        self.actor.display_avatar.with_size.return_value.url = 'https://cdn.discordapp.com/embed/avatars/2.png'
        self.db.claim_report(self.rid, 55)
        self.db.mark_closed(self.rid, 'Resolved', 55, 'Done')
        response = await self.guarded(self.dashboard.report, f'/api/report?guild_id=1&report_id={self.rid}')
        result = body(response)
        self.assertIn('/embed/avatars/', result['reporter']['avatar_url'])
        for field in ('claimant', 'resolver'):
            self.assertEqual(result[field]['display_name'], 'Staff Name')
            self.assertEqual(result[field]['avatar_url'], 'https://cdn.discordapp.com/embed/avatars/2.png')
        self.bot.fetch_user.assert_awaited_once_with(11)

    async def test_unauthorized_reads_do_not_fetch_identities(self):
        self.uncached_reporter()
        self.actor.roles = []
        response = await self.guarded(self.dashboard.inventory)
        self.assertEqual(response.status, 403)
        self.bot.fetch_user.assert_not_awaited()

    async def test_queue_fetches_only_visible_page_and_valid_filters(self):
        self.uncached_reporter()
        async def fetch_user(uid):
            return discord.User(state=Mock(), data={'id': str(uid), 'username': f'user{uid}',
                'discriminator': '0', 'avatar': None})
        self.bot.fetch_user.side_effect = fetch_user
        for index in range(60):
            self.db.create_report('TV', 1000 + index, 1, 22, {'channel_name': 'News'})
        response = await self.guarded(self.dashboard.inventory, '/api/reports?guild_id=1&status=invalid')
        self.assertEqual(response.status, 400)
        self.bot.fetch_user.assert_not_awaited()
        response = await self.guarded(self.dashboard.inventory)
        result = body(response)
        self.assertEqual(result['total'], 61)
        self.assertEqual(len(result['items']), 50)
        self.assertEqual(self.bot.fetch_user.await_count, 50)
        fetched_ids = {call.args[0] for call in self.bot.fetch_user.await_args_list}
        self.assertEqual(fetched_ids, {int(r['reporter_id']) for r in result['items']})

    async def test_admin_actions_are_no_longer_available(self):
        for action in ('block', 'unblock', 'panel', 'liveboard_start', 'liveboard_refresh', 'liveboard_stop', 'bulk_close', 'pings', 'sync'):
            response = await self.guarded(self.dashboard.preview, data={'action': action, 'guild_id': '1'})
            self.assertEqual(response.status, 400)
        self.assertFalse(self.dashboard.reviews)
        self.channel.send.assert_not_awaited()

    async def test_anonymous_inventory_is_protected(self):
        response=await self.guarded(self.dashboard.inventory,session=False)
        self.assertEqual(response.status,401)
        self.assertNotIn('News',response.text)

    async def test_host_origin_and_csrf_are_checked(self):
        for headers in ({'Host':'evil.test'},{'Origin':'https://evil.test'},{'X-Session-Token':'bad'}):
            with self.subTest(headers=headers):
                response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)},**headers)
                self.assertEqual(response.status,403)
        self.assertFalse(self.dashboard.reviews)

    async def test_wrong_content_type_and_non_object_json_rejected(self):
        response=await self.guarded(self.dashboard.preview,data={},**{'Content-Type':'text/plain'})
        self.assertEqual(response.status,400)
        response=await self.guarded(self.dashboard.preview,data=[])
        self.assertEqual(response.status,400)

    async def test_staff_revocation_and_channel_access_are_checked(self):
        self.actor.roles=[]
        response=await self.guarded(self.dashboard.inventory)
        self.assertEqual(response.status,403)
        self.actor.roles=[SimpleNamespace(id=99)]
        self.channel.permissions_for.return_value.view_channel=False
        response=await self.guarded(self.dashboard.inventory)
        self.assertEqual(response.status,403)

    async def test_server_and_report_isolation(self):
        foreign=self.db.create_report('VOD',11,2,22,{'title':'Foreign report'})
        response=await self.guarded(self.dashboard.inventory)
        self.assertEqual(body(response)['total'],1)
        response=await self.guarded(self.dashboard.report,f'/api/report?guild_id=1&report_id={foreign}')
        self.assertEqual(response.status,404)

    async def test_inventory_filters_and_snowflakes_as_strings(self):
        self.db.claim_report(self.rid,55)
        response=await self.guarded(self.dashboard.inventory,'/api/reports?guild_id=1&type=TV&assigned=mine&q=audio&provider=Example%20TV')
        result=body(response)
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['reporter_id'],'11')
        self.assertEqual(result['items'][0]['staff_url'],'https://discord.com/channels/1/22/33')
        response=await self.guarded(self.dashboard.inventory,'/api/reports?guild_id=1&type=VOD')
        self.assertEqual(body(response)['total'],0)

    async def test_preview_has_no_side_effect_and_requires_exact_yes(self):
        response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)})
        rid=body(response)['review_id']
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.db.get_by_id(self.rid)['status'],'Open')
        response=await self.guarded(self.dashboard.confirm,data={'review_id':rid,'confirmation':'Yes'})
        self.assertEqual(response.status,400)

    async def test_stale_review_cannot_execute(self):
        response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)})
        self.db.claim_report(self.rid,66)
        response=await self.guarded(self.dashboard.confirm,data={'review_id':body(response)['review_id'],'confirmation':'yes'})
        self.assertIn(response.status,{400,409})
        self.channel.send.assert_not_awaited()

    async def test_confirm_is_idempotent_and_session_bound(self):
        response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)})
        rid=body(response)['review_id']
        with patch.object(self.dashboard.actions,'execute',AsyncMock(return_value={'messages':['Done']})) as execute:
            first=await self.guarded(self.dashboard.confirm,data={'review_id':rid,'confirmation':'yes'})
            second=await self.guarded(self.dashboard.confirm,data={'review_id':rid,'confirmation':'yes'})
            await asyncio.gather(*self.dashboard.tasks)
            self.assertEqual(first.status,202)
            self.assertIn(body(second)['state'],{'running','done'})
            execute.assert_awaited_once()
        self.dashboard.sessions['other']={'user_id':55,'csrf':'test-csrf','expires':time.monotonic()+3600}
        self.sid='other'
        response=await self.guarded(self.dashboard.review_status,f'/api/review?review_id={rid}')
        self.assertEqual(response.status,404)

    async def test_discard_logout_and_expiry_remove_reviews(self):
        for operation in ('discard','logout','expiry'):
            self.dashboard.sessions[self.sid]={'user_id':55,'csrf':'test-csrf','expires':time.monotonic()+3600}
            response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)})
            rid=body(response)['review_id']
            if operation=='discard': await self.guarded(self.dashboard.discard,data={'review_id':rid})
            elif operation=='logout': await self.guarded(self.dashboard.logout,data={})
            else:
                self.dashboard.reviews[rid]['expires']=time.monotonic()-1
                self.dashboard.prune()
            self.assertNotIn(rid,self.dashboard.reviews)

    async def test_oauth_state_is_bound_to_browser_and_used_once(self):
        self.dashboard.states['state']={'expires':time.monotonic()+600}
        self.dashboard.exchange_code=AsyncMock(return_value=55)
        response=await self.guarded(self.dashboard.callback,'/auth/callback?state=state&code=test',**{'Cookie':f'{STATE_COOKIE}=wrong'})
        self.assertEqual(response.status,403)
        self.dashboard.exchange_code.assert_not_awaited()
        response=await self.guarded(self.dashboard.callback,'/auth/callback?state=state&code=test',**{'Cookie':f'{STATE_COOKIE}=state'})
        self.assertEqual(response.status,302)
        self.assertTrue(response.cookies[COOKIE]['httponly'])
        response=await self.guarded(self.dashboard.callback,'/auth/callback?state=state&code=test',**{'Cookie':f'{STATE_COOKIE}=state'})
        self.assertEqual(response.status,403)

    async def test_expired_oauth_and_session_are_rejected(self):
        self.dashboard.states['state']={'expires':time.monotonic()-1}
        response=await self.guarded(self.dashboard.callback,'/auth/callback?state=state&code=x',**{'Cookie':f'{STATE_COOKIE}=state'})
        self.assertEqual(response.status,403)
        self.dashboard.sessions[self.sid]['expires']=time.monotonic()-1
        response=await self.guarded(self.dashboard.inventory)
        self.assertEqual(response.status,401)

    async def test_claim_executes_existing_notification_once(self):
        data,_=await self.prepared()
        result=await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
        self.assertEqual(self.db.get_by_id(self.rid)['status'],'Claimed')
        self.channel.send.assert_awaited_once()
        self.assertIn('Claimed',' '.join(result['messages']))
        with self.assertRaises(ValueError): await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
        self.channel.send.assert_awaited_once()

    async def test_resolve_and_not_resolved_persist_details(self):
        for action,status in [('resolve','Resolved'),('not_resolved','Not Resolved')]:
            self.db.update_status(self.rid,'Open')
            data,_=await self.prepared(action,note='Staff explanation')
            await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
            report=self.db.get_by_id(self.rid)
            self.assertEqual(report['status'],status)
            self.assertEqual(report['resolved_note'],'Staff explanation')
            self.assertEqual(report['resolved_by'],55)

    async def test_ticket_closure_cancel_and_role_recheck(self):
        self.db.set_ticket_channel_id(self.rid,77)
        data,_=await self.prepared('resolve')
        self.bot.ticket_closures.delay=100
        task=asyncio.create_task(self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize))
        while not self.bot.ticket_closures.pending: await asyncio.sleep(0)
        await self.bot.ticket_closures.on_message(SimpleNamespace(author=SimpleNamespace(id=11),channel=SimpleNamespace(id=77)))
        await task
        self.assertEqual(self.db.get_by_id(self.rid)['status'],'Open')
        self.channel.delete.assert_not_awaited()
        self.bot.ticket_closures.delay=.01
        self.authorize.side_effect=[self.actor,web.HTTPForbidden(reason='Role removed')]
        with self.assertRaises(web.HTTPForbidden): await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
        self.assertEqual(self.db.get_by_id(self.rid)['status'],'Open')
        self.channel.delete.assert_not_awaited()

    async def test_state_change_during_grace_period_prevents_closure(self):
        self.db.set_ticket_channel_id(self.rid,77)
        data,_=await self.prepared('resolve')
        task=asyncio.create_task(self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize))
        while not self.bot.ticket_closures.pending: await asyncio.sleep(0)
        self.db.update_reporter_id(self.rid,66)
        await task
        self.assertEqual(self.db.get_by_id(self.rid)['status'],'Open')
        self.channel.delete.assert_not_awaited()

    async def test_reassign_ticket_and_invalid_actions_are_rejected(self):
        for action,extra in [('not_resolved',{}),('block',{'user_id':'11','duration_minutes':'-1'}),('invalid',{})]:
            with self.assertRaises(ValueError): await self.prepared(action,**extra)
        self.db.set_ticket_channel_id(self.rid,77)
        with self.assertRaises(ValueError): await self.prepared('reassign',user_id='11')

    async def test_headers_do_not_cache_private_reports(self):
        response=await self.guarded(self.dashboard.inventory)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])

    async def test_dashboard_disabled_by_default(self):
        with patch.dict(os.environ,{},clear=True),patch.object(Dashboard,'start',AsyncMock()) as start:
            await start_dashboard(self.bot)
            start.assert_not_awaited()


class ConfigTests(unittest.TestCase):
    def test_origin_and_secret_validation(self):
        for url in ('http://example.test','https://example.test/path','https://user:pass@example.test','https://example.test?x=1','https://example.test#x','https://example.test:bad'):
            with self.subTest(url=url),self.assertRaises(ValueError): DashboardConfig(url,'123','test-secret')
        config=DashboardConfig('https://example.test','123','private')
        self.assertNotIn('private',repr(config))
        self.assertTrue(config.secure)

    def test_snowflake_precision_and_invalid_input(self):
        self.assertEqual(snowflake('1457559352717086917','server'),1457559352717086917)
        for value in ('1.0','-1','0','1e3','９９',str(2**63)):
            with self.assertRaises(ValueError):snowflake(value,'server')

class ActionIntegrationTests(BaseDashboardCase):
    async def test_ticket_creation_reuses_persistent_view_and_is_serialized(self):
        self.guild.default_role=Mock(spec=discord.Role)
        self.guild.get_role=lambda rid:Mock(spec=discord.Role)
        self.guild.me=Mock(spec=discord.Member)
        self.guild.me.guild_permissions=SimpleNamespace(manage_channels=True)
        ticket=Mock(spec=discord.TextChannel)
        ticket.id,ticket.mention=77,'<#77>'
        ticket.send=AsyncMock()
        self.guild.create_text_channel=AsyncMock(return_value=ticket)
        data,_=await self.prepared('ticket')
        results=await asyncio.gather(self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize),self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize),return_exceptions=True)
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.db.get_by_id(self.rid)['status'],'Ticket Open')
        self.assertEqual(self.db.get_by_id(self.rid)['ticket_channel_id'],77)
        self.assertEqual(sum(isinstance(r,Exception) for r in results),1)
        self.assertIsNotNone(ticket.send.call_args.kwargs['view'])

    async def test_reopen_resend_and_reassign_dispatch_existing_callbacks(self):
        self.db.mark_closed(self.rid,'Resolved',55,'Done')
        data,_=await self.prepared('reopen')
        await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
        self.assertEqual(self.db.get_by_id(self.rid)['status'],'Open')
        other=self.db.create_report('VOD',11,1,22,{'title':'Movie','issue':'Playback failure'})
        self.channel.send.return_value=SimpleNamespace(id=88)
        data,_=await self.prepared('resend',report_id=str(other))
        await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
        self.assertEqual(self.db.get_by_id(other)['staff_message_id'],88)
        target=Mock(spec=discord.Member)
        target.id,target.name,target.mention=66,'replacement','<@66>'
        self.guild.fetch_member.return_value=target
        data,_=await self.prepared('reassign',user_id='66')
        await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
        self.assertEqual(self.db.get_by_id(self.rid)['reporter_id'],66)

    async def test_resend_review_includes_notifications_and_rejects_ping_drift(self):
        rid=self.db.create_report('TV',11,1,22,{'channel_name':'News','issue':'No sound'})
        self.cfg.tv_staff_ping_user_ids=[55]
        response=await self.guarded(self.dashboard.preview,data={'action':'resend','guild_id':'1','report_id':str(rid)})
        preview=body(response)
        self.assertEqual(preview['preview']['content'],'<@55>')
        self.assertIn('embed',preview['preview'])
        self.db.toggle_report_pings()
        response=await self.guarded(self.dashboard.confirm,data={'review_id':preview['review_id'],'confirmation':'yes'})
        self.assertEqual(response.status,409)
        self.channel.send.assert_not_awaited()

    async def test_permission_removed_after_preview_and_role_removed_before_confirm(self):
        data,_=await self.prepared('claim')
        self.channel.permissions_for.return_value.send_messages=False
        with self.assertRaises(ValueError):await self.dashboard.actions.execute(self.guild,self.actor,data,self.authorize)
        self.channel.send.assert_not_awaited()
        self.channel.permissions_for.return_value.send_messages=True
        response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)})
        self.actor.roles=[]
        response=await self.guarded(self.dashboard.confirm,data={'review_id':body(response)['review_id'],'confirmation':'yes'})
        self.assertEqual(response.status,403)

    async def test_signout_while_authorizing_cannot_start_action(self):
        response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)})
        rid=body(response)['review_id']
        async def fetch_member(uid):
            self.dashboard.sessions.pop(self.sid,None)
            return self.actor
        self.guild.fetch_member.side_effect=fetch_member
        response=await self.guarded(self.dashboard.confirm,data={'review_id':rid,'confirmation':'yes'})
        self.assertEqual(response.status,401)
        self.assertFalse(self.dashboard.tasks)

    async def test_unexpected_execution_failure_is_not_retried(self):
        response=await self.guarded(self.dashboard.preview,data={'action':'claim','guild_id':'1','report_id':str(self.rid)})
        rid=body(response)['review_id']
        with patch.object(self.dashboard.actions,'execute',AsyncMock(side_effect=RuntimeError('private-secret'))) as execute:
            with self.assertLogs('bot.web_dashboard',level='ERROR') as logs:
                await self.guarded(self.dashboard.confirm,data={'review_id':rid,'confirmation':'yes'})
                await asyncio.gather(*self.dashboard.tasks)
            response=await self.guarded(self.dashboard.confirm,data={'review_id':rid,'confirmation':'yes'})
            execute.assert_awaited_once()
            self.assertIn('could not be confirmed',body(response)['error'])
            self.assertNotIn('private-secret',str(logs.output)+response.text)

class HttpTests(BaseDashboardCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from aiohttp.test_utils import TestClient, TestServer
        self.client=TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()
        self.dashboard.config.public_url=str(self.client.make_url('')).rstrip('/')
        self.addAsyncCleanup(self.client.close)

    async def test_http_assets_signin_and_anonymous_api(self):
        response=await self.client.get('/api/admin?guild_id=1',headers={'Cookie':f'{COOKIE}={self.sid}'})
        self.assertEqual(response.status,404)
        response=await self.client.get('/')
        self.assertIn('Sign in with Discord',await response.text())
        response=await self.client.get('/assets/dashboard.js')
        self.assertEqual(response.status,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        response=await self.client.get('/assets/config.py')
        self.assertEqual(response.status,404)
        response=await self.client.get('/api/reports?guild_id=1')
        self.assertEqual(response.status,401)
        response=await self.client.get('/auth/login',allow_redirects=False)
        self.assertEqual(response.status,302)
        self.assertIn('scope=identify',response.headers['Location'])
        self.assertTrue(response.cookies[STATE_COOKIE]['httponly'])

    async def test_http_authenticated_reads_and_mutation_origin(self):
        response=await self.client.get('/api/report?guild_id=1&report_id='+str(self.rid),headers={'Cookie':f'{COOKIE}={self.sid}'})
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['subject'],'News')
        response=await self.client.post('/api/preview',json={'guild_id':'1','action':'claim','report_id':str(self.rid)},headers={'Cookie':f'{COOKIE}={self.sid}','X-Session-Token':'test-csrf'})
        self.assertEqual(response.status,403)
