import copy
import csv
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import analytics
import game
import web
from answers import AI
from storage import Store
from test_flows import QUEST

OFFLINE = json.loads((Path(__file__).resolve().parents[1] / 'content' / 'offline_quests.json').read_text(encoding='utf-8'))


def seed(store):
    # учитель, класс, два ученика, задание и события, как после недели работы
    now = int(time.time())
    teacher = store.get(50)
    teacher.update(role='teacher', group='Учитель', nick='Анна Сергеевна', onboarded=True)
    store.save(50, teacher)
    code = store.create_class(50, '7Б математика', '5–7 класс')
    for uid, name in ((1, 'Иван Петров'), (2, 'Мария Иванова')):
        p = store.get(uid)
        p.update(name=name, nick=name.split()[0], group='5–7 класс', onboarded=True)
        game.add_xp(p, 300)
        game.record_check(p, 'Дроби', 0)
        game.record_check(p, 'Дроби', 0.5)
        game.bump(p, 'quests', 3)
        store.save(uid, p)
        store.join_class(code, uid)
        store.log(uid, 'onboarded', value=1, ts=now - 9 * 86400)
        for i in range(12):
            store.log(uid, 'stage', 'Дроби', 0.0 if i < 5 else 1.0, ts=now - 8 * 86400 + i * 3600)
        store.log(uid, 'quest_start', 'Дроби', ts=now - 86400)
        store.log(uid, 'xp', value=120, ts=now - 3600)
    store.log(1, 'quest_done', 'Дроби', 7 / 9, ts=now - 3000)
    store.log(1, 'boss_done', 'Дроби', 1.0, ts=now - 2000)
    store.log(1, 'teach', 'Дроби', 0.6, ts=now - 1900)
    store.log(1, 'teach', 'Дроби', 0.9, ts=now - 1000)
    qcode = store.save_quest(copy.deepcopy(QUEST), 50, '5–7 класс', 2, 'teacher')
    assignment = store.add_assignment(code, qcode, 'Проценты')
    store.save_result(assignment, 1, 8, 9)
    return code


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / 'bot.sqlite3'))
        self.code = seed(self.store)
        self.secret = self.store.secret()
        app = web.WebApp(self.store, {'username': 'lumi_bot', 'started': time.time(), 'gigachat': 'ok'})
        self.client = TestClient(TestServer(app.application()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def token(self, uid, role):
        return web.make_token(self.secret, uid, role)

    async def get(self, url):
        response = await self.client.get(url)
        return response.status, await response.text()

    def test_token_roundtrip_tamper_and_expiry(self):
        token = self.token(1, 's')
        self.assertEqual(web.read_token(self.secret, token), (1, 's'))
        body, sig = token.split('.')
        self.assertIsNone(web.read_token(self.secret, body + '.' + '0' * len(sig)))
        self.assertIsNone(web.read_token('другой', token))
        self.assertIsNone(web.read_token(self.secret, web.make_token(self.secret, 1, 's', days=-1)))
        self.assertIsNone(web.read_token(self.secret, 'мусор'))

    async def test_landing_health_and_static(self):
        status, text = await self.get('/')
        self.assertEqual(status, 200)
        self.assertIn('Люми', text)
        self.assertIn('https://max.ru/lumi_bot', text)
        status, text = await self.get('/health')
        self.assertEqual(json.loads(text)['gigachat'], 'ok')
        response = await self.client.get('/static/welcome.png')
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.get('/static/..%2Fweb.py'))[0], 404)

    async def test_student_cabinet(self):
        status, text = await self.get(f'/app?t={self.token(1, "s")}')
        self.assertEqual(status, 200)
        for part in ('Иван', 'Мои темы', 'Дроби', 'стоит повторить', 'Достижения', 'Искатель приключений', '7Б математика', 'рейтинг'):
            self.assertIn(part, text)
        status, text = await self.get('/app')
        self.assertIn('кнопкой', text)
        self.assertIn('data-tab="know"', (await self.get(f'/app?t={self.token(1, "s")}'))[1])
        self.assertEqual((await self.get('/app?t=bad'))[0], 403)

    async def test_all_topics_and_fire_strip(self):
        p = self.store.get(3)
        p.update(nick='Оля', group='8–9 класс', onboarded=True)
        game.record_topic(p, 'Казахи')  # только объяснение, без проверок
        game.touch_streak(p)
        self.store.save(3, p)
        status, text = await self.get(f'/app?t={self.token(3, "s")}')
        self.assertIn('Казахи', text)
        self.assertIn('разобрали, проверок ещё не было', text)
        strip = text.split('<div class="strip">')[1].split('</div>')[0]
        self.assertTrue(strip.startswith('<i class="on">🔥</i>'))  # первым идёт сегодняшний день
        self.assertEqual(strip.count('🔥'), 1)
        self.assertIn('Серия за две недели', text)
        self.assertNotIn('Огоньки', text)

    async def test_teacher_dashboard_csv_and_api(self):
        token = self.token(50, 't')
        status, text = await self.get(f'/teacher?t={token}')
        self.assertEqual(status, 200)
        for part in ('7Б математика', 'Иван Петров', 'Мария Иванова', 'Проценты', '1/2', 'Скачать CSV', self.code):
            self.assertIn(part, text)
        response = await self.client.get(f'/teacher/{self.code}.csv?t={token}')
        rows = list(csv.reader(io.StringIO((await response.read()).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(rows[0][0], 'Ученик')
        self.assertIn('8/9', sum(rows[1:], []))
        data = json.loads(await (await self.client.get(f'/api/v1/class/{self.code}?t={token}')).text())
        self.assertEqual(len(data['students']), 2)
        # ученик не видит кабинет учителя, учитель не видит чужой класс
        self.assertEqual((await self.get(f'/teacher?t={self.token(1, "s")}'))[0], 403)
        other = self.store.create_class(77, 'Чужой', '8–9 класс')
        self.assertEqual((await self.get(f'/teacher?t={token}&c={other}'))[0], 404)

    async def test_admin_metrics(self):
        self.assertEqual((await self.get(f'/admin?t={self.token(1, "s")}'))[0], 403)
        status, text = await self.get(f'/admin?t={self.token(1, "a")}')
        self.assertEqual(status, 200)
        self.assertIn('Метрики пользы', text)
        data = json.loads(await (await self.client.get(f'/api/v1/metrics?t={self.token(1, "a")}')).text())
        self.assertEqual(data['users'], 3)


class AnalyticsTests(unittest.TestCase):
    def test_metrics_measure_engagement_and_learning_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'bot.sqlite3'))
            seed(store)
            m = analytics.metrics(store)
        self.assertEqual(m['onboarded'], 2)
        self.assertEqual(m['wau'], 2)
        self.assertEqual(m['return_d7'], 1.0)
        self.assertEqual((m['quests_started'], m['quests_done']), (2, 1))
        self.assertAlmostEqual(m['completion'], 0.5)
        self.assertEqual((m['gain_before'], m['gain_after']), (0.0, 1.0))
        self.assertAlmostEqual(m['teach_before'], 0.6)
        self.assertEqual((m['classes'], m['students_in_classes']), (1, 2))
        self.assertEqual(len(m['daily_active']), 14)
        text = analytics.summary(m)
        self.assertIn('Учебный эффект', text)
        self.assertIn('+100 п.п.', text)


class OfflineQuestTests(unittest.TestCase):
    def test_every_group_has_a_valid_checked_quest(self):
        for group in ('1–4 класс', '5–7 класс', '8–9 класс', '10–11 класс', 'Взрослый'):
            self.assertIn(group, OFFLINE)
            for quest in OFFLINE[group]:
                self.assertEqual(len(quest['stages']), 3)
                for stage in quest['stages']:
                    with self.subTest(group=group, question=stage['question']):
                        self.assertEqual(len(stage['hints']), 2)
                        self.assertFalse(AI.leaks_answer(stage['answer'], stage['hints']))
                        if stage['kind'] == 'choice':
                            self.assertEqual(stage['options'][stage['correct']], stage['answer'])
                        else:
                            # ответ этапа = последнее число в решении
                            last = AI.numbers_in(stage['explain'])[-1] if group != '10–11 класс' or 'Переплата' not in stage['explain'] \
                                else AI.numbers_in(stage['explain'].split('Переплата')[0])[-1]
                            self.assertIs(AI.match_answer(stage['answer'], last), True)

    def test_offline_arithmetic(self):
        # пересчёт руками, чтобы опечатка в json не дошла до детей
        self.assertEqual(15 * 6, 90)
        self.assertEqual(90 + 4 * 15 - 70, 80)
        self.assertEqual(4800 - 4800 * 0.25 - (4800 * 0.75) * 0.05, 3420)
        self.assertEqual(42 / 14 * 42, 126)
        self.assertEqual(504 / 126, 4)
        self.assertEqual(round((100000 * 1.1 - 60500) * 1.1), 54450)
        self.assertEqual(4860 / (1.5 * 6 * 30 * 6 * 0.4), 7.5)


class PromptTests(unittest.TestCase):
    def test_prompt_contains_grade_level_and_weak_topics(self):
        p = {'group': '10–11 класс', 'interests': ['Игры'], 'filters': '', 'nick': 'Нео'}
        game.ensure(p)['difficulty'] = 3
        game.record_check(p, 'Логарифмы', 0)
        game.record_check(p, 'Логарифмы', 0)
        prefs = json.loads(AI.preferences(p))
        self.assertIn('ЕГЭ', prefs['что проходят в этом классе'])
        self.assertIn('Профи', prefs['уровень сложности'])
        self.assertEqual(prefs['слабые темы ученика'], ['Логарифмы'])
        self.assertTrue(AI.senior(p))
        self.assertFalse(AI.senior({'group': '1–4 класс'}))
        self.assertIn('таблица умножения', json.loads(AI.preferences({'group': '1–4 класс'}))['что проходят в этом классе'])


if __name__ == '__main__':
    unittest.main()


class FormulaTests(unittest.TestCase):
    def test_latex_becomes_readable_text(self):
        from tex import to_text
        cases = {
            r'Поток: $\Phi_B = LI$, где $L$ — индуктивность.': 'Поток: Φ_B = LI, где L — индуктивность.',
            r'$$\mathcal{E} = -\frac{d\Phi}{dt}$$': 'ℰ = -dΦ/dt',
            r'\( v = \sqrt{2gh} \)': 'v = √(2gh)',
            r'$x_{1,2} = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}$': 'x₁,₂ = (-b ± √(b² - 4ac))/(2a)',
            r'$\sin 30^\circ = \frac{1}{2}$, $\log_2 8 = 3$': 'sin 30° = 1/2, log₂ 8 = 3',
            r'$\Delta t \approx 5\,\text{с}$, 10\% скидка': 'Δt ≈ 5 с, 10% скидка',
            'Цена 500 $ и 20%.': 'Цена 500 $ и 20%.',
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(to_text(source), expected)

    def test_rich_and_plain_convert_formulas(self):
        from ui import plain, rich
        self.assertEqual(rich(r'**Закон Ома:** $I = \frac{U}{R}$'), '<b>Закон Ома:</b> I = U/R')
        self.assertEqual(plain(r'$a < b$'), 'a &lt; b')


class PwaTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_service_worker_and_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'bot.sqlite3'))
            client = TestClient(TestServer(web.WebApp(store, {'started': time.time()}).application()))
            await client.start_server()
            try:
                manifest = json.loads(await (await client.get('/manifest.webmanifest')).text())
                self.assertEqual((manifest['start_url'], manifest['display']), ('/app', 'standalone'))
                for icon in manifest['icons']:
                    self.assertEqual((await client.get(icon['src'])).status, 200)
                sw = await client.get('/sw.js')
                self.assertIn('javascript', sw.headers['Content-Type'])
                text = await (await client.get('/')).text()
                self.assertIn('rel="manifest"', text)
                self.assertIn("serviceWorker", text)
            finally:
                await client.close()
