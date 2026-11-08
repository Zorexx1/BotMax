# Проверка бота целиком: поднимаем поддельные MAX и GigaChat, запускаем настоящий main.py
# (у себя или в docker) и проходим основные сценарии от лица двух пользователей.
#
#   python tests/e2e_check.py                    бот запускается здесь же
#   python tests/e2e_check.py --docker IMAGE     бот запускается в собранном образе
#
# В сеть не ходит, токены не нужны.
import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
MAX_PORT, GIGA_PORT, WEB_PORT = 18901, 18902, 18903
QUEST = {
    's1': ('Скин стоит 800 рублей, скидка 25%.', 'Сколько рублей составит скидка?', 'calc', [], '200 рублей'),
    's2': ('В магазине А скин за 650, в Б за 600.', 'Где выгоднее?', 'choice', ['Магазин А', 'Магазин Б', 'Одинаково'], 'Магазин Б'),
    's3': ('Налог 10% к цене 600 рублей.', 'Сколько рублей заплатишь итого?', 'calc', [], '660 рублей'),
}
ANSWERS = {q[1]: q[4] for q in QUEST.values()}
EXPLANATION = ('Электромагнитная индукция — это появление тока в контуре, когда меняется магнитный поток. '
               '**ЭДС** равна $\\mathcal{E} = -\\frac{d\\Phi}{dt}$, где $\\Phi$ — магнитный поток.\n\n'
               'Пример из жизни: зарядка телефона без проводов.\n\nХочешь подробнее — уточни, что интересно.')


class FakeMax:
    def __init__(self):
        self.updates = asyncio.Queue()
        self.sent = []      # всё, что бот отправил; text - то, что видно сейчас, shown - все версии
        self.answers = []   # ответы на нажатия (правка меню на месте)
        self.hooks = []     # что бот отправил на вебхук
        self.seq = 0

    def find(self, mid):
        return next((m for m in self.sent if m['mid'] == mid), None)

    def user(self, uid):
        return {'user_id': uid, 'first_name': f'Ученик{uid}', 'last_name': 'Тестов', 'is_bot': False,
                'last_activity_time': 0}

    def message(self, uid, text):
        self.seq += 1
        return {'sender': self.user(uid), 'recipient': {'chat_id': 1000 + uid, 'chat_type': 'dialog', 'user_id': 999},
                'timestamp': int(time.time() * 1000), 'body': {'mid': f'in-{self.seq}', 'seq': self.seq, 'text': text}}

    def say(self, uid, text):
        self.updates.put_nowait({'update_type': 'message_created', 'timestamp': int(time.time() * 1000),
                                 'message': self.message(uid, text)})

    def press(self, uid, payload):
        self.seq += 1
        self.updates.put_nowait({'update_type': 'message_callback', 'timestamp': int(time.time() * 1000),
                                 'callback': {'timestamp': int(time.time() * 1000), 'callback_id': f'cb-{self.seq}',
                                              'payload': payload, 'user': self.user(uid)},
                                 'message': self.message(uid, '')})

    def start(self, uid):
        self.updates.put_nowait({'update_type': 'bot_started', 'timestamp': int(time.time() * 1000),
                                 'chat_id': 1000 + uid, 'user': self.user(uid)})

    async def handler(self, request):
        path, query = request.path, dict(request.query)
        body = await request.read()
        data = json.loads(body) if body.startswith(b'{') else {}
        if path == '/hook':
            self.hooks.append(data)
            return web.json_response({'ok': True})
        if path == '/me':
            return web.json_response({'user_id': 999, 'first_name': 'Люми', 'username': 'lumi_test_bot',
                                      'is_bot': True, 'last_activity_time': 0})
        if path == '/updates':
            items = []
            try:
                items.append(await asyncio.wait_for(self.updates.get(), timeout=1))
                while not self.updates.empty():
                    items.append(self.updates.get_nowait())
            except asyncio.TimeoutError:
                pass
            return web.json_response({'updates': items, 'marker': self.seq})
        if path == '/subscriptions':
            return web.json_response({'subscriptions': []})
        if path == '/uploads':
            return web.json_response({'url': f'http://127.0.0.1:{MAX_PORT}/upload'})
        if path == '/upload':
            return web.json_response({'photos': {'x': {'token': 'image-token'}}})
        if path == '/messages' and request.method == 'POST':
            chat = int(query.get('chat_id') or 0) or 1000 + int(query.get('user_id') or 0)
            self.seq += 1
            text = data.get('text', '')
            self.sent.append({'chat': chat, 'mid': f'out-{self.seq}', 'text': text, 'shown': [text],
                              'attachments': data.get('attachments') or [], 'created': time.time(), 'at': time.time()})
            return web.json_response({'message': {'recipient': {'chat_id': chat, 'chat_type': 'dialog'},
                                                  'timestamp': int(time.time() * 1000),
                                                  'body': {'mid': f'out-{self.seq}', 'seq': self.seq, 'text': text}}})
        if path == '/messages' and request.method in ('PUT', 'DELETE'):
            found = self.find(query.get('message_id'))
            if not found:
                return web.json_response({'code': 'not.found', 'message': 'message not found'}, status=404)
            if request.method == 'DELETE':
                found['deleted'] = True
            else:
                found['text'] = data.get('text', found['text'])
                found['attachments'] = data.get('attachments') or []
                found['shown'].append(found['text'])
                found['at'] = time.time()
            return web.json_response({'success': True})
        if path == '/answers':
            self.answers.append({'text': (data.get('message') or {}).get('text') or '', 'raw': data, 'at': time.time()})
            return web.json_response({'success': True})
        return web.json_response({'success': True})


