import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import game
import texts
from answers import AIError, FilterError
from func import AssistantBot, Store, chunks, rich
from storage import new_profile

QUEST = {
    'title': 'Скин мечты', 'emoji': '🎮', 'role': 'Ты — геймер с копилкой',
    'goal': 'Купить легендарный скин', 'intro': 'В игровом магазине распродажа!',
    'finale': 'Скин твой, и сдача осталась.', 'lesson': 'Процент — сотая часть числа.',
    'topic': 'Проценты',
    'stages': [
        {'kind': 'calc', 'scene': 'Скин стоит 800 рублей, скидка 25%.',
         'question': 'Сколько рублей составит скидка?', 'answer': '200 рублей', 'options': [],
         'hints': ['Найди четверть цены.', 'Раздели цену на 4.'],
         'success': 'Скидка поймана!', 'explain': '800 × 0,25 = 200 рублей.'},
        {'kind': 'choice', 'scene': 'Два магазина: в А скин за 650, в Б — за 600.',
         'question': 'Где выгоднее?', 'options': ['Магазин А', 'Магазин Б', 'Одинаково'],
         'correct': 1, 'answer': 'Магазин Б', 'hints': ['Сравни цены.', 'Меньшая цена выгоднее.'],
         'success': 'Ты сэкономил!', 'explain': '600 меньше 650.'},
        {'kind': 'calc', 'scene': 'На покупку начисляют налог 10% к цене 600 рублей.',
         'question': 'Сколько рублей заплатишь итого?', 'answer': '660 рублей', 'options': [],
         'hints': ['Найди десятую часть цены.', 'Прибавь её к цене.'],
         'success': 'Денег хватило!', 'explain': '600 + 60 = 660 рублей.'},
    ]}

DAILY = {**copy.deepcopy(QUEST), 'stages': copy.deepcopy(QUEST['stages'][:1]), 'topic': 'Дроби'}

CARD = {'kind': 'topic', 'topic': 'Проценты', 'subject': 'Математика',
        'hook': 'Скидки в игровых магазинах — это проценты.',
        'quests': [{'emoji': '🎮', 'title': 'Скин мечты', 'pitch': 'Хватит ли денег на скин?'},
                   {'emoji': '⚽', 'title': 'Трансфер', 'pitch': 'Купи игрока со скидкой.'},
                   {'emoji': '📹', 'title': 'Блогер', 'pitch': 'Посчитай рост подписчиков.'}]}

BOSS = [{'question': f'Вопрос {i}', 'options': ['а', 'б', 'в', 'г'], 'correct': 1,
         'answer': 'б', 'explanation': 'Потому что б.'} for i in range(3)]


def as_stream(mock):
    # бот ждёт ответ частями; в тестах отдаём его одним куском из обычного мока
    async def stream(*args, **kwargs):
        yield await mock(*args, **kwargs)
    return stream


def make_quest(*args, **kwargs):
    return copy.deepcopy(QUEST if args[3] == 3 else DAILY)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'bot.sqlite3')
        self.store = Store(self.path)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.ai = SimpleNamespace(
            explain=AsyncMock(return_value='Объяснение процентов'),
            topic_card=AsyncMock(return_value=copy.deepcopy(CARD)),
            create_quest=AsyncMock(side_effect=make_quest),
            check_answer=AsyncMock(return_value={'correct': True}),
            applications=AsyncMock(return_value='Проценты нужны в магазинах.'),
            analyze_text=AsyncMock(return_value='Простыми словами: найди скидку.'),
            analyze_image=AsyncMock(return_value='На фото: задача про скидку.'),
            quiz=AsyncMock(return_value=copy.deepcopy(BOSS)),
            teach=AsyncMock(return_value={'score': 95, 'praise': 'Суть верна.', 'fix': 'Добавь пример.',
                                          'question': 'А если скидка 50%?'}),
        )
        self.ai.explain_stream = as_stream(self.ai.explain)
        self.ai.applications_stream = as_stream(self.ai.applications)
        self.app = self.make_app()
        self.profile = self.ready_profile(1)

    def make_app(self):
        app = AssistantBot(self.bot, self.store, self.ai)
        app.pool_target = 0
        return app

    def ready_profile(self, uid, **extra):
        p = self.store.get(uid)
        p.update(group='8–9 класс', interests=['Игры'], interests_done=True, nick='Капитан Икс', nick_done=True,
                 onboarded=True, state='ready', topic='Проценты', uid=uid, history=[
                     {'role': 'user', 'content': 'Объясни проценты'},
                     {'role': 'assistant', 'content': 'Процент — сотая часть.'}])
        p.update(extra)
        return p

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def sent_text(self):
        return '\n'.join(call.kwargs.get('text', '') for call in self.bot.send_message.call_args_list)

    def last_text(self):
        return self.bot.send_message.call_args.kwargs['text']

    def message_event(self, text='', attachments=None, uid=1):
        return SimpleNamespace(get_ids=lambda: (10, uid), message=SimpleNamespace(
            recipient=SimpleNamespace(chat_type='dialog'), sender=SimpleNamespace(first_name='Иван', last_name='Петров'),
            body=SimpleNamespace(text=text, attachments=attachments or [])))

    def callback_event(self, payload, uid=1):
        return SimpleNamespace(get_ids=lambda: (10, uid), message=None, answer=AsyncMock(),
                               callback=SimpleNamespace(payload=payload, user=SimpleNamespace(first_name='Иван', last_name='')))

    def payloads(self, call=None):
        call = call or self.bot.send_message.call_args
        result = []
        for item in call.kwargs.get('attachments') or []:
            if hasattr(item, 'payload') and hasattr(item.payload, 'buttons'):
                for row in item.payload.buttons:
                    result += [getattr(b, 'payload', None) or getattr(b, 'url', None) for b in row]
        return result

    async def start_quest(self):
        await self.app.text(10, self.profile, '/quest')
        self.assertEqual(self.profile['state'], 'quest')
        return self.profile['quest']


