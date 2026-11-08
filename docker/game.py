# Опыт, уровни, достижения, серии и сложность. Всё работает со словарём профиля,
# функции начисления возвращают строки для показа в чате ("новый уровень" и т.п.)
from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

TITLES = ['Новичок', 'Любопытный', 'Ученик', 'Исследователь', 'Следопыт', 'Изобретатель',
          'Экспериментатор', 'Аналитик', 'Навигатор', 'Стратег', 'Знаток', 'Мастер', 'Эксперт',
          'Магистр', 'Мудрец', 'Профессор', 'Гений', 'Академик', 'Легенда', 'Абсолют']
LEVEL_EMOJI = ['🐣', '🔍', '📘', '🧭', '🥾', '⚙️', '🧪', '📊', '🛰', '♟', '🦉', '🛠', '🎯', '🔮', '🧙',
               '🎓', '💡', '🏛', '🏆', '🌟']

DIFFICULTY = {1: ('🟢', 'База'), 2: ('🟡', 'Продвинутый'), 3: ('🔴', 'Профи')}
XP_MULT = {1: 1.0, 2: 1.3, 3: 1.7}

# значок, название, какой счётчик, пороги для бронзы/серебра/золота, подсказка
TIERS = ['🥉', '🥈', '🥇']
ROMAN = ['I', 'II', 'III']
BADGES = {
    'quests': ('🗺', 'Искатель приключений', 'quests', (5, 25, 75), 'пройди квесты'),
    'flawless': ('💎', 'Без единой ошибки', 'flawless', (3, 10, 30), 'пройди квест на все звёзды'),
    'hard': ('🔥', 'Профи', 'hard_quests', (3, 10, 30), 'пройди квест на сложности «Профи»'),
    'bosses': ('⚔️', 'Охотник на боссов', 'bosses', (5, 20, 60), 'побеждай боссов'),
    'boss_perfect': ('👑', 'Безупречный удар', 'boss_perfect', (3, 10, 25), 'побей босса без промахов'),
    'teacher': ('🎓', 'Наставник', 'teach_good', (3, 10, 30), 'объясни тему Люми на 80%+'),
    'daily': ('🗓', 'Ежедневный ритуал', 'daily', (7, 30, 100), 'решай задачи дня'),
    'streak': ('🌋', 'Огненная серия', 'best_streak', (5, 14, 45), 'занимайся дни подряд'),
    'erudite': ('🧠', 'Эрудит', 'topics', (3, 8, 20), 'освой темы на 80%+'),
    'curious': ('❓', 'Почемучка', 'questions', (25, 100, 300), 'задавай вопросы'),
    'photo': ('📸', 'Фотоохотник', 'photos', (3, 15, 40), 'разбирай задания по фото'),
    'friend': ('🤝', 'Командный игрок', 'shared', (3, 10, 25), 'проходи квесты друзей и класса'),
}
SECRETS = {
    'comeback': ('🦅', 'Камбэк', 'победи босса после поражения в прошлой битве'),
    'no_hints': ('🧊', 'Холодный расчёт', '10 этапов подряд с первой попытки'),
    'supernova': ('🚀', 'Сверхновая', 'пройди квест на сложности «Профи» на все звёзды'),
}

STATS = ('quests', 'flawless', 'hard_quests', 'stages', 'bosses', 'boss_perfect', 'boss_lost_last',
         'teach', 'teach_good', 'teach_top', 'questions', 'photos', 'daily', 'shared', 'first_try_row')


def today():
    return datetime.now(MSK).date()


def now():
    return datetime.now(MSK)


def new_game():
    # finished - коды квестов, которые человек уже доиграл: ответы он видел, повтор не награждаем
    # fire_days - дни, когда серия продлилась, по ним рисуется серия за две недели
    return {'xp': 0, 'streak': 0, 'best_streak': 0, 'last_day': '', 'daily_day': '', 'daily_seen': '',
            'badges': {}, 'secrets': [], 'stats': {key: 0 for key in STATS},
            'topics': {}, 'recent': [], 'difficulty': 2, 'finished': [], 'today': {}, 'fire_days': []}


def ensure(p):
    game = p.setdefault('game', new_game())
    old_profile = 'fire_days' not in game
    for key, value in new_game().items():
        game.setdefault(key, value)
    if old_profile and game['last_day'] and game['streak']:
        # профиль из прошлой версии: дни текущей серии восстанавливаем по её длине
        last = date.fromisoformat(game['last_day'])
        game['fire_days'] = [(last - timedelta(days=back)).isoformat()
                             for back in range(game['streak'] - 1, -1, -1)]
    if isinstance(game['badges'], list):  # прошлая версия: плоский список без ступеней
        game['badges'] = {}
    for key in STATS:
        game['stats'].setdefault(key, 0)
    return game


def level_floor(n):
    # 0, 80, 240, 480, 800...
    return 40 * (n - 1) * n