def quest_args(stages, messy=False):
    args = {'allowed': True, 'title': 'Скин мечты', 'emoji': '🎮', 'role': 'Геймер', 'goal': 'Купить скин',
            'intro': 'Распродажа!', 'finale': 'Скин твой.', 'lesson': 'Процент — сотая часть числа.'}
    for i, key in enumerate(['s1', 's2', 's3'][:stages], 1):
        scene, question, kind, options, answer = QUEST[key]
        if messy and options:
            options = ', '.join(options)  # как бывает у настоящего GigaChat: варианты одной строкой
        args.update({f's{i}_scene': scene, f's{i}_question': question, f's{i}_kind': kind, f's{i}_options': options,
                     f's{i}_answer': answer, f's{i}_hint1': 'Подумай о процентах.', f's{i}_hint2': 'Посчитай по шагам.',
                     f's{i}_explain': 'Решение по шагам.', f's{i}_success': 'Получилось!'})
    return args


class FakeGiga:
    def __init__(self):
        self.down = False
        self.single = False  # как личный тариф: второй запрос, пока идёт первый, получает 429
        self.messy = False   # отвечать так же неаккуратно, как настоящая модель в логах
        self.busy = 0
        self.calls = []
        self.pause = 0.6  # между кусками потокового ответа

    async def oauth(self, request):
        return web.json_response({'access_token': 'fake', 'expires_at': int(time.time() * 1000) + 1800000})

    async def models(self, request):
        return web.json_response({'object': 'list', 'data': [
            {'id': m, 'object': 'model', 'owned_by': 'test'} for m in ('GigaChat-2', 'GigaChat-2-Max', 'GigaChat-2-Pro')]})

    async def chat(self, request):
        if self.single and self.busy:
            return web.json_response({'status': 429, 'message': 'Too many requests'}, status=429)
        self.busy += 1
        try:
            return await self.answer(request)
        finally:
            self.busy -= 1

    async def answer(self, request):
        data = await request.json()
        if self.down:
            return web.json_response({'status': 500, 'message': 'Internal error'}, status=500)
        user = data['messages'][-1]['content']
        props = (data.get('functions') or [{}])[0].get('parameters', {}).get('properties', {})
        self.calls.append(sorted(props) or ['text'])
        if 's1_scene' in props:
            await asyncio.sleep(3)  # квест собирается долго - на этом проверяем, что другие не ждут
            return self.call(quest_args(3 if 's3_scene' in props else 1, self.messy))
        if 'checks' in props:
            payload = json.loads(user if user.startswith('{') else user.split('\n', 1)[1])
            if 'stages' in payload:
                checks = [{'id': s['id'], 'valid': True, 'answer': ANSWERS[s['question']], 'reason': ''}
                          for s in payload['stages']]
            else:
                checks = [{'id': q['id'], 'valid': True, 'reason': '',
                           'answer': f"{q['options'].index('верно') + 1}) верно" if self.messy else 'верно'}
                          for q in payload['questions']]
            return self.call({'checks': checks})
        if 'q1_question' in props:
            count = sum(1 for key in props if key.endswith('_question'))
            args = {}
            for i in range(1, count + 1):
                args.update({f'q{i}_question': f'Вопрос {i}', f'q{i}_explanation': 'Потому что верно.',
                             f'q{i}_answer': 'верно', f'q{i}_wrong1': 'неверно 1', f'q{i}_wrong2': 'неверно 2',
                             f'q{i}_wrong3': 'неверно 3'})
                if self.messy:
                    # как в логах сервера: хвост кавычек и правильный ответ среди неверных
                    args.update({f'q{i}_answer': 'верно\\", ', f'q{i}_wrong1': 'верно'})
            return self.call(args)
        if 'kind' in props:
            return self.call({'allowed': True, 'kind': 'topic', 'topic': 'Проценты', 'subject': 'Математика',
                              'hook': 'Скидки — это проценты.', 'quests': [
                                  {'emoji': '🎮', 'title': 'Скин мечты', 'pitch': 'Хватит ли денег на скин?'},
                                  {'emoji': '⚽', 'title': 'Трансфер', 'pitch': 'Купи игрока со скидкой.'}]})
        if 'correct' in props:
            return self.call({'allowed': True, 'correct': '660' in user or 'шестьсот' in user})
        if 'score' in props:
            return self.call({'allowed': True, 'score': 85, 'praise': 'Верно.', 'fix': 'Добавь пример.',
                              'question': 'А если 10%?'})
        if data.get('stream'):
            return await self.stream(request, EXPLANATION)
        await asyncio.sleep(0.3)
        return self.text(EXPLANATION)

    async def stream(self, request, content):
        # как настоящий GigaChat при stream=true: события text/event-stream по несколько слов
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        words = content.split(' ')
        step = len(words) // 4 + 1
        try:
            for start in range(0, len(words), step):
                piece = ' '.join(words[start:start + step]) + (' ' if start + step < len(words) else '')
                chunk = {'choices': [{'delta': {'role': 'assistant', 'content': piece}, 'index': 0}],
                         'created': int(time.time()), 'model': 'GigaChat-2', 'object': 'chat.completion'}
                await response.write(f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'.encode())
                await asyncio.sleep(self.pause)
            await response.write(b'data: [DONE]\n\n')
            await response.write_eof()
        except ConnectionResetError:
            pass  # бота остановили посреди ответа
        return response

    def call(self, arguments):
        return web.json_response({'choices': [{'index': 0, 'finish_reason': 'function_call', 'message': {
            'role': 'assistant', 'content': '', 'function_call': {'name': 'submit_result', 'arguments': arguments}}}],
            'created': int(time.time()), 'model': 'GigaChat-2', 'object': 'chat.completion',
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})

    def text(self, content):
        return web.json_response({'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': content}}], 'created': int(time.time()), 'model': 'GigaChat-2',
            'object': 'chat.completion', 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})