class OnboardingTests(FlowTests):
    async def test_full_onboarding_with_custom_interest_and_nick(self):
        p = self.store.get(2)
        p['name'] = 'Мария Иванова'
        await self.app.text(10, p, '/start')
        self.assertEqual(p['state'], 'group')
        self.assertIn('group:14', self.payloads())
        self.assertIn('group:adult', self.payloads())
        await self.app.callback(10, p, 'group:89')
        self.assertEqual(p['state'], 'interests')
        await self.app.callback(10, p, 'interest:games')
        await self.app.text(10, p, 'Minecraft')
        await self.app.callback(10, p, 'interest:games')
        self.assertEqual(p['interests'], ['Minecraft'])
        await self.app.callback(10, p, 'interests:done')
        self.assertEqual(p['state'], 'nick')
        self.assertIn('nick:name', self.payloads())
        await self.app.callback(10, p, 'nick:name')
        self.assertEqual(p['nick'], 'Мария')
        self.assertEqual(p['state'], 'ready')
        self.assertTrue(p['onboarded'])
        self.assertIn('go:random', self.payloads())

    async def test_hero_tabs_switch_in_the_same_message(self):
        p = self.ready_profile(1)
        game.record_check(p, 'Дроби', 0)
        game.record_check(p, 'Дроби', 0.5)
        self.store.save(1, p)
        self.app.images['profile'] = 'profile-image'
        event = self.callback_event('me:know')
        await self.app.handle(event, 'callback')
        self.bot.send_message.assert_not_awaited()
        kwargs = event.answer.call_args.kwargs
        self.assertIn('Мои темы', kwargs['new_text'])
        self.assertIn('стоит повторить', kwargs['new_text'])
        self.assertIn('Дроби', kwargs['new_text'])
        self.assertEqual(kwargs['attachments'][0], 'profile-image')
        self.assertIn("payload='train'", str(kwargs['attachments'][1]))
        event = self.callback_event('train')
        await self.app.handle(event, 'callback')
        self.assertEqual(self.store.get(1)['quest']['mode'], 'practice')
        self.assertEqual(self.ai.create_quest.call_args.args[1], 'Дроби')

    async def test_menu_is_edited_in_place_on_button_press(self):
        p = self.store.get(1)
        p.update(group='5–7 класс', state='interests')
        self.store.save(1, p)
        event = self.callback_event('interest:sport')
        await self.app.handle(event, 'callback')
        event.answer.assert_awaited_once()
        self.assertIn('✅ Спорт', str(event.answer.call_args.kwargs['attachments']))
        self.assertIn('Выбрано', event.answer.call_args.kwargs['new_text'])
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.store.get(1)['interests'], ['Спорт'])
        self.assertEqual(self.store.get(1)['name'], 'Иван')

    async def test_nick_ideas_are_simple_words_and_refreshable(self):
        p = self.store.get(2)
        p.update(group='5–7 класс', interests_done=True, state='nick')
        await self.app.setup(10, p)
        for idea in p['nick_ideas']:
            self.assertIn(idea, texts.NICKS)
            self.assertNotIn(' ', idea)
        await self.app.callback(10, p, 'nick:more')
        self.assertEqual(p['state'], 'nick')
        await self.app.text(10, p, 'x' * 30)
        self.assertFalse(p['nick_done'])
        await self.app.text(10, p, '<b>Нео</b>')
        self.assertEqual(p['nick'], 'bНео/b')

    async def test_teacher_onboarding_skips_interests(self):
        p = self.store.get(3)
        p['state'] = 'group'
        await self.app.callback(10, p, 'group:teacher')
        self.assertEqual((p['role'], p['group'], p['state']), ('teacher', 'Учитель', 'nick'))
        await self.app.text(10, p, 'Анна Сергеевна')
        self.assertTrue(p['onboarded'])
        self.assertIn('go:class', self.payloads())

    async def test_old_profile_is_migrated(self):
        old = {'group': '10+ класс', 'interests': ['Игры'], 'interests_done': True, 'filters': '',
               'state': 'practice', 'history': [], 'quiz': {'version': 2}, 'practice': {'id': 'x'},
               'game': {'xp': 120, 'badges': ['first_quest'], 'stats': {'quests': 1}}}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO users VALUES (?, ?)', (5, json.dumps(old, ensure_ascii=False)))
            db.commit()
        p = self.store.get(5)
        self.assertEqual(p['group'], '10–11 класс')
        self.assertEqual(p['state'], 'ready')
        self.assertNotIn('practice', p)
        self.assertEqual(p['game']['badges'], {})
        self.assertEqual(p['game']['xp'], 120)
        self.assertEqual(p['game']['difficulty'], 2)
        await self.app.text(10, p, 'Проценты')
        self.assertEqual(p['state'], 'nick')

    async def test_deep_links_wait_for_onboarding(self):
        code = self.store.save_quest(copy.deepcopy(QUEST), author=99)
        p = self.store.get(2)
        p['uid'] = 2
        await self.app.start(10, p, code.lower())
        self.assertEqual(p['pending_play'], code)
        await self.app.callback(10, p, 'group:57')
        await self.app.callback(10, p, 'interests:done')
        await self.app.callback(10, p, 'nick:skip')
        self.assertEqual(p['state'], 'quest')
        self.assertTrue(p['quest']['shared'])


