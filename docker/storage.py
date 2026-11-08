# Всё хранится в одной SQLite-базе. Соединение на каждый запрос: для одного процесса хватает.
import json
import random
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import game

CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
OLD_GROUPS = {'10+ класс': '10–11 класс'}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quests (code TEXT PRIMARY KEY, data TEXT NOT NULL, author INTEGER,
    plays INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER NOT NULL,
    kind TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '', value REAL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS events_user_ts ON events(user, ts);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
CREATE TABLE IF NOT EXISTS classes (code TEXT PRIMARY KEY, teacher INTEGER NOT NULL, name TEXT NOT NULL,
    created TEXT NOT NULL, grp TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS members (class TEXT NOT NULL, user INTEGER NOT NULL, joined TEXT NOT NULL,
    PRIMARY KEY (class, user));
CREATE TABLE IF NOT EXISTS assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, class TEXT NOT NULL,
    quest TEXT NOT NULL, topic TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS results (assignment INTEGER NOT NULL, user INTEGER NOT NULL, stars INTEGER NOT NULL,
    max INTEGER NOT NULL, finished TEXT NOT NULL, PRIMARY KEY (assignment, user));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily (user INTEGER NOT NULL, day TEXT NOT NULL, code TEXT NOT NULL,
    PRIMARY KEY (user, day));
'''
QUEST_COLUMNS = {'topic': "TEXT NOT NULL DEFAULT ''", 'grp': "TEXT NOT NULL DEFAULT ''",
                 'level': 'INTEGER NOT NULL DEFAULT 2', 'source': "TEXT NOT NULL DEFAULT 'ai'",
                 'pool': 'INTEGER NOT NULL DEFAULT 0'}


def new_profile():
    return {'group': '', 'role': 'student', 'interests': [], 'interests_done': False, 'nick': '',
            'nick_done': False, 'onboarded': False, 'filters': '', 'state': 'group', 'history': [],
            'topic': '', 'card': None, 'quest': None, 'boss': None, 'played': [], 'name': '',
            'game': game.new_game()}


def stamp():
    return datetime.now(game.MSK).isoformat(timespec='seconds')


def new_code(length=6):
    return ''.join(random.SystemRandom().choice(CODE_ALPHABET) for _ in range(length))


class Store:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)
            have = {row[1] for row in db.execute('PRAGMA table_info(quests)')}
            for column, kind in QUEST_COLUMNS.items():
                if column not in have:
                    db.execute(f'ALTER TABLE quests ADD COLUMN {column} {kind}')
            db.execute('CREATE INDEX IF NOT EXISTS quests_pool ON quests(pool, grp, level)')
            db.commit()

    def db(self):
        return closing(sqlite3.connect(self.path, timeout=10))


    def get(self, user_id):
        with self.db() as db:
            row = db.execute('SELECT data FROM users WHERE id=?', (user_id,)).fetchone()
        profile = json.loads(row[0]) if row else new_profile()
        for key, value in new_profile().items():
            profile.setdefault(key, value)
        game.ensure(profile)
        # остатки первой версии бота
        for old in ('practice', 'story', 'quiz'):
            profile.pop(old, None)
        if profile['state'] in ('practice', 'story', 'story_topic', 'quiz', 'review'):
            profile['state'] = 'ready'
        profile['group'] = OLD_GROUPS.get(profile['group'], profile['group'])
        return profile

    def save(self, user_id, profile):
        with self.db() as db:
            db.execute('INSERT INTO users VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (user_id, json.dumps(profile, ensure_ascii=False)))
            db.commit()

    def profiles(self, ids):
        ids = list(ids)
        if not ids:
            return {}
        with self.db() as db:
            rows = db.execute(f"SELECT id, data FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        result = {}
        for uid, data in rows:
            profile = json.loads(data)
            game.ensure(profile)
            result[uid] = profile
        return result

    def all_user_ids(self):
        with self.db() as db:
            return [row[0] for row in db.execute('SELECT id FROM users')]


    def save_quest(self, quest, author=None, grp='', level=2, source='ai', pool=0):
        with self.db() as db:
            for _ in range(10):
                code = new_code()
                try:
                    db.execute('INSERT INTO quests (code, data, author, created, topic, grp, level, source, pool) '
                               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               (code, json.dumps(quest, ensure_ascii=False), author, stamp(),
                                game.topic_key(quest.get('topic', '')), grp, level, source, pool))
                    db.commit()
                    return code
                except sqlite3.IntegrityError:
                    continue
        return None

    def load_quest(self, code):
        with self.db() as db:
            row = db.execute('SELECT data, author FROM quests WHERE code=?', (code,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, None)

    def played(self, code):
        with self.db() as db:
            db.execute('UPDATE quests SET plays = plays + 1 WHERE code=?', (code,))
            db.commit()

    def author_plays(self, author):
        if author is None:
            return 0
        with self.db() as db:
            row = db.execute('SELECT COALESCE(SUM(plays), 0) FROM quests WHERE author=?', (author,)).fetchone()
        return row[0]

    def pool_take(self, grp, level, exclude=(), topic=None, stages=3):
        query = 'SELECT code, data FROM quests WHERE pool=1 AND grp=? AND level=?'
        args = [grp, level]
        if topic:
            query += ' AND topic=?'
            args.append(game.topic_key(topic))
        with self.db() as db:
            rows = db.execute(query + ' ORDER BY RANDOM() LIMIT 30', args).fetchall()
        for code, data in rows:
            quest = json.loads(data)
            if code not in exclude and len(quest.get('stages', [])) == stages:
                return code, quest
        return None

    def pool_size(self, grp, level, exclude=(), stages=3):
        with self.db() as db:
            rows = db.execute('SELECT code, data FROM quests WHERE pool=1 AND grp=? AND level=?',
                              (grp, level)).fetchall()
        return sum(1 for code, data in rows
                   if code not in exclude and len(json.loads(data).get('stages', [])) == stages)


    def log(self, user, kind, topic='', value=None, ts=None):
        with self.db() as db:
            db.execute('INSERT INTO events (user, kind, topic, value, ts) VALUES (?, ?, ?, ?, ?)',
                       (user or 0, kind, game.topic_key(topic), value, int(ts or time.time())))
            db.commit()

    def query(self, sql, args=()):
        with self.db() as db:
            return db.execute(sql, args).fetchall()

    def weekly_xp(self, users, days=7):
        users = list(users)
        if not users:
            return {}
        since = int(time.time()) - days * 86400
        rows = self.query(f"SELECT user, SUM(value) FROM events WHERE kind='xp' AND ts>=? AND user IN "
                          f"({','.join('?' * len(users))}) GROUP BY user", [since] + users)
        return {user: int(total or 0) for user, total in rows}

    def last_seen(self, users):
        users = list(users)
        if not users:
            return {}
        rows = self.query(f"SELECT user, MAX(ts) FROM events WHERE user IN ({','.join('?' * len(users))}) "
                          'GROUP BY user', users)
        return dict(rows)

    def recent_quests(self, user, limit=10):
        rows = self.query("SELECT topic, value, ts FROM events WHERE user=? AND kind='quest_done' "
                          'ORDER BY ts DESC LIMIT ?', (user, limit))
        return [{'topic': t, 'score': v, 'ts': ts} for t, v, ts in rows]


    def create_class(self, teacher, name, grp=''):
        with self.db() as db:
            for _ in range(10):
                code = new_code(5)
                try:
                    db.execute('INSERT INTO classes VALUES (?, ?, ?, ?, ?)', (code, teacher, name[:60], stamp(), grp))
                    db.commit()
                    return code
                except sqlite3.IntegrityError:
                    continue
        return None

    def get_class(self, code):
        rows = self.query('SELECT code, teacher, name, created, grp FROM classes WHERE code=?', (code,))
        return dict(zip(('code', 'teacher', 'name', 'created', 'grp'), rows[0])) if rows else None

    def teacher_classes(self, teacher):
        rows = self.query('SELECT c.code, c.name, c.grp, COUNT(m.user) FROM classes c LEFT JOIN members m '
                          'ON m.class=c.code WHERE c.teacher=? GROUP BY c.code ORDER BY c.created', (teacher,))
        return [{'code': code, 'name': name, 'grp': grp, 'size': size} for code, name, grp, size in rows]

    def join_class(self, code, user):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO members VALUES (?, ?, ?)', (code, user, stamp()))
            db.commit()

    def leave_class(self, code, user):
        with self.db() as db:
            db.execute('DELETE FROM members WHERE class=? AND user=?', (code, user))
            db.commit()

    def members(self, code):
        return [row[0] for row in self.query('SELECT user FROM members WHERE class=? ORDER BY joined', (code,))]

    def member_classes(self, user):
        rows = self.query('SELECT c.code, c.name, c.teacher FROM members m JOIN classes c ON c.code=m.class '
                          'WHERE m.user=?', (user,))
        return [{'code': code, 'name': name, 'teacher': teacher} for code, name, teacher in rows]

    def add_assignment(self, code, quest_code, topic):
        with self.db() as db:
            cursor = db.execute('INSERT INTO assignments (class, quest, topic, created) VALUES (?, ?, ?, ?)',
                                (code, quest_code, topic[:60], stamp()))
            db.commit()
            return cursor.lastrowid

    def assignments(self, code):
        rows = self.query('SELECT id, quest, topic, created FROM assignments WHERE class=? ORDER BY id DESC', (code,))
        return [dict(zip(('id', 'quest', 'topic', 'created'), row)) for row in rows]

    def assignment(self, assignment_id):
        rows = self.query('SELECT id, class, quest, topic, created FROM assignments WHERE id=?', (assignment_id,))
        return dict(zip(('id', 'class', 'quest', 'topic', 'created'), rows[0])) if rows else None

    def save_result(self, assignment_id, user, got, best, keep_first=False):
        # keep_first: повтор с уже известными ответами не должен улучшать результат для учителя
        update = 'NOTHING' if keep_first else \
            'UPDATE SET stars=MAX(stars, excluded.stars), max=excluded.max, finished=excluded.finished'
        with self.db() as db:
            db.execute('INSERT INTO results VALUES (?, ?, ?, ?, ?) ON CONFLICT(assignment, user) DO ' + update,
                       (assignment_id, user, got, best, stamp()))
            db.commit()

    def results(self, assignment_id):
        rows = self.query('SELECT user, stars, max, finished FROM results WHERE assignment=?', (assignment_id,))
        return {user: {'stars': s, 'max': m, 'finished': f} for user, s, m, f in rows}


    def daily_get(self, user, day):
        rows = self.query('SELECT code FROM daily WHERE user=? AND day=?', (user or 0, day))
        return rows[0][0] if rows else None

    def daily_set(self, user, day, code, replace=False):
        # если задачу уже успели выбрать (например, фоном), остаётся она.
        # replace - когда сегодняшнюю разобрали вместе и вместо неё даём похожую
        with self.db() as db:
            db.execute(f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO daily VALUES (?, ?, ?)",
                       (user or 0, day, code))
            db.commit()
        return self.daily_get(user, day)

    def kv_get(self, key):
        rows = self.query('SELECT value FROM kv WHERE key=?', (key,))
        return rows[0][0] if rows else None

    def kv_set(self, key, value):
        with self.db() as db:
            db.execute('INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            db.commit()

    def secret(self):
        value = self.kv_get('web_secret')
        if not value:
            value = secrets.token_hex(32)
            self.kv_set('web_secret', value)
        return value