def level(xp):
    n = 1
    while n < len(TITLES) and xp >= level_floor(n + 1):
        n += 1
    nxt = level_floor(n + 1) if n < len(TITLES) else None
    return n, LEVEL_EMOJI[n - 1], TITLES[n - 1], level_floor(n), nxt


def bar(fraction, width=10):
    filled = max(0, min(width, round(fraction * width)))
    return '▰' * filled + '▱' * (width - filled)


def progress_line(xp):
    number, emoji, title, floor, nxt = level(xp)
    if nxt is None:
        return f'{emoji} Ур. {number} · {title} · {xp} XP — вершина!'
    return f'{emoji} Ур. {number} · {title}\n{bar((xp - floor) / (nxt - floor))} {xp}/{nxt} XP'


def stars(count, total=3):
    return '⭐' * count + '☆' * (total - count)


def xp_for(p, base):
    return round(base * XP_MULT.get(ensure(p)['difficulty'], 1.0))


def add_xp(p, amount):
    game = ensure(p)
    before = level(game['xp'])[0]
    game['xp'] += max(0, int(amount))
    after = level(game['xp'])
    events = []
    if after[0] > before:
        events.append(f'🎉 <b>Новый уровень {after[0]}: {after[1]} {after[2]}!</b>')
    return events + check_badges(p)


def leveled_up(events):
    return any('Новый уровень' in e for e in events)


def touch_streak(p, day=None):
    game = ensure(p)
    day = day or today()
    current = day.isoformat()
    if game['last_day'] == current:
        return []
    yesterday = (day - timedelta(days=1)).isoformat()
    game['streak'] = game['streak'] + 1 if game['last_day'] == yesterday else 1
    game['best_streak'] = max(game['best_streak'], game['streak'])
    game['last_day'] = current
    game['fire_days'] = (game['fire_days'] + [current])[-60:]
    events = []
    if game['streak'] >= 2:
        events.append(f"🔥 Серия: <b>{game['streak']} {days_word(game['streak'])} подряд</b>!")
    return events + check_badges(p)


def current_streak(p, day=None):
    game = ensure(p)
    day = day or today()
    if game['last_day'] in (day.isoformat(), (day - timedelta(days=1)).isoformat()):
        return game['streak']
    return 0


def fire_strip(p, days=14, day=None):
    # от сегодняшнего дня назад: True - в этот день серия продлилась
    day = day or today()
    fire = set(ensure(p)['fire_days'])
    return [(day - timedelta(days=back)).isoformat() in fire for back in range(days)]


def days_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return 'день'
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return 'дня'
    return 'дней'


def is_today(value, day=None):
    return value == (day or today()).isoformat()


def stat_value(game, stat):
    if stat == 'best_streak':
        return game['best_streak']
    if stat == 'topics':
        # освоенная тема: не меньше четырёх проверок и 80% верных
        return sum(1 for t in game['topics'].values() if t.get('n', 0) >= 4 and mastery(t) >= 0.8)
    return game['stats'].get(stat, 0)


def bump(p, stat, amount=1):
    game = ensure(p)
    game['stats'][stat] = game['stats'].get(stat, 0) + amount
    return check_badges(p)


def check_badges(p):
    game = ensure(p)
    events = []
    for key, (emoji, title, stat, thresholds, _) in BADGES.items():
        tier = sum(1 for t in thresholds if stat_value(game, stat) >= t)
        if tier > game['badges'].get(key, 0):
            game['badges'][key] = tier
            events.append(f'🏅 Достижение: {TIERS[tier - 1]} {emoji} <b>{title} {ROMAN[tier - 1]}</b>')
    return events


def unlock_secret(p, key):
    game = ensure(p)
    if key in game['secrets'] or key not in SECRETS:
        return []
    game['secrets'].append(key)
    emoji, title, _ = SECRETS[key]
    return [f'🤫 Секретное достижение: {emoji} <b>{title}</b>!']


def badge_rows(p):
    game = ensure(p)
    rows = []
    for key, (emoji, title, stat, thresholds, how) in BADGES.items():
        tier = game['badges'].get(key, 0)
        value = stat_value(game, stat)
        rows.append((emoji, title, tier, value, thresholds[tier] if tier < 3 else None, how))
    return rows


def badges_text(p):
    lines = []
    for emoji, title, tier, value, nxt, how in badge_rows(p):
        if tier:
            head = f'{TIERS[tier - 1]} {emoji} <b>{title} {ROMAN[tier - 1]}</b>'
        else:
            head = f'🔒 {emoji} {title}'
        tail = f' — {value}/{nxt}, {how}' if nxt else ' — максимум!'
        lines.append(head + tail)
    game = ensure(p)
    secret = [f'{SECRETS[k][0]} <b>{SECRETS[k][1]}</b> — {SECRETS[k][2]}' for k in game['secrets']]
    hidden = len(SECRETS) - len(game['secrets'])
    lines += [''] + secret + ([f'❔ Ещё секретных: {hidden}'] if hidden else [])
    return '\n'.join(lines).strip()