class TopicAndChatTests(FlowTests):
    async def test_new_topic_explanation_then_quest_from_offered_ideas(self):
        self.profile.update(history=[], topic='')
        await self.app.text(10, self.profile, 'проценты')
        self.assertIn('Думаю над ответом', self.sent_text())
        text = self.last_text()
        self.assertTrue(text.startswith('Объяснение процентов'))
        self.assertIn('Проверим в деле', text)
        self.assertIn('Скин мечты', text)
        self.assertEqual(self.profile['topic'], 'Проценты')
        await self.app.callback(10, self.profile, 'go:quest')
        self.assertIn('Хватит ли денег на скин?', self.ai.create_quest.call_args.args[2])
        self.assertEqual(self.profile['state'], 'quest')
        self.assertIn('Скин стоит 800 рублей', self.sent_text())
        await self.app.callback(10, self.profile, 'mode:stop')
        await self.app.callback(10, self.profile, 'go:quest')
        self.assertIn('Купи игрока со скидкой', self.ai.create_quest.call_args.args[2])

    async def test_own_setting_button(self):
        await self.app.callback(10, self.profile, 'own')
        self.assertEqual(self.profile['state'], 'quest_setting')
        await self.app.text(10, self.profile, 'я открываю кофейню')
        self.assertEqual(self.ai.create_quest.call_args.args[2], 'я открываю кофейню')

    async def test_buttons_keep_their_places(self):
        def layout(call):
            rows = []
            for item in call.kwargs.get('attachments') or []:
                if hasattr(item, 'payload') and hasattr(item.payload, 'buttons'):
                    rows = [[getattr(b, 'payload', None) or 'link' for b in row] for row in item.payload.buttons]
            return rows
        self.app.web_url = 'https://lumi.example'
        await self.app.text(10, self.profile, 'проценты')
        after_topic = layout(self.bot.send_message.call_args)
        await self.app.text(10, self.profile, '/boss')
        for answer in ('2', '2', '2'):
            await self.app.text(10, self.profile, answer)
        await self.app.text(10, self.profile, '/quest')
        for answer in ('200', 'B', '660'):
            await self.app.text(10, self.profile, answer)
        after_quest = layout(self.bot.send_message.call_args)
        self.assertEqual(after_topic, after_quest)
        self.assertEqual(after_topic, [['go:quest', 'own'], ['ai:simple', 'ai:deeper'], ['ai:example', 'go:why'],
                                       ['go:boss', 'go:teach'], ['home']])
        await self.app.callback(10, self.profile, 'home')
        home = layout(self.bot.send_message.call_args)
        self.assertIn(['new', 'go:random'], home)
        self.assertIn(['go:daily', 'go:me'], home)
        self.assertEqual(home[0], ['topic'])
        self.assertIn('Главная', self.last_text())

    async def test_new_topic_asks_what_to_explain(self):
        await self.app.callback(10, self.profile, 'new')
        self.assertIn('Какую тему разберём', self.last_text())
        self.assertEqual(self.payloads(), ['home'])
        self.assertEqual(self.profile['topic'], '')

    async def test_question_goes_to_explanation_with_html_escaped(self):
        self.ai.topic_card.return_value = {'kind': 'question', 'topic': 'Скидки', 'subject': '',
                                           'hook': '', 'quests': []}
        self.ai.explain.return_value = '<script> **Главное:** 25% — четверть'
        await self.app.text(10, self.profile, 'Как найти 25%?')
        text = self.last_text()
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('<b>Главное:</b>', text)
        self.assertEqual(self.profile['topic'], 'Скидки')
        self.assertEqual(self.bot.send_message.call_args.kwargs['format'], 'html')
        self.assertIn('ai:deeper', self.payloads())
        events = self.store.query("SELECT kind FROM events WHERE user=1")
        self.assertIn(('explain',), events)

    async def test_router_failure_falls_back_to_explanation_but_filter_is_reported(self):
        self.ai.topic_card.side_effect = AIError('broken')
        await self.app.text(10, self.profile, 'Что-то')
        self.ai.explain.assert_awaited_once()
        self.ai.topic_card.side_effect = FilterError('🌿 Эта тема попала в твой фильтр.')
        self.store.save(1, self.profile)
        await self.app.handle(self.message_event('Ужасы'), 'text')
        self.assertIn('фильтр', self.last_text())

    async def test_ai_failure_preserves_persisted_state(self):
        self.store.save(1, self.profile)
        self.ai.topic_card.side_effect = AIError('timeout')
        self.ai.explain.side_effect = AIError('timeout')
        await self.app.handle(self.message_event('Тема'), 'text')
        self.assertEqual(self.store.get(1)['history'], self.profile['history'])
        self.assertEqual(self.app.busy, set())
        self.assertIn('не отвечает', self.last_text())

    async def test_messages_from_one_user_are_queued_not_dropped(self):
        import asyncio
        self.store.save(1, self.profile)
        order = []

        async def slow_explain(profile, history):
            order.append(history[-1]['content'])
            await asyncio.sleep(0.05)
            return 'Ответ'
        self.ai.explain.side_effect = slow_explain
        self.ai.topic_card.return_value = {'kind': 'question', 'topic': 'Проценты', 'subject': '', 'hook': '', 'quests': []}
        await asyncio.gather(self.app.handle(self.message_event('первый вопрос'), 'text'),
                             self.app.handle(self.message_event('второй вопрос'), 'text'))
        self.assertEqual(order, ['первый вопрос', 'второй вопрос'])
        self.assertIn('⏳', self.sent_text())
        self.assertEqual(self.app.busy, set())

    async def test_closed_chat_is_ignored_without_saving(self):
        from maxapi.exceptions.max import MaxApiError
        self.store.save(1, self.profile)
        self.bot.send_message.side_effect = MaxApiError(code=403, raw={'code': 'chat.denied'})
        with self.assertLogs('func', level='INFO') as logs:
            await self.app.handle(self.message_event('/random'), 'text')
        self.assertIn('закрыт пользователем', '\n'.join(logs.output))
        self.ai.create_quest.assert_not_awaited()
        self.assertEqual(self.store.get(1)['state'], 'ready')
        self.assertEqual(self.app.busy, set())

    async def test_why_and_analyze(self):
        await self.app.text(10, self.profile, '/life')
        self.ai.applications.assert_awaited_once()
        self.assertIn('go:quest', self.payloads())
        await self.app.text(10, self.profile, '/analyze')
        self.assertEqual(self.profile['state'], 'analyze_input')
        await self.app.text(10, self.profile, 'Задача про скидку')
        self.assertEqual(self.profile['history'][0]['content'], 'Задача про скидку')

    async def test_filter_hero_card_badges_and_difficulty(self):
        await self.app.text(10, self.profile, '/filter без ужасов')
        self.assertEqual(self.profile['filters'], 'без ужасов')
        await self.app.text(10, self.profile, '/profile')
        self.assertIn('Капитан Икс', self.last_text())
        self.assertIn('Продвинутый', self.last_text())
        self.assertIn('me:know', self.payloads())
        await self.app.callback(10, self.profile, 'me:settings')
        self.assertIn('без ужасов', self.last_text())
        await self.app.callback(10, self.profile, 'me:badges')
        self.assertIn('🔒', self.last_text())
        self.assertIn('0/3', self.last_text())
        await self.app.callback(10, self.profile, 'me:level')
        await self.app.callback(10, self.profile, 'level:3')
        self.assertEqual(self.profile['game']['difficulty'], 3)
        self.assertIn('Профи', self.last_text())

    async def test_command_menu_order_images_and_unknown_command(self):
        self.bot.set_commands = AsyncMock(return_value=SimpleNamespace(commands=[1] * 16))
        self.bot.upload_media = AsyncMock(return_value='image-attachment')
        self.bot.get_me = AsyncMock(return_value=SimpleNamespace(username='lumi_bot'))
        await self.app.prepare()
        names = [c.name for c in self.bot.set_commands.call_args.args]
        self.assertEqual(names[:3], ['start', 'help', 'menu'])
        for name in ('quest', 'boss', 'teach', 'daily', 'play', 'class'):
            self.assertIn(name, names)
        self.assertLessEqual(len(names), 32)  # больше MAX не принимает
        self.assertIn('levelup', self.app.images)
        self.assertEqual(self.app.username, 'lumi_bot')
        await self.app.text(10, self.profile, '/quiz')
        self.assertEqual(self.profile['state'], 'boss')
        await self.app.text(10, self.profile, '/cancel')
        await self.app.text(10, self.profile, '/whatever')
        self.assertIn('/help', self.last_text())
        await self.app.text(10, self.profile, '/stats')
        self.assertIn('/help', self.last_text())

    async def test_cabinet_link_uses_signed_token(self):
        import web
        self.app.web_url = 'https://lumi.example'
        await self.app.text(10, self.profile, '/me')
        url = next(u for u in self.payloads() if u and u.startswith('https://'))
        self.assertTrue(url.startswith('https://lumi.example/app?t='))
        self.assertEqual(web.read_token(self.store.secret(), url.split('t=')[1]), (1, 's'))


