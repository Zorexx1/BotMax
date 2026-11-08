import asyncio
import json
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from hooks import Hooks, sign


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.got = []
        self.fail_first = 0

        async def receive(request):
            body = await request.read()
            if self.fail_first:
                self.fail_first -= 1
                return web.Response(status=503)
            self.got.append((body, request.headers.get('X-Lumi-Signature')))
            return web.json_response({'ok': True})
        app = web.Application()
        app.router.add_post('/hook', receive)
        self.server = TestServer(app)
        await self.server.start_server()
        self.url = str(self.server.make_url('/hook'))

    async def asyncTearDown(self):
        await self.server.close()

    async def test_result_is_delivered_and_signed(self):
        hooks = Hooks(self.url, 'секрет')
        hooks.emit('quest_done', {'uid': 7, 'nick': 'Нео', 'group': '8–9 класс'}, 'проценты', 0.78)
        await hooks.close()
        body, signature = self.got[0]
        self.assertEqual(signature, sign('секрет', body))
        data = json.loads(body)
        self.assertEqual((data['event'], data['user'], data['topic'], data['value']), ('quest_done', 7, 'проценты', 0.78))

    async def test_server_error_is_retried(self):
        self.fail_first = 1
        hooks = Hooks(self.url, '')
        with patch('hooks.asyncio.sleep', return_value=None):
            await hooks.deliver(b'{}')
        self.assertEqual(len(self.got), 1)
        self.assertIsNone(self.got[0][1])

    async def test_nothing_is_sent_without_urls_or_for_minor_events(self):
        Hooks('', '').emit('quest_done', {'uid': 1})
        hooks = Hooks(self.url, '')
        hooks.emit('xp', {'uid': 1}, value=10)
        self.assertFalse(hooks.tasks)
        await asyncio.sleep(0)
        self.assertEqual(self.got, [])
