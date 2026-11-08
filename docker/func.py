import asyncio
import contextvars
import json
import logging
import os
import random
import re
import socket
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from maxapi import Bot, Dispatcher
from maxapi.enums.parse_mode import ParseMode
from maxapi.exceptions.max import MaxApiError
from maxapi.types import BotCommand, InputMedia

import analytics
import game
import texts as T
import web
from hooks import Hooks
from answers import AI, BACKGROUND, AIError, FilterError, LearningError, QuizError, credentials_from_env
from media import PhotoError, download_image, image_attachment
from storage import Store, new_profile
from texts import pick
from ui import chunks, events_text, h, home_menu, keyboard, link, plain, resume_menu, rich, topic_menu

LOG = logging.getLogger(__name__)
GROUPS = T.GROUPS
NUMBERS = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']
LETTERS = ['A', 'B', 'C']
# на эти кнопки меню перерисовывается в том же сообщении
MENU_PREFIXES = ('group:', 'interest:', 'interests:done', 'nick:', 'level:', 'cls:grade:', 'me:')
TABS = [('hero', '🦸 Герой'), ('know', '📚 Мои темы'), ('awards', '🏅 Награды'), ('class', '🏫 Класс'),
        ('settings', '⚙️ Настройки')]
CALLBACK = contextvars.ContextVar('callback', default=None)
# эти команды только что-то показывают, их можно выполнить, не дожидаясь ответа ИИ
READ_ONLY = ('/me', '/profile', '/help', '/menu', '/home')
OFFLINE_PATH = Path(__file__).parent / 'content' / 'offline_quests.json'


class ChatClosed(Exception):
    pass


def reset_topic(p):
    p.update(history=[], topic='', card=None, quest=None, boss=None, state='ready')


def looks_like_question(text):
    return text.strip().endswith('?') and not AI.numbers_in(text)


def copied_share(text, sources):
    # какая доля текста ученика дословно совпадает с текстами Люми (по четыре слова подряд)
    def fours(value):
        words = re.findall(r'\w+', value.lower())
        return {tuple(words[i:i + 4]) for i in range(len(words) - 3)}
    mine = fours(text)
    if not mine:
        return 0.0
    theirs = set()
    for source in sources:
        theirs |= fours(source)
    return len(mine & theirs) / len(mine)