class QuestTests(FlowTests):
    async def test_full_quest_with_hints_choice_side_question_and_rewards(self):
        run = await self.start_quest()
        self.assertNotIn('200 рублей', self.sent_text())
        self.bot.send_message.reset_mock()
        await self.app.text(10, self.profile, '300')
        self.assertIn('Найди четверть цены.', self.sent_text())
        self.ai.check_answer.assert_not_awaited()
        await self.app.text(10, self.profile, 'было 800, скидка 200 руб')
        self.assertEqual((run['stage'], run['stars']), (1, [2]))
        await self.app.callback(10, self.profile, f"quest:{run['id']}:1:pick:0")
        self.assertIn(0, run['removed'])
        await self.app.text(10, self.profile, 'b')
        self.assertEqual(run['stage'], 2)
        await self.app.text(10, self.profile, 'а что такое налог?')
        self.assertEqual(run['tries'], 0)
        await self.app.text(10, self.profile, 'шестьсот шестьдесят')
        self.ai.check_answer.assert_awaited_once()
        self.assertEqual(self.profile['state'], 'ready')
        text = self.sent_text()
        self.assertIn('Квест пройден', text)
        self.assertIn('Новый уровень 2', text)
        self.assertIn('/play ', text)
        g = self.profile['game']
        self.assertEqual((g['stats']['quests'], g['stats']['stages']), (1, 3))
        self.assertGreater(g['xp'], 80)
        mastery = g['topics']['проценты']
        self.assertEqual((mastery['n'], mastery['quests']), (3, 1))
        kinds = [k for (k,) in self.store.query('SELECT kind FROM events WHERE user=1')]
        for kind in ('quest_start', 'stage', 'quest_done', 'xp'):
            self.assertIn(kind, kinds)

    async def test_finished_quest_goes_to_webhooks(self):
        sent = []
        self.app.hooks = SimpleNamespace(emit=lambda kind, p, topic='', value=None: sent.append((kind, topic)))
        await self.start_quest()
        for answer in ('200', 'B', '660'):
            await self.app.text(10, self.profile, answer)
        self.assertIn(('quest_done', 'Проценты'), sent)

    async def test_reveal_requires_attempt_and_gives_zero_stars(self):
        run = await self.start_quest()
        await self.app.callback(10, self.profile, f"quest:{run['id']}:0:reveal")
        self.assertEqual(run['stage'], 0)
        await self.app.callback(10, self.profile, f"quest:{run['id']}:0:hint")
        await self.app.callback(10, self.profile, f"quest:{run['id']}:0:reveal")
        self.assertEqual(run['stars'], [0])
        self.assertIn('800 × 0,25', self.sent_text())

    async def test_stale_buttons_and_blocked_commands(self):
        run = await self.start_quest()
        before = copy.deepcopy(self.profile)
        await self.app.callback(10, self.profile, f"quest:{run['id']}:2:hint")
        await self.app.callback(10, self.profile, 'quest:old:0:hint')
        self.assertEqual(self.profile, before)
        await self.app.text(10, self.profile, '/boss')
        self.assertEqual(self.profile['state'], 'quest')
        self.assertIn('resume', self.payloads())
        await self.app.callback(10, self.profile, 'mode:stop')
        self.assertEqual(self.profile['state'], 'ready')

    async def test_shared_quest_by_code(self):
        await self.start_quest()
        code = self.profile['quest']['code']
        friend = self.ready_profile(2)
        await self.app.text(10, friend, f'/play {code.lower()}')
        self.assertTrue(friend['quest']['shared'])
        self.assertEqual(self.store.author_plays(1), 1)
        for answer in ('200', 'B', '660'):
            await self.app.text(10, friend, answer)
        self.assertEqual(friend['game']['stats']['shared'], 1)
        self.assertNotIn('Командный игрок I', self.sent_text())  # теперь нужно три квеста друзей

    async def test_random_quest_goes_to_pool_and_next_user_gets_it_instantly(self):
        await self.app.text(10, self.profile, '/random')
        self.assertEqual(self.ai.create_quest.await_count, 1)
        code = self.profile['quest']['code']
        friend = self.ready_profile(2)
        await self.app.text(10, friend, '/random')
        self.assertEqual(self.ai.create_quest.await_count, 1)
        self.assertEqual(friend['quest']['code'], code)
        # этот квест из запаса он уже проходил, значит собираем новый
        await self.app.text(10, self.profile, '/cancel')
        await self.app.text(10, self.profile, '/random')
        self.assertEqual(self.ai.create_quest.await_count, 2)

    async def test_pool_refill_runs_in_background(self):
        self.app.pool_target = 2
        await self.app.text(10, self.profile, '/random')
        for task in list(self.app.background):
            await task
        # после выдачи держим в запасе три непройденных квеста
        self.assertEqual(self.ai.create_quest.await_count, 4)
        self.assertEqual(self.store.pool_size('8–9 класс', 2, exclude=self.profile['played']), 3)

    async def test_offline_quest_when_ai_is_down(self):
        self.ai.create_quest.side_effect = AIError('down')
        await self.app.text(10, self.profile, '/random')
        self.assertEqual(self.profile['state'], 'quest')
        self.assertIn('из моей коллекции', self.sent_text())
        self.assertIn('Зарядка электросамоката', self.sent_text())
        # квест по конкретной теме из коллекции не подменяем
        await self.app.text(10, self.profile, '/cancel')
        self.store.save(1, self.profile)
        await self.app.handle(self.message_event('/quest логарифмы'), 'text')
        self.assertIn('не отвечает', self.last_text())

    async def test_progress_message_is_updated_and_removed(self):
        sent = SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid='mid-1')))
        self.bot.send_message = AsyncMock(return_value=sent)
        self.bot.edit_message = AsyncMock()
        self.bot.delete_message = AsyncMock()

        async def slow(*args, **kwargs):
            import asyncio
            await asyncio.sleep(0.05)
            return copy.deepcopy(QUEST)
        self.ai.create_quest.side_effect = slow
        with patch.object(texts, 'QUEST_STEPS', ['шаг 1', 'шаг 2', 'шаг 3']):
            app = self.make_app()
            async with app.progress(10, ['шаг 1', 'шаг 2'], interval=0.01):
                import asyncio
                await asyncio.sleep(0.05)
        self.bot.edit_message.assert_awaited()
        self.bot.delete_message.assert_awaited_once_with(message_id='mid-1')

    async def test_difficulty_rises_after_three_perfect_quests(self):
        for _ in range(3):
            await self.app.text(10, self.profile, '/quest')
            for answer in ('200', 'B', '660'):
                await self.app.text(10, self.profile, answer)
        self.assertEqual(self.profile['game']['difficulty'], 3)
        self.assertIn('поднимаю сложность', self.sent_text())