def payloads(message):
    found = []
    for item in message.get('attachments') or []:
        for row in (item.get('payload') or {}).get('buttons') or []:
            found.append([b.get('payload') or b.get('url') for b in row])
    return found


class Scenario:
    def __init__(self, fake):
        self.max = fake
        self.failures = []

    def check(self, ok, what):
        print(('  ✅ ' if ok else '  ❌ ') + what, flush=True)
        if not ok:
            self.failures.append(what)

    async def wait(self, uid, predicate, timeout=12, since=0, first=False):
        # first=True: смотрим на первую версию сообщения ("Готовлю пример..."), а не на текущий текст
        chat = 1000 + uid
        end = time.time() + timeout
        while time.time() < end:
            for m in self.max.sent[since:]:
                text = m['shown'][0] if first else m['text']
                if m['chat'] == chat and not m.get('deleted') and predicate(text):
                    return m
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    async def settled(message, timeout=20):
        # ответ дописан, когда под ним появились кнопки
        end = time.time() + timeout
        while message and not message['attachments'] and time.time() < end:
            await asyncio.sleep(0.1)
        return bool(message and message['attachments'])

    async def wait_answer(self, predicate, since, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for a in self.max.answers[since:]:
                if predicate(a['text']):
                    return a
            await asyncio.sleep(0.1)
        return None

    async def run(self):
        m = self.max
        print('Знакомство')
        m.start(1)
        self.check(await self.wait(1, lambda t: 'Шаг 1 из 3' in t), 'приветствие и выбор класса')
        for payload, marker in (('group:89', 'Шаг 2 из 3'), ('interest:games', '✅'), ('interests:done', 'Шаг 3 из 3'),
                                ('nick:name', 'Готово')):
            n = len(m.answers)
            m.press(1, payload)
            self.check(await self.wait_answer(lambda t: marker in t or 'Выбрано' in t, n), f'{payload}: меню на месте')

        print('Тема: объяснение, формулы, предложение квеста, кнопки')
        n = len(m.sent)
        m.say(1, 'электромагнитная индукция')
        reply = await self.wait(1, lambda t: t.startswith('🧠 Думаю над ответом'), since=n, first=True)
        self.check(bool(reply), 'сразу «Думаю над ответом…»')
        self.check(await self.settled(reply), 'объяснение пришло')
        if reply:
            self.check(len(reply['shown']) >= 3, f"текст появляется по частям ({len(reply['shown'])} версии сообщения)")
            self.check(reply['text'].startswith('Электромагнитная индукция — это'), 'первая фраза — прямой ответ')
            self.check('ℰ = -dΦ/dt' in reply['text'] and '$' not in reply['text'], 'формулы без LaTeX')
            self.check('Проверим в деле' in reply['text'], 'в конце предложение квеста')
            rows = payloads(reply)
            self.check(rows[:1] == [['go:quest', 'own']], 'кнопка квеста первая')
            self.check(rows[-1:] == [['home']] and ['new', 'go:random'] not in rows, 'внизу одна кнопка «На главную»')

        print('Кнопки под ответом отвечают сразу, даже если Люми занята')
        n = len(m.sent)
        pressed = time.time()
        m.press(1, 'ai:example')
        example = await self.wait(1, lambda t: t.startswith('💡 Готовлю пример'), since=n, timeout=5, first=True)
        self.check(bool(example) and example['created'] - pressed < 2, 'после «Пример» сразу «Готовлю пример…»')
        m.press(1, 'ai:simple')
        m.say(1, '/me')
        queued = await self.wait(1, lambda t: 'следом будет объяснение попроще' in t, since=n, timeout=5)
        card = await self.wait(1, lambda t: '🦸' in t, since=n, timeout=5)
        self.check(await self.settled(example) and 'Электромагнитная' in example['text'], 'пример дописан в том же сообщении')
        self.check(bool(queued and example) and queued['at'] < example['at'], 'на второе нажатие: «следом будет объяснение попроще»')
        self.check(bool(card and example) and card['at'] < example['at'], '/me открылся, пока Люми дописывала пример')
        simple = await self.wait(1, lambda t: t.startswith('🧠 Объясняю проще'), since=n, timeout=10, first=True)
        self.check(await self.settled(simple), 'следом пришло объяснение попроще')

        print('Пока у одного собирается квест, другой не ждёт')
        n = len(m.sent)
        m.press(1, 'go:quest')
        await asyncio.sleep(0.3)
        m.start(2)
        other = await self.wait(2, lambda t: 'Шаг 1 из 3' in t, since=n, timeout=10)
        quest = await self.wait(1, lambda t: 'Этап 1 из 3' in t, since=n, timeout=30)
        self.check(bool(other and quest and other['at'] < quest['at']), 'второй пользователь получил ответ раньше')

        print('Проходим квест')
        m.say(1, '200')
        stage2 = await self.wait(1, lambda t: 'Этап 2 из 3' in t, since=n)
        self.check(bool(stage2), 'этап 1 засчитан')
        pick = next((p for row in payloads(stage2 or {}) for p in row if p and p.endswith(':pick:' + str(
            ['A', 'B', 'C'].index('B')))), None)
        options = re.findall(r'<b>([ABC])\)</b> ([^\n]+)', (stage2 or {}).get('text', ''))
        letter = next((l for l, o in options if 'Магазин Б' in o), 'B')
        m.say(1, letter)
        self.check(bool(await self.wait(1, lambda t: 'Этап 3 из 3' in t, since=n)), 'этап 2 (выбор) засчитан')
        m.say(1, 'шестьсот шестьдесят')
        self.check(bool(await self.wait(1, lambda t: 'Квест пройден' in t, since=n)), 'квест пройден, ответ словами принят')
        await asyncio.sleep(1)
        self.check(any(h.get('event') == 'quest_done' and h.get('user') == 1 for h in m.hooks), 'итог квеста ушёл на вебхук')
        del pick

        print('Задача дня одна на день, а после разбора нужна похожая')
        n = len(m.sent)
        m.say(1, '/daily')
        first = await self.wait(1, lambda t: 'Задача дня ·' in t, since=n, timeout=30)
        m.say(1, '/cancel')
        await self.wait(1, lambda t: 'Вышли' in t, since=n)
        n2 = len(m.sent)
        m.say(1, '/daily')
        second = await self.wait(1, lambda t: 'Задача дня ·' in t, since=n2, timeout=30)
        self.check(bool(first and second and first['text'] == second['text']), 'после выхода та же задача')
        stage = await self.wait(1, lambda t: '<b>Задание</b>' in t, since=n2)
        buttons = [b for row in payloads(stage or {}) for b in row if b]
        m.press(1, next((b for b in buttons if b.endswith(':hint')), ''))
        await self.wait(1, lambda t: 'Подсказка 1' in t, since=n2)
        m.press(1, next((b for b in buttons if b.endswith(':reveal')), ''))
        shown = await self.wait(1, lambda t: 'разобрали вместе' in t, since=n2)
        self.check(bool(shown) and ['daily:again'] in payloads(shown), 'после разбора не засчитана, есть «Похожая задача»')
        n3 = len(m.sent)
        m.press(1, 'daily:again')
        self.check(bool(await self.wait(1, lambda t: 'Похожая задача' in t, since=n3, timeout=30)), 'выдана похожая задача')
        await self.wait(1, lambda t: '<b>Задание</b>' in t, since=n3, timeout=30)
        m.say(1, '200')
        self.check(bool(await self.wait(1, lambda t: 'Задача дня решена' in t, since=n3)), 'похожую решил сам — засчитано')
        n4 = len(m.sent)
        m.say(1, '/daily')
        self.check(bool(await self.wait(1, lambda t: 'уже решена' in t, since=n4)), 'повторно не выдаётся')

        print('«Удиви меня» из запаса')
        await asyncio.sleep(8)  # запас пополняется фоном
        n = len(m.sent)
        started = time.time()
        m.say(1, '/random')
        got = await self.wait(1, lambda t: 'Этап 1 из 3' in t, since=n, timeout=30)
        self.check(bool(got) and got['at'] - started < 2, f'квест из запаса за {got["at"] - started:.1f} с' if got else 'квест не пришёл')
        m.say(1, '/cancel')

        print('Босс, «Учитель», герой, метрики')
        n = len(m.sent)
        m.say(1, '/boss')
        for i in range(3):
            q = await self.wait(1, lambda t, i=i: f'Удар {i + 1} из 3' in t, since=n)
            right = next((num for num, text in re.findall(r'([1-4])️⃣ ([^\n]+)', (q or {}).get('text', ''))
                          if text.strip() == 'верно'), '1')
            m.say(1, right)
        won = await self.wait(1, lambda t: 'Идеальная победа' in t, since=n)
        self.check(bool(won), 'босс побеждён')
        if not won:
            print('    ответы бота:', [x['text'][:120] for x in m.sent[n:] if x['chat'] == 1001])
        m.say(1, '/teach')
        await self.wait(1, lambda t: 'Режим «Учитель»' in t, since=n)
        m.say(1, 'Процент — это сотая часть')
        self.check(bool(await self.wait(1, lambda t: 'Понимание темы: 85%' in t, since=n)), 'оценка понимания')
        m.say(1, '/me')
        card = await self.wait(1, lambda t: 'Ур.' in t and '🦸' in t, since=n)
        self.check(bool(card), 'карточка героя')
        flat = [p for row in payloads(card or {}) for p in row]
        self.check('go:random' not in flat and 'go:daily' not in flat, 'у героя нет «Удиви меня» и «Задачи дня»')
        self.check('🔥' in (card or {}).get('text', ''), 'серия дней в карточке героя')
        k = len(m.answers)
        m.press(1, 'me:know')
        know = await self.wait_answer(lambda t: 'Мои темы' in t, k)
        self.check(bool(know) and 'Проценты' in know['text'], 'в «Моих темах» разобранная тема')
        link = next((p for row in payloads(card or {}) for p in row if p and p.startswith('http')), None)
        self.check(bool(link), 'ссылка на кабинет')
        if link:
            page = urllib.request.urlopen(link.replace('https://', 'http://'), timeout=5).read().decode()
            self.check('Ученик1' in page and 'Мои темы' in page, 'кабинет открывается')
            self.check('<i class="on">🔥</i>' in page and 'Серия за две недели' in page, 'на сайте серия дней')

        print('Главная и новая тема')
        n = len(m.sent)
        m.press(1, 'home')
        home = await self.wait(1, lambda t: 'Главная' in t, since=n, timeout=5)
        rows = payloads(home or {})
        self.check(rows[:3] == [['topic'], ['new', 'go:random'], ['go:daily', 'go:me']], 'на главной все разделы')
        self.check(any(a.get('type') == 'image' for a in (home or {}).get('attachments', [])), 'у главной есть картинка')
        m.press(1, 'new')
        ask = await self.wait(1, lambda t: 'Какую тему разберём' in t, since=n, timeout=5)
        self.check(bool(ask) and payloads(ask) == [['home']], '«Новая тема» спрашивает, что объяснить')
        m.say(1, '/stats')
        self.check(bool(await self.wait(1, lambda t: 'Метрики Люми' in t, since=n)), 'метрики для админа')
        health = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/health', timeout=5).read())
        self.check(health.get('ok') and health.get('gigachat') == 'ok', '/health')

    async def outage(self, giga):
        m = self.max
        print('GigaChat недоступен, потом вернулся')
        giga.down = True
        n = len(m.sent)
        m.say(1, 'что такое плотность?')
        self.check(bool(await self.wait(1, lambda t: 'не отвечает' in t, since=n, timeout=40)), 'честное сообщение о сбое')
        giga.down = False
        n = len(m.sent)
        m.say(1, 'что такое плотность?')
        again = await self.wait(1, lambda t: t.startswith('🧠'), since=n, first=True)
        self.check(await self.settled(again) and 'Электромагнитная' in again['text'], 'после восстановления снова отвечает')

    async def single_stream(self, giga):
        m = self.max
        print('GigaChat держит один запрос за раз и отвечает неаккуратно')
        giga.single = giga.messy = True
        n = len(m.sent)
        m.say(1, 'закон Ома')
        reply = await self.wait(1, lambda t: t.startswith('🧠'), since=n, first=True)
        self.check(await self.settled(reply, timeout=40) and 'Проверим в деле' in reply['text'],
                   'объяснение и предложение квеста пришли')
        n = len(m.sent)
        m.press(1, 'ai:example')
        example = await self.wait(1, lambda t: t.startswith('💡'), since=n, first=True)
        self.check(await self.settled(example, timeout=40), 'пример после этого тоже')
        n = len(m.sent)
        m.press(1, 'go:boss')
        first = await self.wait(1, lambda t: 'Удар 1 из 3' in t or 'сбежал' in t or 'не отвечает' in t,
                                since=n, timeout=90)
        self.check(bool(first) and 'Удар 1 из 3' in first['text'], 'после объяснения босс выходит на арену')
        if first and 'Удар' in first['text']:
            m.press(1, 'mode:stop')
            await self.wait(1, lambda t: 'Вышли' in t, since=n)
        n = len(m.sent)
        m.press(1, 'go:quest')
        quest = await self.wait(1, lambda t: 'Этап 1 из 3' in t or 'не отвечает' in t or 'Не получилось' in t,
                                since=n, timeout=90)
        self.check(bool(quest) and 'Этап 1 из 3' in quest['text'], 'и квест по теме собирается')
        if quest and 'Этап' in quest['text']:
            m.press(1, 'mode:stop')
            await self.wait(1, lambda t: 'Вышли' in t, since=n)
        giga.single = giga.messy = False

    async def after_restart(self):
        m = self.max
        print('После перезапуска')
        n = len(m.sent)
        m.say(1, '/me')
        card = await self.wait(1, lambda t: '🦸' in t, since=n, timeout=20)
        self.check(bool(card and 'Квестов: 1' in card['text'] and 'Ур. 1 ' not in card['text']), 'прогресс сохранился')
        m.say(1, '/daily')
        self.check(bool(await self.wait(1, lambda t: 'уже решена' in t, since=n)), 'задача дня помнит, что решена')


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker', help='образ, в котором запускать бота')
    parser.add_argument('--log', action='store_true', help='показать лог бота')
    args = parser.parse_args()
    fake_max, fake_giga = FakeMax(), FakeGiga()
    runners = []
    for port, routes in ((MAX_PORT, [web.route('*', '/{tail:.*}', fake_max.handler)]),
                         (GIGA_PORT, [web.post('/api/v2/oauth', fake_giga.oauth), web.get('/v1/models', fake_giga.models),
                                      web.post('/v1/chat/completions', fake_giga.chat)])):
        app = web.Application(client_max_size=50 * 1024 * 1024)  # картинки больше мегабайта
        app.add_routes(routes)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', port).start()
        runners.append(runner)
    key = base64.b64encode(b'11111111-2222-3333-4444-555555555555:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee').decode()
    env = {'MAX_BOT_TOKEN': 'test', 'MAX_API_URL': f'http://127.0.0.1:{MAX_PORT}', 'GIGACHAT_CREDENTIALS': key,
           'GIGACHAT_BASE_URL': f'http://127.0.0.1:{GIGA_PORT}/v1',
           'GIGACHAT_AUTH_URL': f'http://127.0.0.1:{GIGA_PORT}/api/v2/oauth', 'GIGACHAT_QUEST_MODEL': 'GigaChat-2-Max',
           'WEB_PORT': str(WEB_PORT), 'WEB_BASE_URL': f'http://127.0.0.1:{WEB_PORT}', 'ADMIN_IDS': '1',
           'POOL_TARGET': '1', 'LOG_LEVEL': 'WARNING', 'WEBHOOK_URLS': f'http://127.0.0.1:{MAX_PORT}/hook'}
    data = tempfile.mkdtemp()
    if args.docker:
        command = ['docker', 'run', '--rm', '--network', 'host', '--name', 'botmax-e2e']
        for name, value in env.items():
            command += ['-e', f'{name}={value}']
        command += [args.docker]
    else:
        env['DATABASE_PATH'] = str(Path(data) / 'bot.sqlite3')
        command = [sys.executable, 'main.py']
    if args.docker:
        volume = f'botmax-e2e-{os.getpid()}'
        command[-1:-1] = ['-v', f'{volume}:/app/data']
    clean = {k: v for k, v in os.environ.items() if 'PROXY' not in k.upper()}

    def launch():
        return subprocess.Popen(command, cwd=ROOT, env={**clean, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stop(process):
        if args.docker:
            subprocess.run(['docker', 'stop', 'botmax-e2e'], capture_output=True)
        process.terminate()
        return process.communicate(timeout=30)[0].decode(errors='replace')

    scenario = Scenario(fake_max)
    log = ''
    bot = launch()
    try:
        await asyncio.sleep(4)
        await asyncio.wait_for(scenario.run(), timeout=300)
        await asyncio.wait_for(scenario.outage(fake_giga), timeout=120)
        await asyncio.wait_for(scenario.single_stream(fake_giga), timeout=120)
        log += stop(bot)
        scenario.check('по одному' in log, 'бот заметил 429 и стал отправлять запросы по одному')
        bot = launch()
        await asyncio.sleep(4)
        await asyncio.wait_for(scenario.after_restart(), timeout=60)
    except asyncio.TimeoutError:
        scenario.check(False, 'сценарий не уложился по времени')
    finally:
        log += stop(bot)
        if args.docker:
            subprocess.run(['docker', 'volume', 'rm', '-f', volume], capture_output=True)
        for runner in runners:
            await runner.cleanup()
    errors = [line for line in log.splitlines() if ('ERROR' in line or 'Traceback' in line)
              and 'GigaChat' not in line]
    if scenario.failures or '--log' in sys.argv:
        print('\nХвост лога бота:\n' + '\n'.join(log.splitlines()[-40:]))
    scenario.check(not errors, 'в логе бота нет ошибок' + ('' if not errors else ':\n    ' + '\n    '.join(errors[:10])))
    print(f'\nИтого: {"всё в порядке" if not scenario.failures else "проблемы: " + str(len(scenario.failures))}')
    return 0 if not scenario.failures else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