def load_offline():
    try:
        return json.loads(OFFLINE_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        LOG.warning('Запасные квесты не загружены')
        return {}


def acquire_run_lock():
    # второй экземпляр с тем же токеном начнёт воровать сообщения у первого
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        lock.bind(('0.0.0.0', 18765))
    except OSError:
        lock.close()
        raise SystemExit('Бот уже запущен: порт блокировки 18765 занят. Проверь docker compose ps.') from None
    return lock


class AssistantBot:
    IMAGES = ('welcome', 'home', 'explore', 'quest', 'profile', 'quiz', 'boss', 'review', 'success', 'daily',
              'levelup', 'teacher')
    FALLBACK = {'home': 'welcome', 'quest': 'explore', 'boss': 'quiz', 'daily': 'quest', 'levelup': 'success',
                'teacher': 'profile'}

    def __init__(self, bot, store, ai, hooks=None):
        self.bot, self.store, self.ai = bot, store, ai
        self.hooks = hooks or Hooks('')
        self.busy = set()
        self.locks = {}
        self.warned = set()
        self.images = {}
        self.username = os.getenv('MAX_BOT_USERNAME', '')
        self.web_url = os.getenv('WEB_BASE_URL', '').rstrip('/')
        self.admins = {int(x) for x in re.findall(r'\d+', os.getenv('ADMIN_IDS', ''))}
        self.pool_target = int(os.getenv('POOL_TARGET', '2') or 0)
        self.offline = load_offline()
        self.background = set()
        self.refilling = set()
        self.secret = store.secret()


    async def prepare(self):
        result = await self.bot.set_commands(*(BotCommand(name=name, description=description)
                                               for name, description in T.COMMANDS))
        LOG.info('Меню команд зарегистрировано: %s', len(result.commands or []))
        if not self.username and hasattr(self.bot, 'get_me'):
            try:
                self.username = (await self.bot.get_me()).username or ''
            except Exception as exc:
                LOG.warning('Имя бота не получено: %s', type(exc).__name__)
        for name in self.IMAGES:
            path = Path(__file__).parent / 'assets' / f'{name}.png'
            if not path.exists():
                continue
            try:
                self.images[name] = await self.bot.upload_media(InputMedia(path=str(path), type='image'))
            except Exception as exc:
                LOG.warning('Не удалось загрузить %s: %s', name, type(exc).__name__)
        LOG.info('Иллюстрации загружены: %s', ', '.join(self.images) or 'нет')

    async def banner(self, chat_id, name, text, attachments=None):
        image = self.images.get(name) or self.images.get(self.FALLBACK.get(name, ''))
        return await self.send(chat_id, text, ([image] if image else []) + (attachments or []))

    async def send(self, chat_id, text, attachments=None, user_id=None):
        parts = chunks(text)
        mid = None
        for i, part in enumerate(parts):
            target = {'user_id': user_id} if user_id else {'chat_id': chat_id}
            try:
                result = await self.bot.send_message(**target, text=part, format=ParseMode.HTML,
                                                     attachments=attachments if i == len(parts) - 1 else None)
            except MaxApiError as exc:
                if getattr(exc, 'code', None) in (403, 404):
                    raise ChatClosed() from None
                raise
            mid = getattr(getattr(getattr(result, 'message', None), 'body', None), 'mid', None)
        return mid

    async def menu(self, chat_id, text, attachments=None, image=None):
        picture = self.images.get(image) if image else None
        attachments = ([picture] if picture else []) + (attachments or [])
        ctx = CALLBACK.get()
        if ctx and not ctx['answered']:
            ctx['answered'] = True
            try:
                await ctx['event'].answer(new_text=text, attachments=attachments, format=ParseMode.HTML)
                return
            except Exception as exc:
                LOG.info('Меню не обновлено на месте: %s', type(exc).__name__)
        await self.send(chat_id, text, attachments)

    async def edit(self, mid, text, attachments=None):
        if not isinstance(mid, str) or not hasattr(self.bot, 'edit_message'):
            return False
        try:
            await self.bot.edit_message(message_id=mid, text=text, attachments=attachments, format=ParseMode.HTML)
            return True
        except Exception as exc:
            LOG.info('Сообщение не обновилось: %s', type(exc).__name__)
            return False

    async def delete(self, mid):
        if isinstance(mid, str) and hasattr(self.bot, 'delete_message'):
            try:
                await self.bot.delete_message(message_id=mid)
            except Exception:
                pass

    async def write(self, chat_id, placeholder, stream, finish):
        # сразу шлём заглушку и дописываем в неё ответ по мере генерации.
        # finish(text) возвращает итоговый текст и кнопки
        mid = await self.send(chat_id, placeholder)
        text, shown = '', 0.0
        try:
            async with self.keep_typing(chat_id):
                async for text in stream:
                    if time.monotonic() - shown >= 1.5 and len(text) < 3500:
                        shown = time.monotonic()
                        await self.edit(mid, rich(text) + ' ▍')
        except BaseException:
            await self.delete(mid)
            raise
        final, attachments = await finish(text)
        if len(final) > 3900 or not await self.edit(mid, final, attachments):
            await self.delete(mid)
            await self.send(chat_id, final, attachments)
        return text

    async def safe_send(self, chat_id, text, attachments=None):
        try:
            await self.send(chat_id, text, attachments)
        except Exception as exc:
            LOG.info('Сообщение не доставлено: %s', type(exc).__name__)

    async def notify(self, user_id, text, attachments=None):
        try:
            await self.send(None, text, attachments, user_id=user_id)
            return True
        except Exception as exc:
            LOG.info('Уведомление %s не доставлено: %s', user_id, type(exc).__name__)
            return False

    async def typing(self, chat_id):
        if hasattr(self.bot, 'send_action'):
            try:
                await self.bot.send_action(chat_id=chat_id)
            except Exception:
                pass

    @asynccontextmanager
    async def keep_typing(self, chat_id, interval=4.0):
        stop = asyncio.Event()

        async def loop():
            while not stop.is_set():
                await self.typing(chat_id)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)

    @asynccontextmanager
    async def progress(self, chat_id, steps, interval=4.0):
        # сообщение "рисую карту... проверяю ответы...", пока ждём GigaChat
        mid = await self.send(chat_id, steps[0])
        stop = asyncio.Event()

        async def ticker():
            for step in steps[1:] + [None]:
                await self.typing(chat_id)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    pass
                if step and isinstance(mid, str) and hasattr(self.bot, 'edit_message'):
                    try:
                        await self.bot.edit_message(message_id=mid, text=step, format=ParseMode.HTML)
                    except Exception:
                        pass
            while not stop.is_set():
                await self.typing(chat_id)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(ticker())
        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            if isinstance(mid, str) and hasattr(self.bot, 'delete_message'):
                try:
                    await self.bot.delete_message(message_id=mid)
                except Exception:
                    pass


    def log(self, p, kind, topic='', value=None):
        try:
            self.store.log(p.get('uid'), kind, topic, value)
        except Exception as exc:
            LOG.warning('Событие не записано: %s', type(exc).__name__)
        self.hooks.emit(kind, p, topic, value)

    def xp(self, p, amount):
        if amount > 0:
            self.log(p, 'xp', value=amount)
        return game.add_xp(p, amount)


    def is_teacher(self, p):
        return p.get('role') == 'teacher'

    def token(self, p, role=None):
        return web.make_token(self.secret, p.get('uid') or 0, role or ('t' if self.is_teacher(p) else 's'))

    def cabinet_button(self, p, text=None, teacher=None, page=None):
        teacher = self.is_teacher(p) if teacher is None else teacher
        page = page or ('teacher' if teacher else 'app')
        text = text or {'teacher': '🌐 Кабинет учителя', 'admin': '🌐 Метрики'}.get(page, '📊 Мой кабинет')
        if not self.web_url:
            return None
        role = {'teacher': 't', 'admin': 'a'}.get(page, 's')
        path = {'app': 'app'}.get(page, page)
        return link(text, f'{self.web_url}/{path}?t={self.token(p, role)}')

    def kb(self, p):
        return topic_menu() if p['topic'] or p['history'] else self.home_kb(p)

    def home_kb(self, p):
        extra = [self.cabinet_button(p, '📊 Кабинет')]
        if self.is_teacher(p):
            extra.append(('🏫 Классы', 'go:class'))
        top = []
        if p['state'] in ('quest', 'boss'):
            top.append([('▶️ Продолжить', 'resume'), ('🚪 Выйти', 'mode:stop')])
        elif p['topic']:
            top.append([('📖 Вернуться к теме', 'topic')])
        return home_menu(extra, top)

    async def home(self, chat_id, p, greeting=''):
        g = game.ensure(p)
        streak = game.current_streak(p)
        daily = '✅ решена' if game.is_today(g['daily_day']) else (
            'реши похожую' if game.is_today(g['daily_seen']) else 'ждёт')
        lines = [greeting or '🏠 <b>Главная</b>', '',
                 f"🦸 <b>{h(p['nick'] or 'Герой')}</b> · {h(p['group'])}", game.progress_line(g['xp']),
                 f'🔥 Серия: {streak} {game.days_word(streak)} · 🗓 Задача дня: {daily}']
        if p['state'] in ('quest', 'boss'):
            lines.append('⏸ Есть незаконченный ' + ('квест' if p['state'] == 'quest' else 'бой с боссом'))
        elif p['topic']:
            lines.append(f"📖 Сейчас разбираем: «{h(p['topic'])}»")
        lines += ['', 'Напиши тему или вопрос, и я объясню. Или выбери ниже 👇']
        await self.banner(chat_id, 'welcome' if greeting else 'home', '\n'.join(lines), self.home_kb(p))


    def onboarded(self, p):
        return bool(p['group'] and p['interests_done'] and p['nick_done'])

    def first_name(self, p):
        name = (p.get('name') or '').strip()
        return name if self.is_teacher(p) else name.split(' ')[0]

    async def setup(self, chat_id, p):
        teacher = self.is_teacher(p)
        if not p['group'] or p['state'] == 'group':
            p['state'] = 'group'
            rows = [[('🧸 1–4 класс', 'group:14'), ('📗 5–7 класс', 'group:57')],
                    [('📘 8–9 класс', 'group:89'), ('🎓 10–11 класс', 'group:10')],
                    [('🧑 Взрослый', 'group:adult'), ('👩‍🏫 Я учитель', 'group:teacher')]]
            return await self.menu(chat_id, T.ASK_GROUP, keyboard(rows))
        if not p['interests_done'] or p['state'] == 'interests':
            p['state'] = 'interests'
            buttons = [((('✅ ' + name) if name in p['interests'] else f'{emoji} {name}'), 'interest:' + key)
                       for key, (emoji, name) in T.INTERESTS.items()]
            rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            chosen = ', '.join(p['interests'])
            text = T.ASK_INTERESTS + (f'\n\n<b>Выбрано:</b> {h(chosen)}' if chosen else '')
            rows.append([('✅ Готово', 'interests:done')])
            return await self.menu(chat_id, text, keyboard(rows))
        if not p['nick_done'] or p['state'] == 'nick':
            p['state'] = 'nick'
            p['nick_ideas'] = T.nick_ideas()
            rows = []
            if self.first_name(p):
                rows.append([(f'👤 {self.first_name(p)}', 'nick:name')])
            if not teacher:
                rows.append([('🦸 ' + idea, f'nick:{i}') for i, idea in enumerate(p['nick_ideas'])])
                rows.append([('🔄 Другие', 'nick:more'), ('🙈 Без позывного', 'nick:skip')])
            return await self.menu(chat_id, T.ASK_NICK_TEACHER if teacher else T.ASK_NICK, keyboard(rows))
        p['state'] = 'ready'
        p.pop('nick_ideas', None)
        menu = self.kb(p)
        if not p['onboarded']:
            p['onboarded'] = True
            self.log(p, 'onboarded', value=1)
            self.schedule_refill(p['group'], game.ensure(p)['difficulty'], 3, p['played'])
            self.prepare_daily(p, game.today())
            await self.menu(chat_id, T.ready_text(h(p['nick'] or 'герой'), p['group']), menu)
        else:
            await self.menu(chat_id, '✅ Профиль обновлён! Пиши тему или выбирай 👇', menu)
        if p.get('pending_class'):
            await self.join_class(chat_id, p, p.pop('pending_class'))
        elif p.get('pending_play'):
            await self.play(chat_id, p, p.pop('pending_play'))

    async def add_interest(self, chat_id, p, text):
        text = ' '.join(text.split())
        if len(text) > 40 or len(p['interests']) >= 12:
            return await self.send(chat_id, '🎯 Напиши увлечение покороче — до 40 символов. '
                'Всего можно до 12 интересов. Если хватит — жми «✅ Готово»!')
        if text not in p['interests']:
            p['interests'].append(text)
        await self.setup(chat_id, p)

    async def set_nick(self, chat_id, p, nick):
        nick = ' '.join(nick.replace('<', '').replace('>', '').split())
        if not nick or len(nick) > 24:
            return await self.send(chat_id, '🦸 Позывной — до 24 символов. Попробуй ещё раз или выбери кнопку!')
        p.update(nick=nick, nick_done=True, state='ready')
        await self.setup(chat_id, p)


    async def hero_card(self, chat_id, p, tab='hero'):
        # "приложение" прямо в чате: вкладки переключаются в том же сообщении
        tab = tab if tab in dict(TABS) else 'hero'
        text = getattr(self, f'tab_{tab}')(p)
        nav = [(f'· {label} ·' if key == tab else label, f'me:{key}') for key, label in TABS]
        rows = [nav[:3], nav[3:]]
        if tab == 'know' and game.weakest_topic(p):
            rows.append([(f'🔁 Потренировать: {game.weakest_topic(p)[:30]}', 'train')])
        elif tab == 'class':
            rows.append([('🏫 Мои классы', 'go:class')] if self.is_teacher(p) else [('🔑 Вступить по коду', 'cls:join')])
        elif tab == 'settings':
            rows += [[('🎒 Класс', 'edit:group'), ('🎯 Интересы', 'edit:interests')],
                     [('🦸 Позывной', 'edit:nick'), ('📈 Сложность', 'me:level')], [('🌿 Фильтр тем', 'filter')]]
        rows.append([('🏠 На главную', 'home'), self.cabinet_button(p, '📊 Кабинет')])
        await self.menu(chat_id, text, keyboard(rows), image='profile')

    def tab_hero(self, p):
        g = game.ensure(p)
        streak = game.current_streak(p)
        earned, total = game.badge_count(p)
        fire = ''.join('🔥' if on else '▫️' for on in game.fire_strip(p))
        return (f"🦸 <b>{h(p['nick'] or 'Безымянный герой')}</b> · {h(p['group'] or 'класс не выбран')}\n"
                f"{game.progress_line(g['xp'])}\n"
                f"📈 Сложность: {game.difficulty_label(p)}\n\n"
                f"🔥 Серия: {streak} {game.days_word(streak)} · рекорд {g['best_streak']}\n"
                f"{fire}\n\n"
                f"🗺 Квестов: {g['stats']['quests']} · ⚔️ Боссов: {g['stats']['bosses']}\n"
                f"🎓 Уроков: {g['stats']['teach']} · 🗓 Задач дня: {g['stats']['daily']}\n"
                f"🏅 Достижения: {earned}/{total} {game.badge_icons(p)}")

    def tab_know(self, p):
        head = ('📚 <b>Мои темы</b>\n\nВсе темы, которые мы разбирали, свежие сверху. Как получается, '
                'считаю по квестам, боссам и твоим объяснениям в режиме «Учитель».')
        topics = game.recent_topics(p)
        if not topics:
            return head + '\n\nПока пусто: напиши тему, и она появится здесь.'
        lines = [head, '']
        for t in topics[:15]:
            emoji, word = game.topic_status(t)
            percent = f" — {round(game.mastery(t) * 100)}%" if t['n'] >= 2 else ''
            lines.append(f"{emoji} {h(t['name'])}{percent}, {word}")
        if len(topics) > 15:
            where = ', все есть в кабинете на сайте' if self.web_url else ''
            lines.append(f'…и ещё {len(topics) - 15}{where}.')
        if game.weakest_topic(p):
            lines += ['', 'Задача дня через день будет на тему, которая получается хуже всего.']
        return '\n'.join(lines)

    def tab_awards(self, p):
        return '🏅 <b>Достижения</b>\n🥉 → 🥈 → 🥇, у каждого три ступени\n\n' + game.badges_text(p)

    def tab_class(self, p):
        if self.is_teacher(p):
            classes = self.store.teacher_classes(p.get('uid'))
            if not classes:
                return '🏫 <b>Классы</b>\n\nКлассов пока нет. Создать: «Мои классы» → «Создать класс».'
            return '🏫 <b>Мои классы</b>\n\n' + '\n'.join(
                f"• {h(c['name'])} · код <code>{c['code']}</code> · учеников: {c['size']}" for c in classes)
        classes = self.store.member_classes(p.get('uid'))
        if not classes:
            return ('🏫 <b>Класс</b>\n\nТы пока не в классе. Если учитель дал код — нажми «Вступить по коду». '
                    'Тогда здесь будет рейтинг класса, а задания учителя будут приходить в чат.')
        lines = []
        for c in classes:
            members = self.store.members(c['code'])
            weekly = self.store.weekly_xp(members)
            profiles = self.store.profiles(members)
            board = sorted(members, key=lambda m: -weekly.get(m, 0))[:5]
            lines += [f"🏫 <b>{h(c['name'])}</b> · рейтинг недели", '']
            for i, m in enumerate(board):
                me = ' ← ты' if m == p.get('uid') else ''
                nick = profiles.get(m, {}).get('nick') or 'Герой'
                lines.append(f"{['🥇', '🥈', '🥉', '4.', '5.'][i]} {h(nick)} · {weekly.get(m, 0)} XP{me}")
            lines.append('')
        return '\n'.join(lines).strip()

    def tab_settings(self, p):
        return (f"⚙️ <b>Настройки</b>\n\n"
                f"🎒 Класс: {h(p['group'] or 'не выбран')}\n"
                f"🎯 Интересы: {h(', '.join(p['interests']) or 'любые')}\n"
                f"🦸 Позывной: {h(p['nick'] or 'нет')}\n"
                f"📈 Сложность: {game.difficulty_label(p)} (подстраивается сама)\n"
                f"🌿 Избегаем: {h(p['filters'] or 'ничего')}")

    async def level_menu(self, chat_id, p, chosen=None):
        rows = [[(('✅ ' if game.ensure(p)['difficulty'] == lvl else '') + f'{emoji} {name}', f'level:{lvl}')]
                for lvl, (emoji, name) in game.DIFFICULTY.items()]
        text = ('⚙️ <b>Сложность задач</b>\n\n'
                f'🟢 <b>База</b> — {h(T.LEVEL_GUIDE[1].split(": ", 1)[1])}\n'
                f'🟡 <b>Продвинутый</b> — {h(T.LEVEL_GUIDE[2].split(": ", 1)[1])}\n'
                f'🔴 <b>Профи</b> — {h(T.LEVEL_GUIDE[3].split(": ", 1)[1])}\n\n'
                'Чем сложнее — тем больше опыта. Я сам подстраиваю сложность по твоим результатам, '
                'но выбрать можно и вручную.')
        if chosen:
            text += f'\n\n✅ Теперь: <b>{game.difficulty_label(p)}</b>'
        rows.append([('⬅️ Назад', 'me:settings')])
        await self.menu(chat_id, text, keyboard(rows))

    async def filter_prompt(self, chat_id, p):
        p['state'] = 'filter'
        await self.send(chat_id, '🌿 <b>Сделаем общение комфортнее</b>\n\n'
            'Напиши, каких тем лучше не касаться в примерах и квестах. Например: «без ужасов».\n\n'
            'Сейчас избегаем: ' + h(p['filters'] or 'ничего') + '\n\n/cancel — назад',
            keyboard([[('🧹 Очистить фильтр', 'filter:clear')]]))

    async def add_filter(self, chat_id, p, text):
        combined = '\n'.join(filter(None, [p['filters'], text]))
        if len(combined) > 1500:
            return await self.send(chat_id, '🌿 Давай короче: на все пожелания есть 1500 символов. '
                'Старый фильтр можно очистить в /filter.')
        p['filters'] = combined
        reset_topic(p)
        await self.send(chat_id, '🌿 Сохранил! Начинаем с чистого листа — напиши тему 👇')


    async def chat_message(self, chat_id, p, text):
        history = self.context(p) + [{'role': 'user', 'content': text}]
        card = None

        async def explanation():
            # сюжеты просим, когда объяснение уже пошло, иначе на одном потоке
            # объяснение стоит за ними в очереди
            nonlocal card
            async for part in self.ai.explain_stream(p, history):
                if card is None:
                    card = asyncio.ensure_future(self.ai.topic_card(p, text, p['history'][-2:]))
                yield part

        async def finish(result):
            try:
                found = await self.card_result(card, p, text)
            except FilterError as exc:
                return h(str(exc)), self.kb(p)
            events = []
            if not (found and found['kind'] == 'chat'):
                events = self.question_reward(p)
                game.record_topic(p, found['topic'] if found else '')
            body = rich(result)
            if found and found['kind'] == 'topic':
                reset_topic(p)
                p.update(topic=found['topic'], history=[history[-1], {'role': 'assistant', 'content': result}],
                         card={'topic': found['topic'], 'quests': found['quests'], 'next': 0})
                self.log(p, 'topic', found['topic'])
                idea = found['quests'][0]
                body += (f"\n\n🎮 <b>Проверим в деле?</b> Есть квест «{plain(idea['title'])}»: {plain(idea['pitch'])}\n"
                         'Жми «🎮 Квест по теме» или придумай свой сюжет.')
            else:
                if found and found['topic']:
                    p['topic'] = found['topic']
                self.log(p, 'explain', p['topic'])
                p['history'] = (history + [{'role': 'assistant', 'content': result}])[-16:]
            p['state'] = 'ready'
            return body + events_text(events), self.kb(p)
        try:
            await self.write(chat_id, '🧠 Думаю над ответом…', explanation(), finish)
        finally:
            if card and not card.done():
                card.cancel()
            elif card and not card.cancelled():
                card.exception()  # чтобы asyncio не ругался на непрочитанную ошибку

    def question_reward(self, p):
        # опыт и "Почемучку" дают первые 15 вопросов за день: кнопку "Пример" легко нажимать без конца
        if not game.count_today(p, 'questions', 15):
            return []
        return self.xp(p, 2) + game.bump(p, 'questions')

    async def card_result(self, card, p, text):
        if card is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(card), timeout=10)
        except FilterError:
            raise
        except asyncio.TimeoutError:
            card.cancel()
        except AIError as exc:
            if '429' not in str(exc):
                LOG.warning('Сюжеты к теме не подобраны: %s', exc)
                return None
            # тариф не пустил два запроса сразу, просим сюжеты ещё раз
            try:
                return await self.ai.topic_card(p, text, p['history'][-2:])
            except AIError:
                return None
        return None

    @staticmethod
    def context(p):
        if p['history'] or not p['topic']:
            return list(p['history'])
        return [{'role': 'user', 'content': f"Тема: {p['topic']}"},
                {'role': 'assistant', 'content': f"Отлично, разбираем тему «{p['topic']}»."}]

    async def ask(self, chat_id, p, text, placeholder='🧠 Думаю над ответом…'):
        history = self.context(p) + [{'role': 'user', 'content': text}]

        async def finish(result):
            events = self.question_reward(p)
            self.log(p, 'explain', p['topic'])
            p['history'] = (history + [{'role': 'assistant', 'content': result}])[-16:]
            p['state'] = 'ready'
            return rich(result) + events_text(events), self.kb(p)
        await self.write(chat_id, placeholder, self.ai.explain_stream(p, history), finish)

    async def why(self, chat_id, p):
        async def finish(result):
            p['history'] = (p['history'] + [{'role': 'user', 'content': 'Зачем мне это в жизни?'},
                                            {'role': 'assistant', 'content': result}])[-16:]
            return rich(result), self.kb(p)
        await self.write(chat_id, '🌍 Ищу, где это пригодится…', self.ai.applications_stream(p, self.context(p)), finish)

    async def analyze(self, chat_id, p, text):
        await self.typing(chat_id)
        result = await self.ai.analyze_text(p, text)
        reset_topic(p)
        self.log(p, 'analyze')
        await self.send(chat_id, rich(result), self.kb(p))
        p['history'] = [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': result}]

    async def photo(self, chat_id, p, url, caption=''):
        if not self.onboarded(p):
            await self.setup(chat_id, p)
            return await self.send(chat_id, '👋 Сначала закончим знакомство — потом пришли фото ещё раз!')
        if p['state'] not in ('ready', 'analyze_input'):
            return await self.send(chat_id, '↩️ Сейчас идёт другое задание. Напиши /cancel и пришли фото ещё раз.')
        async with self.progress(chat_id, ['📸 Читаю задание на фото…', '🔎 Разбираю условие…', '✍️ Пишу решение по шагам…']):
            data, mime = await download_image(url)
            result = await self.ai.analyze_image(p, data, mime, caption)
        reset_topic(p)
        events = self.xp(p, 5) + game.bump(p, 'photos') if game.count_today(p, 'photos', 5) else []
        self.log(p, 'photo')
        await self.send(chat_id, rich(result) + events_text(events), self.kb(p))
        p['history'] = [{'role': 'user', 'content': 'Разбор задания с изображения. ' + caption},
                        {'role': 'assistant', 'content': result}]


    def topic_for_quest(self, p):
        if p['topic']:
            return p['topic']
        return 'тема из нашего разговора' if p['history'] else ''

    def offline_quest(self, p, stages):
        group = p['group'] if p['group'] in self.offline else 'Взрослый'
        options = [q for q in self.offline.get(group, []) if f"offline:{q['title']}" not in p['played']]
        options = options or self.offline.get(group, [])
        if not options:
            return None
        quest = json.loads(json.dumps(random.choice(options)))
        quest['stages'] = quest['stages'][:stages]
        quest['offline'] = True
        return quest

    async def start_quest(self, chat_id, p, topic, setting='', stages=3, mode='quest', pool_ok=False):
        group, level = p['group'], game.ensure(p)['difficulty']
        quest = code = None
        use_pool = pool_ok and not p['filters']
        if use_pool:
            found = self.from_pool(group, level, p['played'], stages)
            if found:
                code, quest = found
        if quest is None:
            try:
                async with self.progress(chat_id, T.QUEST_STEPS):
                    quest = await self.ai.create_quest(p, topic, setting, stages, history=p['history'])
                code = self.store.save_quest(quest, p.get('uid'), group, level, 'ai', pool=1 if use_pool else 0)
            except FilterError:
                raise
            except AIError:
                quest = self.offline_quest(p, stages) if pool_ok else None
                if not quest:
                    raise
                await self.send(chat_id, T.OFFLINE_NOTE)
        await self.begin_run(chat_id, p, quest, code, mode)
        if use_pool:
            self.schedule_refill(group, level, stages, p['played'])

    def from_pool(self, group, level, played, stages, topic=None):
        # если на своём уровне пусто, берём соседний: лучше так, чем ждать минуту
        for lvl in (level, level - 1, level + 1):
            if 1 <= lvl <= 3:
                found = self.store.pool_take(group, lvl, exclude=played, topic=topic, stages=stages)
                if found:
                    return found
        return None

    def schedule_refill(self, group, level, stages, played=(), topic=None):
        if self.pool_target <= 0 or not group:
            return
        key = (group, level, stages, topic)
        if key in self.refilling:
            return
        if topic:
            need = 0 if self.store.pool_take(group, level, exclude=played, topic=topic, stages=stages) else 1
        else:
            need = max(self.pool_target - self.store.pool_size(group, level, stages=stages),
                       3 - self.store.pool_size(group, level, exclude=played, stages=stages), 0)
        if not need:
            return
        self.refilling.add(key)
        task = asyncio.create_task(self.refill(group, level, stages, min(need, 3), topic))
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def warm_up(self, days=14):
        since = int(time.time()) - days * 86400
        users = [u for (u,) in self.store.query('SELECT DISTINCT user FROM events WHERE ts>=?', (since,))]
        combos = {(p['group'], game.ensure(p)['difficulty']) for p in self.store.profiles(users).values()
                  if p.get('group')}
        for group, level in sorted(combos):
            self.schedule_refill(group, level, 3)
        for uid, profile in self.store.profiles(users).items():
            if profile.get('onboarded') and not game.is_today(game.ensure(profile)['daily_day']):
                profile['uid'] = uid
                self.prepare_daily(profile, game.today())
        if combos:
            LOG.info('Готовлю запас квестов для: %s', ', '.join(f'{g} ({lvl})' for g, lvl in sorted(combos)))

    async def refill(self, group, level, stages, count=1, topic=None):
        BACKGROUND.set(True)
        try:
            for _ in range(count):
                profile = new_profile()
                profile.update(group=group, interests=random.sample([n for _, n in T.INTERESTS.values()], 2))
                game.ensure(profile)['difficulty'] = level
                subject = topic or random.choice(T.TOPIC_POOL.get(group) or T.TOPIC_POOL['8–9 класс'])
                quest = await self.ai.create_quest(profile, subject, '', stages)
                self.store.save_quest(quest, None, group, level, 'ai', pool=1)
                LOG.info('В запасе новый квест: %s, сложность %s, этапов %s', group, level, stages)
        except Exception as exc:
            LOG.warning('Запас квестов не пополнен: %s', type(exc).__name__)
        finally:
            self.refilling.discard((group, level, stages, topic))

    async def begin_run(self, chat_id, p, quest, code, mode, shared=False, assignment=None):
        marker = code or (f"offline:{quest['title']}" if quest.get('offline') else None)
        # этот квест уже доигран, ответы человек видел: пройти можно, но без опыта и наград
        replay = bool(marker) and marker in game.ensure(p)['finished']
        run = {'id': uuid.uuid4().hex[:8], 'code': code, 'marker': marker, 'mode': mode, 'shared': shared,
               'assignment': assignment, 'replay': replay, 'quest': quest, 'stage': 0, 'tries': 0, 'hints': 0,
               'removed': [], 'stars': [], 'xp': 0, 'level': game.ensure(p)['difficulty']}
        p.update(quest=run, state='quest', boss=None, topic=quest.get('topic') or p['topic'])
        if marker:
            p['played'] = (p['played'] + [marker])[-100:]
        self.log(p, 'quest_start', quest.get('topic', ''))
        label = {'daily': '🗓 Задача дня', 'practice': '⚡ Быстрая задача'}.get(mode, '')
        if assignment:
            label = '🏫 Задание учителя'
        head = (f'{label} · {h(quest.get("topic", ""))}\n\n' if label else '')
        intro = (f"{head}{h(quest['emoji'])} <b>{plain(quest['title'])}</b>\n\n"
                 f"🎭 <b>Роль:</b> {plain(quest['role'])}\n🎯 <b>Цель:</b> {plain(quest['goal'])}\n"
                 f"⚙️ <b>Сложность:</b> {game.difficulty_label(p)}\n\n{rich(quest['intro'])}")
        if replay:
            intro += '\n\n🔁 <i>Этот квест ты уже проходил, поэтому опыт и награды за него не начисляются.</i>'
        await self.banner(chat_id, 'daily' if mode == 'daily' else 'quest', intro)
        await self.show_stage(chat_id, p)

    def stage_buttons(self, run):
        stage = run['quest']['stages'][run['stage']]
        prefix = f"quest:{run['id']}:{run['stage']}"
        rows = []
        if stage['kind'] == 'choice':
            rows.append([(LETTERS[i], f'{prefix}:pick:{i}') for i in range(len(stage['options']))
                         if i not in run['removed']])
        rows.append([('💡 Подсказка', f'{prefix}:hint'), ('🏳 Разбор', f'{prefix}:reveal')])
        rows.append([('🚪 Выйти из квеста', 'mode:stop')])
        return keyboard(rows)

    async def show_stage(self, chat_id, p):
        run = p['quest']
        stages = run['quest']['stages']
        stage = stages[run['stage']]
        head = (f"<b>Этап {run['stage'] + 1} из {len(stages)}</b> · {game.stars(3 - min(2, run['tries'] + run['hints']))}"
                if len(stages) > 1 else '<b>Задание</b>')
        text = f"{head}\n\n{rich(stage['scene'])}\n\n❓ <b>{plain(stage['question'])}</b>"
        if stage['kind'] == 'choice':
            for i, option in enumerate(stage['options']):
                text += (f'\n❌ <s>{LETTERS[i]}) {plain(option)}</s>' if i in run['removed']
                         else f'\n<b>{LETTERS[i]})</b> {plain(option)}')
            text += '\n\n👇 Выбери вариант.'
        else:
            text += '\n\n✍️ Напиши ответ сообщением. Можно спросить что-то по теме — просто закончи вопросительным знаком.'
        await self.send(chat_id, text, self.stage_buttons(run))

    async def answer_stage(self, chat_id, p, text):
        run = p['quest']
        stage = run['quest']['stages'][run['stage']]
        if stage['kind'] == 'choice':
            letter = text.strip().upper().rstrip(').')
            mapping = {'A': 0, 'А': 0, 'B': 1, 'В': 1, 'Б': 1, 'C': 2, 'С': 2, '1': 0, '2': 1, '3': 2}
            if letter in mapping:
                return await self.pick_option(chat_id, p, mapping[letter])
            normalized = [AI.normalize_answer(o) for o in stage['options']]
            if AI.normalize_answer(text) in normalized:
                return await self.pick_option(chat_id, p, normalized.index(AI.normalize_answer(text)))
            if looks_like_question(text):
                return await self.side_question(chat_id, p, text)
            return await self.send(chat_id, '👇 Выбери вариант кнопкой — A, B или C.', self.stage_buttons(run))
        if looks_like_question(text):
            return await self.side_question(chat_id, p, text)
        verdict = AI.match_answer(stage['answer'], text)
        if verdict is None:
            await self.typing(chat_id)
            verdict = (await self.ai.check_answer(p, stage, text))['correct']
        if verdict:
            return await self.complete_stage(chat_id, p, solved=True)
        await self.wrong(chat_id, p)

    async def pick_option(self, chat_id, p, index):
        run = p['quest']
        stage = run['quest']['stages'][run['stage']]
        if index in run['removed'] or index >= len(stage['options']):
            return
        if index == stage['correct']:
            return await self.complete_stage(chat_id, p, solved=True)
        run['removed'].append(index)
        if len(stage['options']) - len(run['removed']) <= 1:
            run['tries'] += 1
            return await self.complete_stage(chat_id, p, solved=False)
        await self.wrong(chat_id, p)

    async def wrong(self, chat_id, p):
        run = p['quest']
        stage = run['quest']['stages'][run['stage']]
        run['tries'] += 1
        if run['tries'] >= 3:
            return await self.complete_stage(chat_id, p, solved=False)
        used = run['tries'] + run['hints'] - 1
        if used < len(stage['hints']):
            text = f"{pick(T.WRONG)}\n\n💡 {rich(stage['hints'][min(used, len(stage['hints']) - 1)])}"
        else:
            text = '🤔 Пока не сходится. Подсказки закончились — попробуй ещё раз или открой «🏳 Разбор».'
        await self.send(chat_id, text + f"\n\nОсталось попыток: {3 - run['tries']}", self.stage_buttons(run))

    async def hint(self, chat_id, p):
        run = p['quest']
        stage = run['quest']['stages'][run['stage']]
        shown = run['tries'] + run['hints']
        if shown >= len(stage['hints']):
            return await self.send(chat_id, '💡 Подсказки закончились. Попробуй ответить или открой «🏳 Разбор».',
                                   self.stage_buttons(run))
        run['hints'] += 1
        await self.send(chat_id, f"💡 <b>Подсказка {shown + 1}:</b> {rich(stage['hints'][shown])}\n\n"
                        '<i>Подсказка стоит одну звезду этапа.</i>', self.stage_buttons(run))

    async def side_question(self, chat_id, p, text):
        run = p['quest']
        stage = run['quest']['stages'][run['stage']]
        context = p['history'][-4:] + [{'role': 'user', 'content':
            f"Я прохожу квест по теме «{run['quest'].get('topic', '')}». Условие этапа: {stage['scene']} "
            f"{stage['question']}\nМой вопрос (не называй готовый ответ этапа, только помоги понять): {text}"}]

        async def finish(result):
            return rich(result) + '\n\n👉 Возвращайся к ответу, когда будешь готов!', self.stage_buttons(run)
        await self.write(chat_id, '🤔 Сейчас объясню…', self.ai.explain_stream(p, context), finish)

    async def complete_stage(self, chat_id, p, solved):
        run = p['quest']
        stages = run['quest']['stages']
        stage = stages[run['stage']]
        topic = run['quest'].get('topic', '')
        replay = run.get('replay')
        quality = 1.0 if solved and not run['tries'] and not run['hints'] else (0.5 if solved else 0.0)
        if solved:
            got = max(1, 3 - run['tries'] - run['hints'])
            xp = 0 if replay else game.xp_for(p, 5 + 5 * got)
            text = f"{pick(T.CORRECT)} {rich(stage['success'])}"
            if run['tries'] or run['hints']:
                text += f"\n\n🧩 <b>Как это решается:</b>\n{rich(stage['explain'])}"
        else:
            got, xp = 0, 0 if replay else 2
            text = (f"🧩 <b>Разберём вместе.</b>\n{rich(stage['explain'])}\n\n"
                    f"✅ Ответ: <b>{plain(stage['answer'])}</b>\n\n"
                    f"Сюжет идёт дальше: {rich(stage['success'])}")
            game.mark_finished(p, run.get('marker'))  # ответ показан, повтор уже не в зачёт
        text += f'\n\n{game.stars(got)}' + (f' · +{xp} XP' if xp else '')
        events = []
        if not replay:
            # повтор не трогает ни статистику, ни серию: ответы уже известны
            game.record_check(p, topic, quality)
            self.log(p, 'stage', topic, quality)
            events = self.xp(p, xp) + game.bump(p, 'stages')
            if solved:
                events += game.touch_streak(p)  # серию продлевает только то, что решил сам
            stats = game.ensure(p)['stats']
            stats['first_try_row'] = stats.get('first_try_row', 0) + 1 if quality == 1 else 0
            if stats['first_try_row'] >= 10:
                events += game.unlock_secret(p, 'no_hints')
        run['stars'].append(got)
        run['xp'] += xp
        run.update(stage=run['stage'] + 1, tries=0, hints=0, removed=[])
        await self.send(chat_id, text + events_text(events))
        if run['stage'] < len(stages):
            return await self.show_stage(chat_id, p)
        await self.finish_quest(chat_id, p)

    async def finish_quest(self, chat_id, p):
        run = p['quest']
        quest = run['quest']
        topic = quest.get('topic', '')
        total, best = sum(run['stars']), 3 * len(quest['stages'])
        fraction = total / best if best else 0
        mode, replay = run['mode'], run.get('replay')
        g = game.ensure(p)
        game.mark_finished(p, run.get('marker'))
        # засчитываем, только если сам решил больше половины этапов, а не посмотрел разбор
        own = not replay and 2 * sum(1 for s in run['stars'] if s) > len(quest['stages'])
        bonus = game.xp_for(p, {'daily': 30, 'practice': 5}.get(mode, 20)) if own else 0
        events = self.xp(p, bonus)
        if mode == 'daily':
            self.prepare_tomorrow(p)
            if own:
                g['daily_day'] = game.today().isoformat()
                events += game.bump(p, 'daily')
                self.log(p, 'daily_done', topic, fraction)
            else:
                # ответ этой задачи уже известен, для зачёта нужна похожая
                g['daily_seen'], g['daily_topic'] = game.today().isoformat(), topic
        if own:
            game.record_quest(p, topic)
        if mode == 'quest' and own:
            events += game.bump(p, 'quests')
            if total == best:
                events += game.bump(p, 'flawless')
            if run['level'] == 3:
                events += game.bump(p, 'hard_quests')
                if total == best:
                    events += game.unlock_secret(p, 'supernova')
            if run['shared']:
                events += game.bump(p, 'shared')
                self.log(p, 'shared_play', topic, fraction)
        if run.get('assignment'):
            self.store.save_result(run['assignment'], p.get('uid'), total, best, keep_first=replay)
            if not replay:
                self.log(p, 'assignment_done', topic, fraction)
        if not replay:
            self.log(p, 'quest_done', topic, fraction)
            events += game.record_result(p, fraction)
        if replay:
            title = '🔁 <b>Пройдено ещё раз</b>'
        elif own:
            title = {'daily': '🗓 <b>Задача дня решена!</b>', 'practice': '⚡ <b>Задача решена!</b>'}.get(
                mode, '🏁 <b>Квест пройден!</b>')
        else:
            title = {'daily': '🧩 <b>Задачу дня разобрали вместе</b>', 'practice': '🧩 <b>Задачу разобрали вместе</b>'}.get(
                mode, '🧩 <b>Квест разобрали вместе</b>')
        earned = f"💎 +{run['xp'] + bonus} XP\n" if run['xp'] + bonus else ''
        text = (f"{title}\n\n{rich(quest['finale'])}\n\n🧠 <b>Что ты прокачал:</b>\n{rich(quest['lesson'])}\n\n"
                f"{'⭐' * total}{'☆' * (best - total)}  {total}/{best}\n"
                f"{earned}{game.progress_line(game.ensure(p)['xp'])}" + events_text(events))
        buttons = self.kb(p)
        if mode == 'daily' and own:
            text += f"\n\n🔥 Серия: {game.current_streak(p)} · новая задача дня — завтра!"
        elif mode == 'daily':
            text += ('\n\nВ зачёт идут задачи, решённые самому, поэтому эта пока не засчитана. '
                     'Реши похожую — и задача дня будет на тебе.')
            buttons = keyboard([[('🔁 Похожая задача', 'daily:again')], [('🏠 На главную', 'home')]])
        elif replay:
            text += '\n\nЗа повтор опыт и награды не начисляются. Новый квест на эту тему — «🎮 Квест по теме».'
        elif not own:
            text += ('\n\nКвест засчитывается, если сам решил больше половины этапов. '
                     'Теперь, когда решение понятно, попробуй ещё один на эту тему.')
        if run.get('assignment'):
            text += ('\n\n🏫 Учитель видит результат первой попытки.' if replay
                     else '\n\n🏫 Результат отправлен учителю.')
        elif run['code'] and not run['shared'] and mode == 'quest' and own:
            text += (f"\n\n🔗 <b>Брось вызов другу!</b> Код квеста: <code>{run['code']}</code>\n"
                     f"Друг пишет мне /play {run['code']}")
            if self.username:
                text += f"\nили открывает ссылку: https://max.ru/{self.username}?start={run['code']}"
        steps = ' '.join(f"Этап {i + 1}: {s['question']} Решение: {s['explain']}"
                         for i, s in enumerate(quest['stages']))
        p['history'] = (p['history'] + [
            {'role': 'user', 'content': f"Я прошёл квест «{quest['title']}» по теме «{topic}»."},
            {'role': 'assistant', 'content': (quest['lesson'] + '\n' + steps)[:3000]}])[-16:]
        p.update(quest=None, state='ready')
        await self.banner(chat_id, 'levelup' if game.leveled_up(events) else 'success', text, buttons)

    def prepare_tomorrow(self, p):
        self.prepare_daily(p, game.today().fromordinal(game.today().toordinal() + 1))

    async def play(self, chat_id, p, code):
        code = code.strip().upper()
        quest, author = self.store.load_quest(code) if re.fullmatch(r'[A-Z0-9]{4,8}', code) else (None, None)
        if not quest:
            return await self.send(chat_id, f'🔍 Квест с кодом «{h(code)}» не нашёлся. Проверь код — '
                                   'он из 6 символов, например <code>K7M2QX</code>.')
        reset_topic(p)
        # квест друга - только тот, что собрал другой человек; квесты из общего запаса автора не имеют
        shared = author is not None and author != p.get('uid')
        if shared:
            self.store.played(code)
            await self.send(chat_id, '🤝 <b>Квест от друга!</b> Посмотрим, кто наберёт больше звёзд ⭐')
        await self.begin_run(chat_id, p, quest, code, 'quest', shared=shared)

    def daily_topic(self, p, day):
        # через день повторяем слабую тему, в остальные дни берём тему из программы класса
        weak = game.weakest_topic(p)
        if weak and day.toordinal() % 2 == 0:
            return weak, True
        pool = T.TOPIC_POOL.get(p['group']) or T.TOPIC_POOL['8–9 класс']
        return pool[(zlib.crc32(str(p.get('uid')).encode()) + day.toordinal()) % len(pool)], False

    async def pick_daily(self, p, day, chat_id=None):
        # из запаса, если есть, иначе собираем. Если фон успел раньше, остаётся его задача
        uid, group, level = p.get('uid'), p['group'], game.ensure(p)['difficulty']
        topic, review = self.daily_topic(p, day)
        found = self.from_pool(group, level, p['played'], 1, topic=topic if review else None)
        if found:
            code = found[0]
        else:
            try:
                if chat_id is None:
                    quest = await self.ai.create_quest(p, topic, '', 1)
                else:
                    async with self.progress(chat_id, T.QUEST_STEPS):
                        quest = await self.ai.create_quest(p, topic, '', 1)
                source = 'ai'
            except FilterError:
                raise
            except AIError:
                quest = self.offline_quest(p, 1)
                if not quest:
                    raise
                source = 'offline'
            code = self.store.save_quest(quest, uid, group, level, source)
        return self.store.daily_set(uid, day.isoformat(), code)

    async def daily(self, chat_id, p):
        g = game.ensure(p)
        day = game.today()
        if game.is_today(g['daily_day']):
            streak = game.current_streak(p)
            return await self.send(chat_id, f'✅ Задача дня уже решена! 🔥 Серия: {streak} {game.days_word(streak)}.\n\n'
                                   'Новая будет завтра. А пока можно взять случайный квест.', self.kb(p))
        if game.is_today(g['daily_seen']):
            return await self.similar_daily(chat_id, p)
        code = self.store.daily_get(p.get('uid'), day.isoformat()) or await self.pick_daily(p, day, chat_id)
        quest, _ = self.store.load_quest(code)
        weak = game.weakest_topic(p)
        reset_topic(p)
        if weak and game.topic_key(quest.get('topic')) == game.topic_key(weak):
            percent = round(game.mastery(g['topics'][game.topic_key(weak)]) * 100)
            await self.send(chat_id, f'🔁 <b>Сегодня повторяем</b> «{h(weak)}»: сейчас там {percent}%.')
        await self.begin_run(chat_id, p, quest, code, 'daily')
        self.schedule_refill(p['group'], g['difficulty'], 3, p['played'])

    async def similar_daily(self, chat_id, p):
        # ответ сегодняшней уже показан, для зачёта даём похожую (та же тема, другие числа).
        # больше трёх в день не собираем, это токены
        g = game.ensure(p)
        day = game.today()
        if game.used_today(p, 'daily_again') >= 3:
            return await self.send(chat_id, '🗓 На сегодня похожих задач хватит, новая задача дня будет завтра. '
                                   'А тему можно потренировать в квесте.', self.kb(p))
        topic = g.get('daily_topic') or self.daily_topic(p, day)[0]
        found = self.from_pool(p['group'], g['difficulty'], p['played'], 1, topic=topic)
        if found:
            code, quest = found
        else:
            async with self.progress(chat_id, T.QUEST_STEPS):
                quest = await self.ai.create_quest(p, topic, '', 1)
            code = self.store.save_quest(quest, p.get('uid'), p['group'], g['difficulty'], 'ai')
        game.count_today(p, 'daily_again', 3)
        self.store.daily_set(p.get('uid'), day.isoformat(), code, replace=True)
        g['daily_seen'] = ''
        reset_topic(p)
        await self.send(chat_id, '🔁 <b>Похожая задача.</b> Тема та же, числа другие. Реши её сам, и задача дня засчитана.')
        await self.begin_run(chat_id, p, quest, code, 'daily')

    def prepare_daily(self, p, day):
        uid = p.get('uid')
        if self.pool_target <= 0 or not uid or not p.get('group') or self.store.daily_get(uid, day.isoformat()):
            return
        key = ('daily', uid, day.isoformat())
        if key in self.refilling:
            return
        self.refilling.add(key)
        snapshot = json.loads(json.dumps(p))

        async def job():
            BACKGROUND.set(True)
            try:
                await self.pick_daily(snapshot, day)
            except Exception as exc:
                LOG.warning('Задача дня заранее не собрана: %s', type(exc).__name__)
            finally:
                self.refilling.discard(key)
        task = asyncio.create_task(job())
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def random_quest(self, chat_id, p):
        pool = T.TOPIC_POOL.get(p['group']) or T.TOPIC_POOL['8–9 класс']
        topic = random.choice(pool)
        reset_topic(p)
        p['topic'] = topic
        await self.start_quest(chat_id, p, topic, pool_ok=True)


    @staticmethod
    def hp(boss):
        return '❤️' * (3 - boss['score']) + '🖤' * boss['score']

    async def start_boss(self, chat_id, p):
        name = pick(T.BOSSES)
        topic = p['topic'] or 'нашей темы'
        await self.banner(chat_id, 'boss', f'⚔️ <b>Битва с боссом!</b>\n\nНа арену выходит <b>{h(name)}</b> — '
                          f'страж темы «{h(topic)}». Сложность: {game.difficulty_label(p)}.\n'
                          'Три вопроса — три удара. Попади хотя бы дважды — и победа твоя!')
        async with self.progress(chat_id, T.BOSS_STEPS):
            questions = await self.ai.quiz(p, self.context(p))
        # опыт за боссов по одной теме - до трёх битв в день, дальше просто тренировка
        rewarded = game.count_today(p, 'boss:' + game.topic_key(p['topic']), 3)
        p['boss'] = {'version': 3, 'id': uuid.uuid4().hex[:8], 'name': name, 'topic': p['topic'],
                     'questions': questions, 'index': 0, 'score': 0, 'rewarded': rewarded}
        p.update(state='boss', quest=None)
        if not rewarded:
            await self.send(chat_id, '🏋️ С боссами этой темы ты сегодня уже бился трижды, так что эта битва '
                            'тренировочная: без опыта и наград.')
        await self.show_boss_question(chat_id, p)

    async def show_boss_question(self, chat_id, p):
        boss = p['boss']
        q = boss['questions'][boss['index']]
        text = (f"{h(boss['name'])}  {self.hp(boss)}\n\n<b>Удар {boss['index'] + 1} из 3</b>\n{plain(q['question'])}\n\n"
                + '\n'.join(f'{NUMBERS[i]} {plain(o)}' for i, o in enumerate(q['options'])))
        await self.send(chat_id, text, keyboard([
            [(str(i + 1), f"answer:{boss['id']}:{boss['index']}:{i}") for i in range(len(q['options']))],
            [('🏃 Сбежать', 'mode:stop')]]))

    async def answer_boss(self, chat_id, p, index):
        boss = p['boss']
        q = boss['questions'][boss['index']]
        if index >= len(q['options']):
            # вариантов бывает и три: "4" в таком вопросе просто не засчитываем
            return await self.send(chat_id, f"⚔️ Тут варианты от 1 до {len(q['options'])}.")
        correct = index == q['correct']
        rewarded = boss.get('rewarded', True)
        game.record_check(p, boss.get('topic', ''), 1.0 if correct else 0.0)
        events = []
        if correct:
            boss['score'] += 1
            xp = game.xp_for(p, 10) if rewarded else 0
            events = self.xp(p, xp) + game.touch_streak(p)
            text = f"{pick(T.HIT)} {h(boss['name'])}: {self.hp(boss)}\n\n{plain(q['explanation'])}"
            if xp:
                text += f'\n\n+{xp} XP'
        else:
            text = (f"{pick(T.MISS)}\n\nПравильный ответ: {NUMBERS[q['correct']]} {plain(q['options'][q['correct']])}\n\n"
                    f"{plain(q['explanation'])}")
        await self.send(chat_id, text + events_text(events))
        boss['index'] += 1
        if boss['index'] < 3:
            return await self.show_boss_question(chat_id, p)
        score = boss['score']
        stats = game.ensure(p)['stats']
        events = []
        # в достижения идёт одна победа над боссом темы в день, иначе их набивают реваншами
        counted = score >= 2 and boss.get('rewarded', True) and game.first_today(p, 'boss', boss.get('topic', ''))
        if counted:
            if stats.get('boss_lost_last'):
                events += game.unlock_secret(p, 'comeback')
            events += game.bump(p, 'bosses')
        if score == 3:
            head = f"👑 <b>Идеальная победа!</b> {h(boss['name'])} повержен без единого промаха."
            if counted:
                events += self.xp(p, game.xp_for(p, 20)) + game.bump(p, 'boss_perfect')
        elif score == 2:
            head = f"🏆 <b>Победа!</b> {h(boss['name'])} повержен."
            if counted:
                events += self.xp(p, game.xp_for(p, 10))
        else:
            head = f"😤 <b>{h(boss['name'])} устоял…</b> Но теперь ты знаешь его слабые места. Реванш?"
        stats['boss_lost_last'] = 0 if score >= 2 else 1
        self.log(p, 'boss_done', boss.get('topic', ''), score / 3)
        events += game.record_result(p, score / 3)
        p.update(state='ready', boss=None)
        await self.banner(chat_id, 'success' if score >= 2 else 'boss',
            f"{head}\n\nПопаданий: {score} из 3\n{game.progress_line(game.ensure(p)['xp'])}" + events_text(events),
            keyboard([[('🔁 Реванш', 'go:boss'), ('🎮 Квест', 'go:quest')],
                      [('🎓 Объясню сам', 'go:teach'), ('✨ Новая тема', 'new')]]))


    async def start_teach(self, chat_id, p):
        p['state'] = 'teach'
        topic = p['topic'] or 'нашу тему'
        who = ('коллега-новичок' if p['group'] in ('10–11 класс', 'Взрослый', T.TEACHER_GROUP)
               else 'младшеклассник')
        await self.banner(chat_id, 'review', '🎓 <b>Режим «Учитель»</b>\n\n'
            f'Меняемся местами! Теперь я — {who}, который ничего не знает про «{h(topic)}».\n\n'
            'Объясни своими словами, как другу. С примером — ещё лучше 😉 '
            'Я оценю, насколько понятно и полно получилось.\n\n/cancel — выйти')

    async def teach_answer(self, chat_id, p, text):
        # оцениваем только своё объяснение: не мой же текст и не пару слов
        said = [m['content'] for m in p['history'] if m['role'] == 'assistant']
        if copied_share(text, said) > 0.5:
            return await self.send(chat_id, '🙈 Это почти слово в слово мой текст. Объясни своими словами, '
                                   'как другу: так и запоминается лучше.\n\n/cancel — выйти')
        if len(re.findall(r'\w+', text)) < 4:
            return await self.send(chat_id, '✍️ Маловато, чтобы я понял. Расскажи хотя бы парой предложений, '
                                   'можно с примером.\n\n/cancel — выйти')
        history = self.context(p) + [{'role': 'user', 'content': text}]
        async with self.progress(chat_id, T.TEACH_LOADING):
            result = await self.ai.teach(p, history, p['topic'] or 'тема разговора')
        score = result['score']
        # опыт за одну тему - первые три объяснения за день, достижение - одно в день
        rewarded = game.count_today(p, 'teach:' + game.topic_key(p['topic']), 3)
        xp = game.xp_for(p, 5 + score // 5) if rewarded else 0
        events = self.xp(p, xp) + game.bump(p, 'teach')
        if score >= 50:
            events += game.touch_streak(p)
        if score >= 80 and rewarded and game.first_today(p, 'teach', p['topic']):
            events += game.bump(p, 'teach_good')
            if score >= 90:
                events += game.bump(p, 'teach_top')
        game.record_check(p, p['topic'], score / 100)
        self.log(p, 'teach', p['topic'], score / 100)
        reply = (f"🎓 <b>Понимание темы: {score}%</b>\n{game.bar(score / 100)}\n\n"
                 f"👍 {rich(result['praise'])}\n\n🔧 {rich(result['fix'])}\n\n"
                 f"🤔 <i>{plain(result['question'])}</i>")
        reply += f'\n\n💎 +{xp} XP' if xp else '\n\n<i>Опыт за эту тему на сегодня уже начислен.</i>'
        await self.send(chat_id, reply + events_text(events), self.kb(p))
        p['history'] = (history + [{'role': 'assistant', 'content':
            f"{result['praise']}\n{result['fix']}\n{result['question']}"}])[-16:]
        p['state'] = 'ready'


    async def class_menu(self, chat_id, p, argument=''):
        if argument:
            return await self.join_class(chat_id, p, argument)
        if self.is_teacher(p):
            classes = self.store.teacher_classes(p.get('uid'))
            lines = ['🏫 <b>Мои классы</b>\n']
            lines += [f"• <b>{h(c['name'])}</b> ({h(c['grp'])}) — код <code>{c['code']}</code>, учеников: {c['size']}"
                      for c in classes] or ['Пока нет ни одного класса. Создайте первый — это 10 секунд.']
            rows = [[(f"📋 {c['name']}", f"cls:open:{c['code']}")] for c in classes]
            rows += [[('➕ Создать класс', 'cls:new')], [self.cabinet_button(p)]]
            return await self.banner(chat_id, 'teacher', '\n'.join(lines), keyboard(rows))
        classes = self.store.member_classes(p.get('uid'))
        if classes:
            text = '🏫 <b>Твои классы</b>\n\n' + '\n'.join(f"• {h(c['name'])}" for c in classes) + \
                   '\n\nЗадания учителя приходят сюда, в чат. Рейтинг класса — в кабинете.'
        else:
            text = ('🏫 <b>Класс</b>\n\nЕсли учитель дал код класса — нажми кнопку и введи его. '
                    'Тогда задания учителя будут приходить прямо сюда.')
        rows = [[('🔑 Вступить по коду', 'cls:join')]]
        rows += [[(f"🚪 Выйти из «{c['name']}»", f"cls:leave:{c['code']}")] for c in classes]
        rows.append([self.cabinet_button(p)])
        await self.send(chat_id, text, keyboard(rows))

    async def class_card(self, chat_id, p, code):
        info = self.store.get_class(code)
        if not info or info['teacher'] != p.get('uid'):
            return await self.send(chat_id, '🔍 Класс не найден.')
        data = web.class_data(self.store, code)
        invite = f'https://max.ru/{self.username}?start=class_{code}' if self.username else ''
        lines = [f"🏫 <b>{h(info['name'])}</b> · {h(info.get('grp', ''))}",
                 f"Код: <code>{code}</code> · учеников: {len(data['students'])}", '',
                 '<b>Как пригласить учеников:</b> отправьте им в чат класса',
                 f'«Напиши боту Люми /class {code}»' + (f' или ссылку {invite}' if invite else '') + '.']
        if data['assignments']:
            lines += ['', '<b>Последние задания:</b>']
            lines += [f"• {h(a['topic'])}: выполнили {a['done']}/{a['total']}"
                      + (f", средний результат {a['avg']}%" if a['avg'] is not None else '') for a in data['assignments'][:5]]
        await self.send(chat_id, '\n'.join(lines), keyboard([
            [('📝 Задать квест классу', f'cls:assign:{code}')], [('📊 Отчёт', f'cls:report:{code}')],
            [self.cabinet_button(p)], [('⬅️ Мои классы', 'go:class')]]))

    async def class_report(self, chat_id, p, code):
        info = self.store.get_class(code)
        if not info or info['teacher'] != p.get('uid'):
            return await self.send(chat_id, '🔍 Класс не найден.')
        data = web.class_data(self.store, code)
        st = data['students']
        active = sum(1 for s in st if s['last_seen'] and s['last_seen'] > time.time() - 7 * 86400)
        lines = [f"📊 <b>Отчёт: {h(info['name'])}</b>", f'Учеников: {len(st)} · активны за неделю: {active}', '']
        if st:
            lines.append('<b>Топ недели по опыту:</b>')
            lines += [f"{i + 1}. {h(s['name'])} — {s['xp_week']} XP, квестов {s['quests']}" for i, s in enumerate(st[:5])]
            help_needed = [s for s in st if s['weak']]
            if help_needed:
                lines += ['', '<b>Кому нужна помощь:</b>']
                lines += [f"• {h(s['name'])}: {h(s['weak'][0]['name'])} — {s['weak'][0]['mastery']}%" for s in help_needed[:5]]
        for a in data['assignments'][:3]:
            missing = [s['name'] for s in st if str(s['id']) not in a['results']]
            lines += ['', f"📝 <b>{h(a['topic'])}</b>: {a['done']}/{a['total']}"
                      + (f", средний результат {a['avg']}%" if a['avg'] is not None else '')]
            if missing:
                lines.append('Ещё не прошли: ' + h(', '.join(missing[:10])) + ('…' if len(missing) > 10 else ''))
        if not st:
            lines.append('В классе пока никого. Отправьте ученикам код.')
        await self.send(chat_id, '\n'.join(lines), keyboard([[self.cabinet_button(p, '🌐 Подробно в кабинете')],
                                                             [('⬅️ К классу', f'cls:open:{code}')]]))

    async def create_class(self, chat_id, p, grade_key):
        name = p.pop('draft_class', '') or 'Мой класс'
        group = GROUPS.get(grade_key, '')
        code = self.store.create_class(p.get('uid'), name, group)
        p['state'] = 'ready'
        self.log(p, 'class_created', value=1)
        await self.menu(chat_id, f'✅ Класс <b>{h(name)}</b> ({h(group)}) создан! Код: <code>{code}</code>')
        await self.class_card(chat_id, p, code)

    async def join_class(self, chat_id, p, code):
        code = code.strip().upper().removeprefix('CLASS_')
        info = self.store.get_class(code) if re.fullmatch(r'[A-Z0-9]{4,8}', code) else None
        if not info:
            return await self.send(chat_id, f'🔍 Класс с кодом «{h(code)}» не найден. Проверь код у учителя.')
        if info['teacher'] == p.get('uid'):
            return await self.class_card(chat_id, p, code)
        already = code in {c['code'] for c in self.store.member_classes(p.get('uid'))}
        self.store.join_class(code, p.get('uid'))
        p['state'] = 'ready'
        if already:
            return await self.send(chat_id, f"🏫 Ты уже в классе «{h(info['name'])}».")
        self.log(p, 'join_class', value=1)
        teacher = self.store.profiles([info['teacher']]).get(info['teacher'], {})
        await self.send(chat_id, f"🎉 Ты в классе <b>{h(info['name'])}</b>"
                        + (f" — учитель {h(teacher.get('nick') or '')}" if teacher.get('nick') else '') + '!\n\n'
                        'Задания учителя будут приходить сюда. Учитель видит твоё имя из MAX, '
                        'уровень и результаты заданий класса — больше ничего.', self.kb(p))
        name = p.get('name') or p.get('nick') or 'ученик'
        await self.notify(info['teacher'], f"👋 В класс «{h(info['name'])}» вступил(а) {h(name)}.")

    async def assign(self, chat_id, p, code, topic):
        info = self.store.get_class(code)
        p['state'] = 'ready'
        if not info or info['teacher'] != p.get('uid'):
            return await self.send(chat_id, '🔍 Класс не найден.')
        profile = new_profile()
        profile.update(group=info.get('grp') or '8–9 класс', filters=p['filters'])
        async with self.progress(chat_id, T.QUEST_STEPS):
            quest = await self.ai.create_quest(profile, topic[:60], '', 3)
        quest_code = self.store.save_quest(quest, p.get('uid'), profile['group'], 2, 'teacher')
        assignment = self.store.add_assignment(code, quest_code, quest.get('topic') or topic)
        self.log(p, 'assignment_created', topic, 1)
        members = self.store.members(code)
        delivered = 0
        for uid in members:
            delivered += await self.notify(uid, f"🏫 <b>Задание от учителя</b> · {h(info['name'])}\n\n"
                                           f"{h(quest['emoji'])} <b>{plain(quest['title'])}</b>\nТема: {h(quest.get('topic', topic))}",
                                           keyboard([[('▶️ Начать квест', f'asg:{assignment}')]]))
        preview = '\n'.join(f"{i + 1}. {plain(s['question'])} → <b>{plain(s['answer'])}</b>" for i, s in enumerate(quest['stages']))
        await self.send(chat_id, f"✅ <b>Квест «{plain(quest['title'])}» отправлен</b>: {delivered} из {len(members)} учеников.\n\n"
                        f"<b>Этапы и ответы (видите только вы):</b>\n{preview}\n\n"
                        'Результаты появятся в отчёте и в кабинете учителя.',
                        keyboard([[('📊 Отчёт', f'cls:report:{code}')], [self.cabinet_button(p)]]))

    async def start_assignment(self, chat_id, p, assignment_id):
        a = self.store.assignment(assignment_id)
        if not a or a['class'] not in {c['code'] for c in self.store.member_classes(p.get('uid'))}:
            return await self.send(chat_id, '🔍 Задание не найдено — возможно, ты не в этом классе.')
        quest, _ = self.store.load_quest(a['quest'])
        if not quest:
            return await self.send(chat_id, '🔍 Квест задания не найден.')
        reset_topic(p)
        await self.begin_run(chat_id, p, quest, a['quest'], 'quest', shared=True, assignment=a['id'])


    MODE_COMMANDS = ('/quest', '/random', '/daily', '/practice', '/boss', '/teach', '/why', '/analyze', '/play')

    async def command(self, chat_id, p, command, argument=''):
        if command in self.MODE_COMMANDS + ('/class',) and p['state'] in ('quest', 'boss'):
            return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
        if command in self.MODE_COMMANDS + ('/class',) and p['state'] not in ('ready', 'quest_topic', 'analyze_input'):
            p['state'] = 'ready'
        if command == '/quest':
            if argument:
                reset_topic(p)
                p['topic'] = argument[:60]
                return await self.start_quest(chat_id, p, argument[:60])
            topic = self.topic_for_quest(p)
            card = p.get('card') or {}
            if topic and card.get('topic') == p['topic'] and card.get('quests'):
                idea = card['quests'][card.get('next', 0) % len(card['quests'])]
                card['next'] = card.get('next', 0) + 1
                return await self.start_quest(chat_id, p, topic, setting=f"{idea['title']}. {idea['pitch']}")
            if topic:
                return await self.start_quest(chat_id, p, topic)
            p['state'] = 'quest_topic'
            return await self.send(chat_id, '🎮 <b>Какую тему превратим в квест?</b>\n\n'
                'Напиши, например: «дроби», «закон Ома», «вероятность».', keyboard([[('🎲 Удиви меня', 'go:random')]]))
        if command == '/random':
            return await self.random_quest(chat_id, p)
        if command == '/daily':
            return await self.daily(chat_id, p)
        if command == '/class':
            return await self.class_menu(chat_id, p, argument)
        if command == '/play':
            if not argument:
                return await self.send(chat_id, '🔗 Напиши код квеста от друга: <code>/play K7M2QX</code>')
            return await self.play(chat_id, p, argument)
        if command == '/analyze':
            if argument:
                return await self.analyze(chat_id, p, argument)
            p['state'] = 'analyze_input'
            return await self.send(chat_id, '🔎 <b>Разберём задание вместе!</b>\n\n'
                'Пришли текст, непонятный абзац или одно фото задания — подойдёт и скриншот.\n\n'
                'Сначала скажу простыми словами, о чём речь, потом пройдём по шагам.\n/cancel — назад')
        if command == '/stats':
            if p.get('uid') not in self.admins:
                return await self.send(chat_id, T.UNKNOWN_COMMAND)
            return await self.send(chat_id, analytics.summary(analytics.metrics(self.store)),
                                   keyboard([[self.cabinet_button(p, page='admin')]]))
        if command not in self.MODE_COMMANDS:
            return await self.send(chat_id, T.UNKNOWN_COMMAND)
        if not p['history'] and not p['topic']:
            return await self.send(chat_id, T.NEED_TOPIC, self.kb(p))
        if command == '/practice':
            return await self.start_quest(chat_id, p, self.topic_for_quest(p), stages=1, mode='practice')
        if command == '/boss':
            return await self.start_boss(chat_id, p)
        if command == '/teach':
            return await self.start_teach(chat_id, p)
        if command == '/why':
            return await self.why(chat_id, p)

    async def stop_mode(self, chat_id, p, text='↩️ Вышли из режима. Пиши тему или вопрос!'):
        was_quest = p['state'] == 'quest'
        p.update(state='ready', quest=None, boss=None)
        if was_quest:
            text = '🚪 Вышли из квеста. Опыт за пройденные этапы остаётся у тебя 💎'
        await self.send(chat_id, text, self.kb(p))

    async def start(self, chat_id, p, payload=''):
        code = (payload or '').strip().upper()
        p.update(quest=None, boss=None)
        if not self.onboarded(p):
            if code.startswith('CLASS_'):
                p['pending_class'] = code[6:]
            elif code:
                p['pending_play'] = code
            await self.banner(chat_id, 'welcome', T.WELCOME)
            if p['state'] not in ('interests', 'nick'):
                p['state'] = 'group' if not p['group'] else ('interests' if not p['interests_done'] else 'nick')
            return await self.setup(chat_id, p)
        p['state'] = 'ready'
        if code.startswith('CLASS_'):
            return await self.join_class(chat_id, p, code[6:])
        if code:
            return await self.play(chat_id, p, code)
        g = game.ensure(p)
        self.schedule_refill(p['group'], g['difficulty'], 3, p['played'])
        self.prepare_daily(p, game.today())
        await self.home(chat_id, p, greeting=f"👋 <b>С возвращением, {h(p['nick'] or 'герой')}!</b>")

    async def new_topic(self, chat_id, p):
        reset_topic(p)
        ideas = ', '.join(f'«{t.lower()}»' for t in random.sample(
            T.TOPIC_POOL.get(p['group']) or T.TOPIC_POOL['8–9 класс'], 3))
        await self.send(chat_id, '✍️ <b>Какую тему разберём?</b>\n\nНапиши её своими словами или задай вопрос. '
                        f'Например: {h(ideas)} или «зачем нужны логарифмы?»',
                        keyboard([[('🏠 На главную', 'home')]]))

    async def text(self, chat_id, p, text):
        parts = text.split(maxsplit=1)
        command = parts[0].lower().split('@')[0]
        command = T.ALIASES.get(command, command)
        argument = parts[1].strip() if len(parts) > 1 else ''
        if command == '/help':
            return await self.send(chat_id, T.HELP, self.kb(p))
        if command == '/start':
            return await self.start(chat_id, p, argument)
        if command == '/cancel':
            if not self.onboarded(p):
                return await self.setup(chat_id, p)
            return await self.stop_mode(chat_id, p)
        if not self.onboarded(p):
            if not text.startswith('/'):
                if p['state'] == 'interests':
                    return await self.add_interest(chat_id, p, text)
                if p['state'] == 'nick':
                    return await self.set_nick(chat_id, p, text)
            return await self.setup(chat_id, p)
        if command == '/new':
            return await self.new_topic(chat_id, p)
        if command == '/me':
            return await self.hero_card(chat_id, p)
        if command == '/menu':
            return await self.home(chat_id, p)
        if command == '/filter':
            if p['state'] in ('quest', 'boss'):
                return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
            return await (self.add_filter(chat_id, p, argument) if argument else self.filter_prompt(chat_id, p))
        if text.startswith('/'):
            return await self.command(chat_id, p, command, argument)
        state = p['state']
        if state == 'interests':
            return await self.add_interest(chat_id, p, text)
        if state == 'nick':
            return await self.set_nick(chat_id, p, text)
        if state == 'group':
            return await self.setup(chat_id, p)
        if state == 'filter':
            return await self.add_filter(chat_id, p, text)
        if state == 'analyze_input':
            return await self.analyze(chat_id, p, text)
        if state == 'quest':
            return await self.answer_stage(chat_id, p, text)
        if state == 'quest_setting':
            p['state'] = 'ready'
            return await self.start_quest(chat_id, p, self.topic_for_quest(p) or text, setting=text[:300])
        if state == 'quest_topic':
            reset_topic(p)
            p['topic'] = text[:60]
            return await self.start_quest(chat_id, p, text[:60])
        if state == 'boss':
            if text.strip() in ('1', '2', '3', '4'):
                return await self.answer_boss(chat_id, p, int(text.strip()) - 1)
            return await self.send(chat_id, '⚔️ Бей кнопкой или цифрой от 1 до 4!\n\n/cancel — сбежать с арены')
        if state == 'teach':
            return await self.teach_answer(chat_id, p, text)
        if state == 'class_name':
            p['draft_class'] = ' '.join(text.split())[:60]
            p['state'] = 'class_grade'
            rows = [[(label, f'cls:grade:{key}')] for key, label in GROUPS.items()]
            return await self.send(chat_id, '🎒 Для какого возраста класс? От этого зависит сложность квестов.', keyboard(rows))
        if state == 'join_code':
            return await self.join_class(chat_id, p, text)
        if state.startswith('assign:'):
            return await self.assign(chat_id, p, state.split(':', 1)[1], text)
        await self.chat_message(chat_id, p, text)

    async def callback(self, chat_id, p, payload):
        if payload.startswith('group:'):
            key = payload.split(':')[1]
            if p['state'] != 'group' or (key not in GROUPS and key != 'teacher'):
                return
            if key == 'teacher':
                p.update(group=T.TEACHER_GROUP, role='teacher', interests_done=True)
            else:
                p.update(group=GROUPS[key], role='student')
            reset_topic(p)
            p['state'] = 'interests' if not p['interests_done'] else ('nick' if not p['nick_done'] else 'ready')
            return await self.setup(chat_id, p)
        if payload.startswith('interest:') and p['state'] == 'interests':
            item = T.INTERESTS.get(payload.split(':')[1])
            if item:
                name = item[1]
                if name in p['interests']:
                    p['interests'].remove(name)
                elif len(p['interests']) < 12:
                    p['interests'].append(name)
            return await self.setup(chat_id, p)
        if payload == 'interests:done' and p['state'] == 'interests':
            p['interests_done'] = True
            reset_topic(p)
            p['state'] = 'nick' if not p['nick_done'] else 'ready'
            return await self.setup(chat_id, p)
        if payload.startswith('nick:') and p['state'] == 'nick':
            choice = payload.split(':')[1]
            ideas = p.get('nick_ideas') or []
            if choice == 'skip':
                p.update(nick='', nick_done=True, state='ready')
                return await self.setup(chat_id, p)
            if choice == 'more':
                return await self.setup(chat_id, p)
            if choice == 'name' and self.first_name(p):
                return await self.set_nick(chat_id, p, self.first_name(p))
            if choice.isdigit() and int(choice) < len(ideas):
                return await self.set_nick(chat_id, p, ideas[int(choice)])
            return
        if payload in ('edit:group', 'edit:interests', 'edit:nick'):
            if p['state'] in ('quest', 'boss'):
                return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
            p['state'] = payload.split(':')[1]
            return await self.setup(chat_id, p)
        if not self.onboarded(p):
            return await self.setup(chat_id, p)
        if payload == 'me:level':
            return await self.level_menu(chat_id, p)
        if payload.startswith('me:'):
            tab = payload[3:]
            return await self.hero_card(chat_id, p, 'awards' if tab == 'badges' else tab)
        if payload == 'train':
            weak = game.weakest_topic(p)
            if p['state'] in ('quest', 'boss'):
                return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
            if not weak:
                return await self.send(chat_id, '💪 Слабых тем пока нет. Попробуй «🎲 Удиви меня»!')
            reset_topic(p)
            p['topic'] = weak
            found = self.from_pool(p['group'], game.ensure(p)['difficulty'], p['played'], 1, topic=weak)
            if found:
                return await self.begin_run(chat_id, p, found[1], found[0], 'practice')
            return await self.start_quest(chat_id, p, weak, stages=1, mode='practice')
        if payload.startswith('level:'):
            level = payload.split(':')[1]
            if level in ('1', '2', '3'):
                game.ensure(p).update(difficulty=int(level), recent=[])
                return await self.level_menu(chat_id, p, chosen=True)
            return
        if payload == 'filter':
            return await self.filter_prompt(chat_id, p)
        if payload == 'filter:clear' and p['state'] == 'filter':
            p['filters'] = ''
            reset_topic(p)
            return await self.send(chat_id, '🌿 Фильтр очищен! Какую тему разберём?')
        if payload == 'new':
            if p['state'] in ('quest', 'boss'):
                return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
            return await self.new_topic(chat_id, p)
        if payload == 'mode:stop':
            if p['state'] not in ('ready', 'group', 'interests', 'nick'):
                return await self.stop_mode(chat_id, p)
            return
        if payload == 'resume':
            if p['state'] == 'quest' and p['quest']:
                return await self.show_stage(chat_id, p)
            if p['state'] == 'boss' and p['boss']:
                return await self.show_boss_question(chat_id, p)
            return
        if payload == 'go:me':
            return await self.hero_card(chat_id, p)
        if payload == 'home':
            return await self.home(chat_id, p)
        if payload == 'topic':
            if not p['topic']:
                return await self.home(chat_id, p)
            return await self.send(chat_id, f"📖 Продолжаем «{h(p['topic'])}». Спроси что-нибудь по теме "
                                   'или выбери, что делаем дальше 👇', topic_menu())
        if payload == 'daily:again':
            return await self.command(chat_id, p, '/daily')
        if payload.startswith('go:'):
            return await self.command(chat_id, p, '/' + payload[3:])
        if payload.startswith('cls:'):
            return await self.class_button(chat_id, p, payload.split(':'))
        if payload.startswith('asg:'):
            if p['state'] in ('quest', 'boss'):
                return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
            part = payload.split(':')[1]
            return await self.start_assignment(chat_id, p, int(part)) if part.isdigit() else None
        if payload == 'own':
            if p['state'] in ('quest', 'boss'):
                return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
            if not p['topic']:
                return await self.send(chat_id, T.NEED_TOPIC, self.kb(p))
            p['state'] = 'quest_setting'
            return await self.send(chat_id, '✍️ <b>Придумай мир для квеста.</b>\n\n'
                'Кто ты и чего хочешь добиться? Например: «я тренер команды по Dota», '
                '«открываю кофейню», «лечу на Марс».\n\n/cancel — назад')
        if payload.startswith('card:'):
            return await self.card_choice(chat_id, p, payload.split(':'))
        if payload.startswith('quest:'):
            return await self.quest_button(chat_id, p, payload.split(':'))
        if payload.startswith('answer:'):
            boss = p.get('boss')
            bits = payload.split(':')
            if (p['state'] == 'boss' and boss and len(bits) == 4 and bits[1] == boss['id']
                    and bits[2] == str(boss['index']) and bits[3] in ('0', '1', '2', '3')):
                await self.answer_boss(chat_id, p, int(bits[3]))
            return
        if payload.startswith('ai:'):
            if p['state'] in ('quest', 'boss'):
                return await self.send(chat_id, T.FINISH_FIRST, resume_menu())
            if not p['history'] and not p['topic']:
                return await self.send(chat_id, T.NEED_TOPIC, self.kb(p))
            prompts = {'ai:simple': ('🧠 Объясняю проще…', 'Объясни это ещё проще, с бытовым примером и без '
                                     'лишних терминов.'),
                       'ai:deeper': ('🚀 Разбираю подробнее…', 'Расскажи подробнее. Если это точные науки, то '
                                     'строгая формулировка, откуда это берётся, задача посложнее моего уровня '
                                     'по шагам и типичная ошибка.'),
                       'ai:example': ('💡 Готовлю пример…', 'Приведи ещё один пример по этой теме. Если в теме '
                                      'есть расчёты, то с числами и решением по шагам. Можно через мои '
                                      'интересы, если подходит.')}
            if payload in prompts:
                p['state'] = 'ready'
                placeholder, request = prompts[payload]
                await self.ask(chat_id, p, request, placeholder)

    async def class_button(self, chat_id, p, parts):
        action = parts[1] if len(parts) > 1 else ''
        code = parts[2] if len(parts) > 2 else ''
        if action == 'join':
            p['state'] = 'join_code'
            return await self.send(chat_id, '🔑 Напиши код класса от учителя — 5 символов, например <code>K7M2Q</code>.\n\n/cancel — назад')
        if action == 'leave' and code:
            self.store.leave_class(code, p.get('uid'))
            return await self.send(chat_id, '🚪 Ты вышел из класса. Вернуться можно по коду учителя.')
        if not self.is_teacher(p):
            return
        if action == 'new':
            p['state'] = 'class_name'
            return await self.send(chat_id, '✍️ Как назовём класс? Например, «7Б математика».\n\n/cancel — назад')
        if action == 'grade' and p['state'] == 'class_grade' and code in GROUPS:
            return await self.create_class(chat_id, p, code)
        if action == 'open' and code:
            return await self.class_card(chat_id, p, code)
        if action == 'report' and code:
            return await self.class_report(chat_id, p, code)
        if action == 'assign' and code:
            p['state'] = f'assign:{code}'
            return await self.send(chat_id, '📝 <b>Какую тему задать классу?</b>\n\nНапример: «Проценты», '
                                   '«Закон Ома», «Логарифмы». Я соберу проверенный квест и разошлю ученикам.\n\n/cancel — назад')

    async def card_choice(self, chat_id, p, parts):
        card = p.get('card')
        if len(parts) != 3 or not card or parts[1] != card['id'] or p['state'] in ('quest', 'boss'):
            return
        choice = parts[2]
        if choice == 'explain':
            return await self.ask(chat_id, p, f"Объясни тему «{card['topic']}».", '🧠 Объясняю тему…')
        if choice == 'own':
            p['state'] = 'quest_setting'
            return await self.send(chat_id, '✍️ <b>Придумай мир для квеста!</b>\n\n'
                'Кто ты и чего хочешь добиться? Например: «я тренер команды по Dota», '
                '«открываю кофейню», «лечу на Марс», «снимаю ролик для блога».\n\n/cancel — назад')
        if choice.isdigit() and int(choice) < len(card['quests']):
            q = card['quests'][int(choice)]
            return await self.start_quest(chat_id, p, card['topic'], setting=f"{q['title']}. {q['pitch']}")

    async def quest_button(self, chat_id, p, parts):
        run = p.get('quest')
        if (p['state'] != 'quest' or not run or len(parts) < 4 or parts[1] != run['id']
                or parts[2] != str(run['stage'])):
            return
        action = parts[3]
        if action == 'hint':
            return await self.hint(chat_id, p)
        if action == 'reveal':
            if not run['tries'] and not run['hints']:
                return await self.send(chat_id, '😉 Сначала попробуй хотя бы раз — даже ошибка прокачивает мозг. '
                                       'Или возьми подсказку 💡', self.stage_buttons(run))
            return await self.complete_stage(chat_id, p, solved=False)
        if action == 'pick' and len(parts) == 5 and parts[4].isdigit():
            return await self.pick_option(chat_id, p, int(parts[4]))


    async def handle(self, event, kind):
        chat_id, user_id = event.get_ids()
        if user_id is None or chat_id is None:
            return
        message = getattr(event, 'message', None)
        if message and getattr(message, 'recipient', None) and message.recipient.chat_type != 'dialog':
            return
        payload = (event.callback.payload or '') if kind == 'callback' else ''
        in_place = kind == 'callback' and payload.startswith(MENU_PREFIXES)
        if kind == 'callback' and not in_place:
            try:
                await event.answer()
            except Exception:
                pass
        command = ''
        if kind == 'text':
            command = ((getattr(message.body, 'text', '') or '').strip().split() or [''])[0].lower()
        just_show = payload in ('home', 'go:me', 'topic') or payload.startswith('me:') or command in READ_ONLY
        if just_show and self.onboarded(self.store.get(user_id)):
            # только показать: не ждём очереди и ничего не сохраняем
            return await self.process(event, kind, chat_id, user_id, payload, in_place, save=False)
        # остальные сообщения одного человека обрабатываем строго по очереди и ничего не теряем,
        # разные люди друг друга не ждут
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        if lock.locked() and user_id not in self.warned:
            self.warned.add(user_id)
            later = T.WAITING.get(payload)
            await self.safe_send(chat_id, f'⏳ Сначала допишу прошлый ответ, следом будет {later}.' if later
                                 else pick(T.BUSY))
        async with lock:
            self.warned.discard(user_id)
            self.busy.add(user_id)
            try:
                await self.process(event, kind, chat_id, user_id, payload, in_place)
            finally:
                self.busy.discard(user_id)

    async def process(self, event, kind, chat_id, user_id, payload, in_place, save=True):
        message = getattr(event, 'message', None)
        ctx = {'event': event, 'answered': False}
        token = CALLBACK.set(ctx if in_place else None)
        try:
            p = self.store.get(user_id)
            p['uid'] = user_id
            user = (event.callback.user if kind == 'callback' else
                    getattr(event, 'user', None) if kind == 'start' else getattr(message, 'sender', None))
            first = getattr(user, 'first_name', None)
            if isinstance(first, str) and first:
                last = getattr(user, 'last_name', None)
                p['name'] = (first + (' ' + last if isinstance(last, str) and last else '')).strip()[:60]
            if kind == 'start':
                await self.start(chat_id, p, getattr(event, 'payload', '') or '')
            elif kind == 'callback':
                await self.callback(chat_id, p, payload)
            else:
                text = (event.message.body.text or '').strip()
                if len(text) > 4000:
                    return await self.send(chat_id, '📝 Давай по частям! Сократи сообщение до 4000 символов.')
                url = image_attachment(event.message)
                if url:
                    await self.photo(chat_id, p, url, text)
                    if save:
                        self.store.save(user_id, p)
                    return
                if not text:
                    return await self.send(chat_id, '📝 Напиши тему, вопрос или пришли фото задания!')
                await self.text(chat_id, p, text)
            if save:
                self.store.save(user_id, p)
        except ChatClosed:
            LOG.info('Чат %s закрыт пользователем, ответ не доставлен', chat_id)
        except (PhotoError, LearningError) as exc:
            await self.safe_send(chat_id, h(str(exc)))
        except QuizError as exc:
            LOG.warning('Проверка теста: %s', exc)
            await self.safe_send(chat_id, '⚔️ Босс сбежал с арены: не получилось собрать честные вопросы с точными ответами.\n\n'
                                 'Попробуй /boss ещё раз или уточни тему.')
        except AIError as exc:
            LOG.warning('GigaChat недоступен: %s', exc)
            await self.safe_send(chat_id, '📡 Мой ИИ-мозг сейчас не отвечает. '
                                 'Попробуй ещё раз через минуту — текущий шаг сохранён.')
        except Exception as exc:
            LOG.exception('Ошибка обработки: %s', type(exc).__name__)
            await self.safe_send(chat_id, '💬 Что-то пошло не так. Отправь сообщение ещё раз — попробуем вместе!')
        finally:
            if in_place and not ctx['answered']:
                try:
                    await event.answer()
                except Exception:
                    pass
            CALLBACK.reset(token)


async def main():
    load_dotenv(Path(__file__).with_name('.env'))
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper())
    if not os.getenv('MAX_BOT_TOKEN'):
        raise SystemExit('Укажи MAX_BOT_TOKEN в .env')
    key, fixes, problem = credentials_from_env()
    if not key:
        raise SystemExit('Укажи GIGACHAT_CREDENTIALS (Authorization Key) в .env')
    if fixes:
        LOG.warning('Ключ GigaChat исправлен автоматически: %s. Лучше поправь .env', ', '.join(fixes))
    if problem:
        LOG.error('Ключ GigaChat не годится: %s', problem)
    run_lock = acquire_run_lock()
    # auto_requests=False: иначе на каждое событие библиотека делает лишний запрос GET /chats,
    # а нам хватает данных из самого события
    bot = Bot(token=os.environ['MAX_BOT_TOKEN'], auto_requests=False)
    if os.getenv('MAX_API_URL'):
        bot.set_api_url(os.environ['MAX_API_URL'])
    ai = AI()
    store = Store(os.getenv('DATABASE_PATH', 'data/bot.sqlite3'))
    app = AssistantBot(bot, store, ai, Hooks())
    if app.hooks.urls:
        LOG.info('Вебхуки: %s', ', '.join(app.hooks.urls))
    info = {'started': time.time(), 'gigachat': 'unknown', 'username': app.username}
    # по умолчанию maxapi обрабатывает события по одному, и все ждут самого долгого
    dp = Dispatcher(use_create_task=True)

    @dp.bot_started()
    async def started(event):
        await app.handle(event, 'start')

    @dp.message_created()
    async def message(event):
        await app.handle(event, 'text')

    @dp.message_callback()
    async def callback(event):
        await app.handle(event, 'callback')

    runner = None
    try:
        ok, details = await ai.health()
        info['gigachat'] = 'ok' if ok else 'error'
        if ok:
            LOG.info('GigaChat работает, %s', details)
        else:
            LOG.error('GigaChat недоступен, на темы бот будет отвечать, что ИИ не отвечает. %s', details)
        await app.prepare()
        info['username'] = app.username
        if ok:
            app.warm_up()
        port = int(os.getenv('WEB_PORT', '8080') or 0)
        if port:
            runner = await web.start(store, info, port=port)
            if not app.web_url:
                LOG.warning('WEB_BASE_URL не задан, кнопки кабинета в боте скрыты (см. README)')
        await bot.delete_webhook()
        await dp.start_polling(bot)
    finally:
        run_lock.close()
        if runner:
            await runner.cleanup()
        await app.hooks.close()
        await ai.close()
        await bot.close_session()