class SpeedTests(FlowTests):
    async def test_explanation_and_card_requests_run_together(self):
        self.profile.update(history=[], topic='')
        await self.app.text(10, self.profile, 'электромагнитная индукция')
        self.ai.explain.assert_awaited_once()
        self.ai.topic_card.assert_awaited_once()
        self.assertEqual(self.profile['history'][-1]['content'], 'Объяснение процентов')
        self.assertIn('ai:deeper', self.payloads())

    async def test_question_without_card_gets_full_buttons(self):
        self.ai.topic_card.return_value = {'kind': 'question', 'topic': 'Проценты', 'subject': '', 'hook': '', 'quests': []}
        await self.app.text(10, self.profile, 'а почему так?')
        self.assertNotIn('Проверим в деле', self.last_text())
        self.assertIn('go:quest', self.payloads())

    async def test_pool_falls_back_to_neighbour_level(self):
        self.store.save_quest(copy.deepcopy(QUEST), None, '8–9 класс', 3, 'ai', pool=1)
        await self.app.text(10, self.profile, '/random')
        self.ai.create_quest.assert_not_awaited()
        self.assertEqual(self.profile['state'], 'quest')

    async def test_warm_up_prepares_quests_for_active_users(self):
        self.app.pool_target = 1
        self.store.save(1, self.profile)
        self.store.log(1, 'explain', 'Проценты')
        self.app.warm_up()
        for task in list(self.app.background):
            await task
        self.assertEqual(self.store.pool_size('8–9 класс', 2, stages=3), 3)
        self.assertTrue(self.store.daily_get(1, game.today().isoformat()))

    async def test_tomorrow_review_is_prepared_in_advance(self):
        self.app.pool_target = 1
        g = game.ensure(self.profile)
        g['topics']['дроби'] = {'name': 'Дроби', 'n': 4, 'score': 1.0, 'quests': 1}
        with patch('game.today', return_value=date(2026, 11, 5)):
            await self.app.text(10, self.profile, '/daily')
            await self.app.text(10, self.profile, '200')
        for task in list(self.app.background):
            await task
        topics = [call.args[1] for call in self.ai.create_quest.call_args_list]
        self.assertIn('Дроби', topics)
        calls = self.ai.create_quest.await_count
        with patch('game.today', return_value=date(2026, 11, 6)):
            await self.app.text(10, self.profile, '/daily')
        self.assertIn('Сегодня повторяем', self.sent_text())
        self.assertEqual(self.ai.create_quest.await_count, calls)

    async def test_daily_is_one_per_day_even_after_exit(self):
        with patch('game.today', return_value=date(2026, 11, 5)):
            await self.app.text(10, self.profile, '/daily')
            code = self.profile['quest']['code']
            await self.app.text(10, self.profile, '/cancel')
            await self.app.text(10, self.profile, '/daily')
            self.assertEqual(self.profile['quest']['code'], code)
            self.assertEqual(self.ai.create_quest.await_count, 1)

    async def test_daily_prepared_in_background_opens_instantly(self):
        self.app.pool_target = 1
        self.store.save(1, self.profile)
        with patch('game.today', return_value=date(2026, 11, 5)):
            await self.app.start(10, self.profile)
            for task in list(self.app.background):
                await task
            calls = self.ai.create_quest.await_count
            await self.app.text(10, self.profile, '/daily')
        self.assertEqual(self.ai.create_quest.await_count, calls)
        self.assertEqual(self.profile['quest']['mode'], 'daily')


class StreamingTests(FlowTests):
    def live_bot(self):
        # бот, у которого есть id сообщений: тогда ответ правится на месте, как в настоящем MAX
        sent = SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid='mid-1')))
        self.bot.send_message = AsyncMock(return_value=sent)
        self.bot.edit_message = AsyncMock()
        self.bot.delete_message = AsyncMock()

    async def test_answer_is_written_into_placeholder(self):
        self.live_bot()

        async def chunks(profile, history):
            for part in ('Проценты', 'Проценты — это сотые', 'Проценты — это сотые доли числа.'):
                yield part
        self.ai.explain_stream = chunks
        await self.app.callback(10, self.profile, 'ai:example')
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn('Готовлю пример', self.bot.send_message.call_args.kwargs['text'])
        last = self.bot.edit_message.call_args.kwargs
        self.assertEqual(last['message_id'], 'mid-1')
        self.assertIn('сотые доли числа', last['text'])
        self.assertIn('go:quest', str(last['attachments']))
        self.bot.delete_message.assert_not_awaited()

    async def test_explanation_does_not_wait_for_quest_ideas(self):
        # у личного тарифа GigaChat один поток: объяснение должно уйти первым
        order = []

        async def chunks(profile, history):
            order.append('объяснение')
            yield 'Проценты — это сотые доли числа.'

        async def card(*args):
            order.append('сюжеты')
            return copy.deepcopy(CARD)
        self.ai.explain_stream = chunks
        self.ai.topic_card.side_effect = card
        self.store.save(1, self.profile)
        await self.app.handle(self.message_event('проценты'), 'text')
        self.assertEqual(order, ['объяснение', 'сюжеты'])
        self.assertIn('Проверим в деле', self.sent_text())

    async def test_failed_answer_removes_placeholder(self):
        self.live_bot()
        self.ai.explain.side_effect = AIError('timeout')
        self.store.save(1, self.profile)
        await self.app.handle(self.message_event('что такое плотность?'), 'text')
        self.bot.delete_message.assert_awaited_once_with(message_id='mid-1')
        self.assertIn('не отвечает', self.bot.send_message.call_args.kwargs['text'])

    async def test_me_during_setup_keeps_nick_choice(self):
        # пока человек выбирает позывной, /me не должен подменить варианты на кнопках
        fresh = new_profile()
        fresh.update(group='8–9 класс', interests_done=True, state='nick')
        self.store.save(1, fresh)
        await self.app.handle(self.message_event('/me'), 'text')
        rows = self.bot.send_message.call_args.kwargs['attachments'][0].payload.buttons
        self.assertIn('🦸 ' + self.store.get(1)['nick_ideas'][0], [b.text for row in rows for b in row])

    async def test_menu_command_shows_home(self):
        self.profile['topic'] = 'Проценты'
        await self.app.text(10, self.profile, '/menu')
        self.assertIn('Главная', self.last_text())
        self.assertIn('Сейчас разбираем: «Проценты»', self.last_text())
        rows = self.bot.send_message.call_args.kwargs['attachments'][0].payload.buttons
        self.assertEqual([b.payload for b in rows[0]], ['topic'])
        await self.app.text(10, self.profile, '/home')
        self.assertIn('Главная', self.last_text())

    async def test_hero_card_opens_while_bot_is_busy(self):
        import asyncio
        self.store.save(1, self.profile)
        release = asyncio.Event()

        async def slow(profile, history):
            await release.wait()
            return 'Длинное объяснение'
        self.ai.explain.side_effect = slow
        self.ai.topic_card.return_value = {'kind': 'question', 'topic': 'Проценты', 'subject': '', 'hook': '', 'quests': []}
        busy = asyncio.create_task(self.app.handle(self.message_event('что такое процент?'), 'text'))
        await asyncio.sleep(0.05)
        await self.app.handle(self.message_event('/me'), 'text')
        self.assertIn('🦸', self.last_text())
        await self.app.handle(self.message_event('/menu'), 'text')
        self.assertIn('Главная', self.last_text())
        waiting = self.callback_event('ai:example')
        task = asyncio.create_task(self.app.handle(waiting, 'callback'))
        await asyncio.sleep(0.05)
        self.assertIn('следом будет пример', self.last_text())
        release.set()
        await asyncio.gather(busy, task)
        self.assertIn('Длинное объяснение', self.sent_text())


