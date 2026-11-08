# Исходящие вебхуки: бот сам сообщает внешним системам (электронный журнал, LMS школы,
# Google-таблица через Apps Script, CRM) о результатах учеников.
#
#   WEBHOOK_URLS=https://example.ru/lumi,https://script.google.com/...   через запятую
#   WEBHOOK_SECRET=...   если задан, в заголовке X-Lumi-Signature будет HMAC-SHA256 от тела
#
# Тело - JSON: {"event": "quest_done", "user": 123, "nick": "...", "group": "8–9 класс",
#               "topic": "проценты", "value": 0.78, "ts": 1700000000}
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

import aiohttp

LOG = logging.getLogger(__name__)
# что отправляем наружу: итоги, а не каждое нажатие
EVENTS = {'quest_done', 'daily_done', 'boss_done', 'teach', 'assignment_done', 'onboarded'}


def sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Hooks:
    def __init__(self, urls=None, secret=None):
        urls = os.getenv('WEBHOOK_URLS', '') if urls is None else urls
        self.urls = [url.strip() for url in urls.split(',') if url.strip()]
        self.secret = os.getenv('WEBHOOK_SECRET', '') if secret is None else secret
        self.tasks = set()

    def emit(self, kind, p, topic='', value=None):
        if not self.urls or kind not in EVENTS:
            return
        body = json.dumps({'event': kind, 'user': p.get('uid'), 'nick': p.get('nick', ''),
                           'group': p.get('group', ''), 'topic': topic, 'value': value,
                           'ts': int(time.time())}, ensure_ascii=False).encode()
        # отправляем в фоне: ученик не должен ждать чужой сервер
        task = asyncio.get_running_loop().create_task(self.deliver(body))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def deliver(self, body):
        headers = {'Content-Type': 'application/json'}
        if self.secret:
            headers['X-Lumi-Signature'] = sign(self.secret, body)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for url in self.urls:
                for attempt in range(3):
                    try:
                        async with session.post(url, data=body, headers=headers) as response:
                            if response.status < 500:
                                if response.status >= 400:
                                    LOG.info('Вебхук %s ответил %s', url, response.status)
                                break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(2 ** attempt)
                else:
                    LOG.warning('Вебхук %s не доставлен', url)

    async def close(self):
        # перед выключением даём дослать то, что уже в пути
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=5)
