# Метрики по таблице events: активность, возвращаемость, доходимость квестов
# и "учебный эффект" (как меняется точность с первой попытки у каждого ученика).
import time
from collections import defaultdict
from datetime import datetime

import game

DAY = 86400


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def metrics(store, now=None):
    now = int(now or time.time())
    rows = store.query('SELECT user, kind, topic, value, ts FROM events ORDER BY ts')
    by_user = defaultdict(list)
    kinds = defaultdict(list)
    for user, kind, topic, value, ts in rows:
        if kind != 'xp':
            by_user[user].append(ts)
        kinds[kind].append((user, topic, value, ts))

    def active(days):
        return len({u for u, stamps in by_user.items() if stamps and stamps[-1] >= now - days * DAY})

    def returned(after):
        eligible = [s for s in by_user.values() if s and s[0] <= now - after]
        if not eligible:
            return None
        return sum(1 for s in eligible if s[-1] >= s[0] + after) / len(eligible)

    stage_by_user = defaultdict(list)
    for user, _, value, _ in kinds['stage']:
        stage_by_user[user].append(value or 0)
    gains = [(_avg(v[:5]), _avg(v[-5:])) for v in stage_by_user.values() if len(v) >= 10]
    teach_by_user = defaultdict(list)
    for user, _, value, _ in kinds['teach']:
        teach_by_user[user].append(value or 0)
    teach_gains = [(v[0], v[-1]) for v in teach_by_user.values() if len(v) >= 2]

    started = len(kinds['quest_start'])
    done = len(kinds['quest_done'])
    topics = defaultdict(int)
    for kind in ('quest_start', 'topic', 'boss_done'):
        for _, topic, _, _ in kinds[kind]:
            if topic:
                topics[topic] += 1
    daily = []
    for back in range(13, -1, -1):
        day_start = now - back * DAY
        label = datetime.fromtimestamp(day_start, game.MSK).strftime('%d.%m')
        users = {u for u, stamps in by_user.items() if any(day_start - DAY < t <= day_start for t in stamps)}
        daily.append((label, len(users)))

    return {
        'users': len(store.all_user_ids()),
        'onboarded': len({u for u, *_ in kinds['onboarded']}),
        'dau': active(1), 'wau': active(7), 'mau': active(30),
        'return_d1': returned(DAY), 'return_d7': returned(7 * DAY),
        'quests_started': started, 'quests_done': done,
        'completion': done / started if started else None,
        'avg_stars': _avg([v for _, _, v, _ in kinds['quest_done']]),
        'first_try': _avg([1.0 if v == 1 else 0.0 for _, _, v, _ in kinds['stage']]),
        'gain_before': _avg([a for a, _ in gains]), 'gain_after': _avg([b for _, b in gains]),
        'gain_users': len(gains),
        'boss_battles': len(kinds['boss_done']),
        'boss_wins': _avg([1.0 if (v or 0) >= 2 / 3 else 0.0 for _, _, v, _ in kinds['boss_done']]),
        'teach_avg': _avg([v for _, _, v, _ in kinds['teach']]),
        'teach_before': _avg([a for a, _ in teach_gains]), 'teach_after': _avg([b for _, b in teach_gains]),
        'daily_done': len(kinds['daily_done']), 'questions': len(kinds['explain']),
        'photos': len(kinds['photo']), 'shared_plays': len(kinds['shared_play']),
        'classes': len(store.query('SELECT code FROM classes')),
        'students_in_classes': len(store.query('SELECT DISTINCT user FROM members')),
        'assignments_done': len(kinds['assignment_done']),
        'top_topics': sorted(topics.items(), key=lambda kv: -kv[1])[:8],
        'daily_active': daily,
    }


def pct(value):
    return '—' if value is None else f'{round(value * 100)}%'


def summary(m):
    gain = ''
    if m['gain_before'] is not None:
        delta = (m['gain_after'] - m['gain_before']) * 100
        gain = f"\n📈 Учебный эффект: точность {pct(m['gain_before'])} → {pct(m['gain_after'])} ({delta:+.0f} п.п., {m['gain_users']} уч.)"
    return (f"📊 <b>Метрики Люми</b>\n\n"
            f"👥 Пользователей: {m['users']} (прошли знакомство: {m['onboarded']})\n"
            f"🔥 Активны: сегодня {m['dau']} · за неделю {m['wau']} · за месяц {m['mau']}\n"
            f"↩️ Вернулись: через сутки {pct(m['return_d1'])} · через неделю {pct(m['return_d7'])}\n\n"
            f"🎮 Квестов: начато {m['quests_started']}, пройдено {m['quests_done']} (доходимость {pct(m['completion'])})\n"
            f"⭐ Средний результат: {pct(m['avg_stars'])} · с первой попытки: {pct(m['first_try'])}"
            f"{gain}\n"
            f"⚔️ Битв с боссом: {m['boss_battles']}, побед {pct(m['boss_wins'])}\n"
            f"🎓 «Учитель»: среднее понимание {pct(m['teach_avg'])}\n"
            f"🗓 Задач дня: {m['daily_done']} · ❓ Вопросов: {m['questions']} · 📸 Фото: {m['photos']}\n"
            f"🏫 Классов: {m['classes']}, учеников в классах: {m['students_in_classes']}, "
            f"заданий выполнено: {m['assignments_done']}")