class DailyBossTeachTests(FlowTests):
    async def test_daily_once_per_day_with_streak(self):
        with patch('game.today', return_value=date(2026, 11, 5)):
            await self.app.text(10, self.profile, '/daily')
            self.assertEqual(self.profile['quest']['mode'], 'daily')
            await self.app.text(10, self.profile, '200')
            self.assertEqual(self.profile['game']['daily_day'], '2026-11-05')
            calls = self.ai.create_quest.await_count
            await self.app.text(10, self.profile, '/daily')
            self.assertEqual(self.ai.create_quest.await_count, calls)
            self.assertIn('уже решена', self.last_text())
        with patch('game.today', return_value=date(2026, 11, 6)):
            await self.app.text(10, self.profile, '/daily')
            await self.app.text(10, self.profile, '200')
        self.assertEqual(self.profile['game']['streak'], 2)

    async def test_daily_reviews_weak_topic(self):
        g = game.ensure(self.profile)
        g['topics']['дроби'] = {'name': 'Дроби', 'n': 4, 'score': 1.0, 'quests': 1}
        with patch('game.today', return_value=date(2026, 11, 6)):  # чётный день - повторение
            await self.app.text(10, self.profile, '/daily')
        self.assertIn('Сегодня повторяем', self.sent_text())
        self.assertEqual(self.ai.create_quest.call_args.args[1], 'Дроби')

    async def test_boss_battle_victory_and_comeback(self):
        await self.app.text(10, self.profile, '/boss')
        for _ in range(3):
            await self.app.text(10, self.profile, '1')
        self.assertIn('устоял', self.sent_text())
        await self.app.text(10, self.profile, '/boss')
        boss = self.profile['boss']
        await self.app.callback(10, self.profile, f"answer:{boss['id']}:0:1")
        await self.app.callback(10, self.profile, f"answer:{boss['id']}:0:1")
        self.assertEqual(boss['index'], 1)
        await self.app.text(10, self.profile, '2')
        await self.app.text(10, self.profile, '2')
        self.assertIn('Идеальная победа', self.sent_text())
        self.assertIn('Камбэк', self.sent_text())
        self.assertEqual(self.profile['game']['stats']['boss_perfect'], 1)
        self.assertNotIn('boss_perfect', self.profile['game']['badges'])  # за одну победу медаль не дают

    async def test_teach_mode(self):
        await self.app.text(10, self.profile, '/review')
        self.assertEqual(self.profile['state'], 'teach')
        await self.app.text(10, self.profile, 'Процент — это сотая часть')
        self.assertEqual(self.ai.teach.call_args.args[2], 'Проценты')
        self.assertIn('95%', self.sent_text())
        self.assertEqual(self.profile['game']['stats']['teach_good'], 1)
        self.assertEqual(self.profile['state'], 'ready')

    async def test_mode_commands_need_topic(self):
        self.profile.update(history=[], topic='')
        await self.app.text(10, self.profile, '/boss')
        self.ai.quiz.assert_not_awaited()
        await self.app.text(10, self.profile, '/quest')
        self.assertEqual(self.profile['state'], 'quest_topic')
        await self.app.text(10, self.profile, 'Дроби')
        self.assertEqual(self.ai.create_quest.call_args.args[1], 'Дроби')


class ScreenTests(FlowTests):
    async def test_hero_card_has_no_quest_shortcuts(self):
        await self.app.text(10, self.profile, '/me')
        self.assertNotIn('go:random', self.payloads())
        self.assertNotIn('go:daily', self.payloads())
        self.assertIn('home', self.payloads())

    async def test_home_screen_has_its_picture(self):
        self.app.images = {'home': 'home-picture', 'welcome': 'welcome-picture'}
        await self.app.callback(10, self.profile, 'home')
        self.assertEqual(self.bot.send_message.call_args.kwargs['attachments'][0], 'home-picture')
        await self.app.text(10, self.profile, '/start')
        self.assertEqual(self.bot.send_message.call_args.kwargs['attachments'][0], 'welcome-picture')

    async def test_fire_strip_starts_with_today(self):
        with patch('game.today', return_value=date(2026, 11, 5)):
            game.touch_streak(self.profile, date(2026, 11, 3))
            game.touch_streak(self.profile, date(2026, 11, 5))
            self.assertEqual(game.fire_strip(self.profile)[:4], [True, False, True, False])
            await self.app.text(10, self.profile, '/me')
        self.assertIn('🔥▫️🔥▫️', self.last_text())

    def test_old_profile_gets_fire_days_from_its_streak(self):
        old = {'game': {'xp': 0, 'streak': 3, 'best_streak': 3, 'last_day': '2026-11-05'}}
        game.ensure(old)
        self.assertEqual(old['game']['fire_days'], ['2026-11-03', '2026-11-04', '2026-11-05'])

    async def test_explained_topics_are_listed_newest_first(self):
        self.ai.topic_card.return_value = {'kind': 'question', 'topic': 'Казахи', 'subject': '', 'hook': '', 'quests': []}
        await self.app.text(10, self.profile, 'казахи')
        self.ai.topic_card.return_value = copy.deepcopy(CARD)
        await self.app.text(10, self.profile, 'проценты')
        self.ai.topic_card.return_value = {'kind': 'chat', 'topic': '', 'subject': '', 'hook': '', 'quests': []}
        await self.app.text(10, self.profile, 'привет')
        await self.app.callback(10, self.profile, 'me:know')
        text = self.last_text()
        self.assertIn('📖 Казахи', text)
        self.assertLess(text.index('Проценты'), text.index('Казахи'))
        self.assertEqual(len(self.profile['game']['topics']), 2)

    def test_topic_list_keeps_a_hundred(self):
        p = {}
        game.record_check(p, 'Тема 0', 1)
        for i in range(1, 106):
            game.record_topic(p, f'Тема {i}')
        topics = game.ensure(p)['topics']
        self.assertEqual(len(topics), 100)
        self.assertIn('тема 0', topics)  # тему с проверками не забываем
        self.assertNotIn('тема 1', topics)  # забываем самые старые без проверок
        self.assertIn('тема 105', topics)