def badge_icons(p):
    game = ensure(p)
    icons = [TIERS[t - 1] + BADGES[k][0] for k, t in game['badges'].items() if t and k in BADGES]
    return ' '.join(icons + [SECRETS[k][0] for k in game['secrets']])


def badge_count(p):
    game = ensure(p)
    return sum(game['badges'].values()) + len(game['secrets']), 3 * len(BADGES) + len(SECRETS)


def topic_key(topic):
    return ' '.join(str(topic or '').lower().split())[:60]


def today_log(p, day=None):
    # что уже засчитали сегодня: сколько раз и за какие темы
    game = ensure(p)
    current = (day or today()).isoformat()
    if game['today'].get('day') != current:
        game['today'] = {'day': current, 'counts': {}, 'marks': []}
    return game['today']


def used_today(p, kind):
    return today_log(p)['counts'].get(kind, 0)


def count_today(p, kind, limit):
    # первые limit раз за день идут в зачёт, дальше нет: иначе награду легко набить одной кнопкой
    counts = today_log(p)['counts']
    if counts.get(kind, 0) >= limit:
        return False
    counts[kind] = counts.get(kind, 0) + 1
    return True


def first_today(p, kind, topic=''):
    # одно и то же по одной теме засчитываем раз в день (босс, "Учитель")
    marks = today_log(p)['marks']
    mark = f'{kind}:{topic_key(topic)}'
    if mark in marks:
        return False
    marks.append(mark)
    return True


def mark_finished(p, marker):
    if marker and marker not in ensure(p)['finished']:
        ensure(p)['finished'] = (ensure(p)['finished'] + [marker])[-300:]


def record_topic(p, topic):
    # тема попадает в "Мои темы", как только Люми её объяснила, даже без проверок
    key = topic_key(topic)
    if not key or key.startswith('тема из нашего'):
        return None
    topics = ensure(p)['topics']
    t = topics.setdefault(key, {'name': str(topic)[:60], 'n': 0, 'score': 0.0, 'quests': 0})
    t['last'] = today().isoformat()
    if len(topics) > 100:
        # храним не больше сотни: сначала забываем старые темы, по которым не было проверок
        unchecked = sorted((v.get('last', ''), i, k) for i, (k, v) in enumerate(topics.items())
                           if not v['n'] and k != key)
        for _, _, old in unchecked[:len(topics) - 100]:
            del topics[old]
    return t


def record_check(p, topic, quality):
    # quality: 1 с первой попытки, 0.5 с подсказкой, 0 не решил
    t = record_topic(p, topic)
    if t is None:
        return
    t['n'] += 1
    t['score'] += quality


def record_quest(p, topic):
    key = topic_key(topic)
    if key in ensure(p)['topics']:
        ensure(p)['topics'][key]['quests'] += 1


def mastery(t):
    return t['score'] / t['n'] if t.get('n') else 0.0


def topic_status(t):
    if not t.get('n'):
        return '📖', 'разобрали, проверок ещё не было'
    if t['n'] < 2:
        return '🆕', 'только начали'
    value = mastery(t)
    if value >= 0.8:
        return '✅', 'получается'
    if value >= 0.5:
        return '🙂', 'неплохо'
    return '⚠️', 'стоит повторить'


def recent_topics(p):
    # свежие сверху; у тем одного дня новее та, что добавлена позже
    items = list(ensure(p)['topics'].values())
    order = sorted(range(len(items)), key=lambda i: (items[i].get('last', ''), i), reverse=True)
    return [items[i] for i in order]


def topic_report(p, limit=3):
    items = [(t['name'], mastery(t)) for t in ensure(p)['topics'].values() if t.get('n', 0) >= 2]
    strong = sorted([i for i in items if i[1] >= 0.75], key=lambda i: -i[1])[:limit]
    weak = sorted([i for i in items if i[1] < 0.7], key=lambda i: i[1])[:limit]
    return strong, weak


def weakest_topic(p):
    weak = topic_report(p, 1)[1]
    return weak[0][0] if weak else None


def record_result(p, fraction):
    # три результата подряд от 85% поднимают сложность, до 35% опускают
    game = ensure(p)
    game['recent'] = (game['recent'] + [round(float(fraction), 2)])[-3:]
    if len(game['recent']) < 3:
        return []
    average = sum(game['recent']) / 3
    level_now = game['difficulty']
    if average >= 0.85 and level_now < 3:
        game['difficulty'] += 1
        game['recent'] = []
        emoji, name = DIFFICULTY[game['difficulty']]
        return [f'📈 Ты щёлкаешь задачи как орешки — поднимаю сложность до {emoji} <b>{name}</b>. Опыта будет больше!']
    if average <= 0.35 and level_now > 1:
        game['difficulty'] -= 1
        game['recent'] = []
        emoji, name = DIFFICULTY[game['difficulty']]
        return [f'🛟 Сделаю чуть проще — {emoji} <b>{name}</b>. Разберёмся и снова поднимемся!']
    return []


def difficulty_label(p):
    emoji, name = DIFFICULTY[ensure(p)['difficulty']]
    return f'{emoji} {name}'