class FairPlayTests(FlowTests):
    # награды только за то, что человек решил сам, и без повторов одного и того же

    async def reveal(self):
        run = self.profile['quest']
        prefix = f"quest:{run['id']}:{run['stage']}"
        await self.app.callback(10, self.profile, prefix + ':hint')
        await self.app.callback(10, self.profile, prefix + ':reveal')

    async def test_revealed_daily_is_not_counted_until_similar_is_solved(self):
        g = self.profile['game']
        with patch('game.today', return_value=date(2026, 11, 5)):
            await self.app.text(10, self.profile, '/daily')
            first = self.profile['quest']['code']
            await self.reveal()
            self.assertIn('разобрали вместе', self.sent_text())
            self.assertEqual((g['daily_day'], g['stats']['daily'], g['streak']), ('', 0, 0))
            self.assertIn('daily:again', self.payloads())
            await self.app.text(10, self.profile, '/daily')  # та же задача уже с ответом, поэтому похожая
            run = self.profile['quest']
            self.assertNotEqual(run['code'], first)
            self.assertEqual(run['mode'], 'daily')
            self.assertEqual(self.store.daily_get(1, '2026-11-05'), run['code'])
            await self.app.text(10, self.profile, '200')
        self.assertEqual((g['daily_day'], g['stats']['daily'], g['streak']), ('2026-11-05', 1, 1))
        self.assertIn('Задача дня решена', self.sent_text())

    async def test_no_more_than_three_similar_daily_tasks(self):
        with patch('game.today', return_value=date(2026, 11, 5)):
            await self.app.text(10, self.profile, '/daily')
            for _ in range(4):
                await self.reveal()
                await self.app.callback(10, self.profile, 'daily:again')
        self.assertIn('похожих задач хватит', self.last_text())
        self.assertEqual(self.profile['state'], 'ready')
        self.assertEqual(self.ai.create_quest.await_count, 4)  # исходная и три похожих

    async def test_replayed_quest_gives_nothing(self):
        await self.start_quest()
        code = self.profile['quest']['code']
        for answer in ('200', 'B', '660'):
            await self.app.text(10, self.profile, answer)
        g = self.profile['game']
        before = (g['xp'], copy.deepcopy(g['stats']), copy.deepcopy(g['topics']))
        await self.app.text(10, self.profile, f'/play {code}')
        self.assertIn('уже проходил', self.sent_text())
        for answer in ('200', 'B', '660'):
            await self.app.text(10, self.profile, answer)
        self.assertIn('Пройдено ещё раз', self.last_text())
        self.assertEqual((g['xp'], g['stats'], g['topics']), before)

    async def test_revealed_stage_makes_replay_unrewarded(self):
        run = await self.start_quest()
        await self.reveal()
        self.assertIn(run['code'], self.profile['game']['finished'])

    async def test_pool_quest_by_code_is_not_a_friend_quest(self):
        code = self.store.save_quest(copy.deepcopy(QUEST), None, '8–9 класс', 2, 'ai', pool=1)
        await self.app.text(10, self.profile, f'/play {code}')
        self.assertFalse(self.profile['quest']['shared'])
        self.assertNotIn('Квест от друга', self.sent_text())

    async def test_teach_rejects_my_own_text_and_counts_topic_once_a_day(self):
        lumi = 'Процент — это сотая часть числа, поэтому 25% от 800 это 800 умножить на 0,25.'
        self.profile['history'].append({'role': 'assistant', 'content': lumi})
        await self.app.text(10, self.profile, '/teach')
        await self.app.text(10, self.profile, lumi)
        self.assertIn('слово в слово', self.last_text())
        self.ai.teach.assert_not_awaited()
        self.assertEqual(self.profile['state'], 'teach')
        await self.app.text(10, self.profile, 'Ну процент это когда целое делят на сто кусочков и берут сколько-то')
        await self.app.text(10, self.profile, '/teach')
        await self.app.text(10, self.profile, 'Процент это одна сотая, например скидка 10% от 500 это 50 рублей')
        self.assertEqual(self.profile['game']['stats']['teach_good'], 1)

    async def test_boss_rematches_do_not_farm_awards(self):
        g = self.profile['game']
        for _ in range(4):
            await self.app.text(10, self.profile, '/boss')
            for _ in range(3):
                await self.app.text(10, self.profile, '2')
        self.assertEqual((g['stats']['bosses'], g['stats']['boss_perfect']), (1, 1))
        self.assertIn('тренировочная', self.sent_text())
        xp = g['xp']
        await self.app.text(10, self.profile, '/boss')
        for _ in range(3):
            await self.app.text(10, self.profile, '2')
        self.assertEqual(g['xp'], xp)

    async def test_boss_question_with_three_options(self):
        three = [{**q, 'options': ['а', 'б', 'в']} for q in copy.deepcopy(BOSS)]
        self.ai.quiz.return_value = three
        await self.app.text(10, self.profile, '/boss')
        self.assertEqual(self.payloads()[:3], [f"answer:{self.profile['boss']['id']}:0:{i}" for i in range(3)])
        self.assertNotIn(f"answer:{self.profile['boss']['id']}:0:3", self.payloads())
        await self.app.text(10, self.profile, '4')
        self.assertIn('от 1 до 3', self.last_text())
        self.assertEqual(self.profile['boss']['index'], 0)
        await self.app.text(10, self.profile, '2')
        self.assertEqual(self.profile['boss']['score'], 1)

    async def test_wrong_boss_answers_do_not_extend_streak(self):
        await self.app.text(10, self.profile, '/boss')
        for _ in range(3):
            await self.app.text(10, self.profile, '1')
        self.assertEqual(self.profile['game']['streak'], 0)

    async def test_questions_give_xp_only_fifteen_times_a_day(self):
        for _ in range(17):
            await self.app.callback(10, self.profile, 'ai:example')
        g = self.profile['game']
        self.assertEqual((g['stats']['questions'], g['xp']), (15, 30))


class ClassTests(FlowTests):
    async def test_teacher_creates_class_student_joins_assignment_and_report(self):
        teacher = self.ready_profile(50, role='teacher', group='Учитель', nick='Анна Сергеевна')
        await self.app.text(10, teacher, '/class')
        self.assertIn('cls:new', self.payloads())
        await self.app.callback(10, teacher, 'cls:new')
        await self.app.text(10, teacher, '7Б математика')
        self.assertEqual(teacher['state'], 'class_grade')
        await self.app.callback(10, teacher, 'cls:grade:57')
        code = self.store.teacher_classes(50)[0]['code']
        self.assertIn(code, self.sent_text())

        student = self.ready_profile(2, name='Иван Петров')
        await self.app.text(10, student, f'/class {code.lower()}')
        self.assertEqual(self.store.members(code), [2])
        self.assertIn('Учитель видит', self.sent_text())
        to_teacher = [c for c in self.bot.send_message.call_args_list if c.kwargs.get('user_id') == 50]
        self.assertIn('Иван Петров', to_teacher[-1].kwargs['text'])

        await self.app.callback(10, teacher, f'cls:assign:{code}')
        await self.app.text(10, teacher, 'Проценты')
        profile_used = self.ai.create_quest.call_args.args[0]
        self.assertEqual(profile_used['group'], '5–7 класс')
        to_student = [c for c in self.bot.send_message.call_args_list if c.kwargs.get('user_id') == 2]
        assignment = int(self.payloads(to_student[-1])[0].split(':')[1])
        self.assertIn('660 рублей', self.sent_text())  # учитель видит ответы

        await self.app.callback(10, student, f'asg:{assignment}')
        for answer in ('200', 'B', '660'):
            await self.app.text(10, student, answer)
        self.assertIn('отправлен учителю', self.sent_text())
        self.assertEqual(self.store.results(assignment)[2]['stars'], 9)

        self.store.save(2, student)
        await self.app.callback(10, teacher, f'cls:report:{code}')
        self.assertIn('1/1', self.last_text())
        self.assertIn('Иван Петров', self.last_text())

    async def test_student_cannot_open_foreign_assignment_or_use_teacher_buttons(self):
        cid = self.store.create_class(50, 'Чужой', '8–9 класс')
        qcode = self.store.save_quest(copy.deepcopy(QUEST), 50)
        assignment = self.store.add_assignment(cid, qcode, 'Проценты')
        await self.app.callback(10, self.profile, f'asg:{assignment}')
        self.assertEqual(self.profile['state'], 'ready')
        self.assertIn('не найдено', self.last_text())
        await self.app.callback(10, self.profile, 'cls:new')
        self.assertEqual(self.profile['state'], 'ready')
        await self.app.text(10, self.profile, '/class NOPE1')
        self.assertIn('не найден', self.last_text())

    async def test_class_deep_link_joins_after_onboarding(self):
        cid = self.store.create_class(50, '9А', '8–9 класс')
        p = self.store.get(7)
        p['uid'] = 7
        await self.app.start(10, p, f'class_{cid}')
        await self.app.callback(10, p, 'group:89')
        await self.app.callback(10, p, 'interests:done')
        await self.app.callback(10, p, 'nick:skip')
        self.assertEqual(self.store.members(cid), [7])


class GameTests(unittest.TestCase):
    def test_levels_tiers_secrets_and_streak(self):
        p = {}
        game.ensure(p)
        self.assertEqual(game.level(0)[0], 1)
        self.assertEqual(game.level(80)[0], 2)
        self.assertEqual(game.level(10 ** 6)[0], len(game.TITLES))
        self.assertTrue(any('Новый уровень 2' in e for e in game.add_xp(p, 80)))
        self.assertEqual(game.bump(p, 'quests', 4), [])
        self.assertIn('Искатель приключений I', game.bump(p, 'quests')[0])
        self.assertIn('🥈', game.bump(p, 'quests', 20)[0])
        self.assertEqual(game.unlock_secret(p, 'comeback')[0].count('Камбэк'), 1)
        self.assertEqual(game.unlock_secret(p, 'comeback'), [])
        for day in range(1, 6):
            events = game.touch_streak(p, date(2026, 1, day))
        self.assertTrue(any('Огненная серия I' in e for e in events))
        self.assertEqual(game.current_streak(p, date(2026, 1, 9)), 0)
        self.assertEqual(game.days_word(3), 'дня')
        self.assertIn('❔', game.badges_text(p))

    def test_erudite_counts_only_mastered_topics(self):
        p = {}
        for topic in ('Дроби', 'Проценты', 'Закон Ома'):
            game.record_check(p, topic, 1)
            game.record_quest(p, topic)
        self.assertNotIn('erudite', game.ensure(p)['badges'])
        for topic in ('Дроби', 'Проценты', 'Закон Ома'):
            for _ in range(3):
                game.record_check(p, topic, 1)
        self.assertIn('Эрудит I', ' '.join(game.check_badges(p)))
        self.assertEqual(game.topic_status(game.ensure(p)['topics']['дроби']), ('✅', 'получается'))

    def test_adaptive_difficulty_and_mastery(self):
        p = {}
        self.assertEqual(game.record_result(p, 1.0) + game.record_result(p, 0.9), [])
        self.assertIn('поднимаю', game.record_result(p, 1.0)[0])
        self.assertEqual(p['game']['difficulty'], 3)
        for _ in range(3):
            events = game.record_result(p, 0.1)
        self.assertEqual(p['game']['difficulty'], 2)
        self.assertIn('проще', events[0])
        game.record_check(p, 'Дроби', 0)
        game.record_check(p, 'Дроби', 0.5)
        game.record_check(p, 'Проценты', 1)
        game.record_check(p, 'Проценты', 1)
        strong, weak = game.topic_report(p)
        self.assertEqual(strong[0][0], 'Проценты')
        self.assertEqual(game.weakest_topic(p), 'Дроби')
        self.assertGreater(game.xp_for(p, 10), 10)

    def test_formatting_helpers(self):
        self.assertEqual(rich('## Итог\n* пункт\n**жир** <i>'), '<b>Итог</b>\n• пункт\n<b>жир</b> &lt;i&gt;')
        parts = chunks('<b>строка</b>\n' * 400, size=500)
        self.assertTrue(all(len(p) <= 500 for p in parts))


if __name__ == '__main__':
    unittest.main()
