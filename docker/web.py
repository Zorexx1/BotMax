# Веб-часть: кабинет ученика, кабинет учителя, метрики и JSON API.
# Работает в том же процессе, что и бот. Доступ по подписанным ссылкам из бота.
#
#   /                 страница проекта
#   /app              кабинет ученика
#   /teacher          кабинет учителя, /teacher/КОД.csv выгрузка класса
#   /admin            метрики
#   /api/v1/...       то же в JSON
#   /health           для docker healthcheck
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

import analytics
import game

LOG = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / 'assets'
TOKEN_DAYS = 30


def make_token(secret, uid, role='s', days=TOKEN_DAYS):
    body = base64.urlsafe_b64encode(json.dumps(
        {'u': uid, 'r': role, 'e': int(time.time()) + days * 86400}, separators=(',', ':')).encode()).decode().rstrip('=')
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f'{body}.{sig}'


def read_token(secret, token):
    try:
        body, sig = token.split('.', 1)
        good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if data['e'] < time.time():
            return None
        return data['u'], data['r']
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def esc(value):
    return html.escape(str(value), quote=True)


def fmt_day(ts):
    return datetime.fromtimestamp(ts, game.MSK).strftime('%d.%m %H:%M') if ts else '—'


def display_name(profile):
    name = profile.get('name') or ''
    nick = profile.get('nick') or ''
    if name and nick and nick != name:
        return f'{name} ({nick})'
    return name or nick or 'Без имени'


def student_data(store, uid):
    p = store.get(uid)
    g = game.ensure(p)
    number, emoji, title, floor, nxt = game.level(g['xp'])
    strong, weak = game.topic_report(p, 5)
    topics = [{'name': t['name'], 'mastery': round(game.mastery(t) * 100), 'checks': t['n'],
               'icon': game.topic_status(t)[0], 'status': game.topic_status(t)[1],
               'quests': t.get('quests', 0)} for t in game.recent_topics(p)]
    badges = [{'emoji': e, 'title': tl, 'tier': tier, 'value': value, 'next': nxt_, 'how': how}
              for e, tl, tier, value, nxt_, how in game.badge_rows(p)]
    classes = []
    for c in store.member_classes(uid):
        members = store.members(c['code'])
        weekly = store.weekly_xp(members)
        profiles = store.profiles(members)
        board = sorted(({'nick': profiles.get(m, {}).get('nick') or 'Герой', 'xp': weekly.get(m, 0), 'me': m == uid}
                        for m in members), key=lambda r: -r['xp'])[:10]
        classes.append({'name': c['name'], 'code': c['code'], 'board': board})
    return {
        'nick': p.get('nick') or 'Герой', 'group': p.get('group', ''), 'xp': g['xp'],
        'level': {'number': number, 'emoji': emoji, 'title': title, 'floor': floor, 'next': nxt},
        'difficulty': game.difficulty_label(p), 'streak': game.current_streak(p), 'best_streak': g['best_streak'],
        'stats': g['stats'], 'topics': topics, 'strong': strong, 'weak': weak, 'badges': badges,
        'secrets': [{'emoji': game.SECRETS[k][0], 'title': game.SECRETS[k][1]} for k in g['secrets']],
        'secrets_hidden': len(game.SECRETS) - len(g['secrets']),
        'classes': classes, 'fire': game.fire_strip(p), 'recent': store.recent_quests(uid),
    }


def class_data(store, code):
    info = store.get_class(code)
    if not info:
        return None
    members = store.members(code)
    profiles = store.profiles(members)
    weekly = store.weekly_xp(members)
    seen = store.last_seen(members)
    assignments = []
    for a in store.assignments(code)[:20]:
        results = store.results(a['id'])
        scores = [r['stars'] / r['max'] for r in results.values() if r['max']]
        assignments.append({'id': a['id'], 'topic': a['topic'], 'created': a['created'][:16].replace('T', ' '),
                            'done': len(results), 'total': len(members), 'avg': round(sum(scores) / len(scores) * 100) if scores else None,
                            'results': {str(u): r for u, r in results.items()}})
    students = []
    for uid in members:
        p = profiles.get(uid, {})
        g = game.ensure(p) if p else game.new_game()
        _, weak = game.topic_report(p, 2) if p else ([], [])
        done = [a['results'][str(uid)] for a in assignments if str(uid) in a['results']]
        students.append({
            'id': uid, 'name': display_name(p), 'level': game.level(g['xp'])[0], 'xp': g['xp'],
            'xp_week': weekly.get(uid, 0), 'quests': g['stats'].get('quests', 0),
            'assignments_done': len(done), 'assignments_avg': round(sum(r['stars'] / r['max'] for r in done) / len(done) * 100) if done else None,
            'streak': game.current_streak(p) if p else 0, 'last_seen': seen.get(uid),
            'weak': [{'name': n, 'mastery': round(m * 100)} for n, m in weak],
        })
    students.sort(key=lambda s: -s['xp_week'])
    return {'code': code, 'name': info['name'], 'group': info.get('grp', ''), 'students': students,
            'assignments': assignments}


CSS = '''
:root{--bg:#f4f1ff;--card:#fff;--ink:#1d1640;--muted:#6b6489;--brand:#6d28d9;--brand2:#ec4899;--teal:#0d9488;
--ok:#16a34a;--warn:#f59e0b;--bad:#ef4444;--line:#e7e2fb;--chip:#efeaff}
@media (prefers-color-scheme:dark){:root{--bg:#120f24;--card:#1d1838;--ink:#f1edff;--muted:#a39cc7;--line:#2e2758;--chip:#2a2350}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:980px;margin:0 auto;padding:16px}
.hero{border-radius:22px;overflow:hidden;position:relative;background:linear-gradient(120deg,var(--brand),var(--brand2));color:#fff}
.hero img{width:100%;display:block;max-height:280px;object-fit:cover}
.hero .in{padding:18px 20px}.hero h1{margin:0 0 4px;font-size:26px}.hero p{margin:0;opacity:.9}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;margin:14px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
.tile b{display:block;font-size:24px}.tile span{color:var(--muted);font-size:13px}
h2{font-size:18px;margin:0 0 12px}.muted{color:var(--muted)}
.bar{height:10px;background:var(--chip);border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--teal),var(--brand))}
.row{display:flex;align-items:center;gap:10px;margin:8px 0}.row .name{flex:0 0 42%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row .bar{flex:1}.row .v{flex:0 0 46px;text-align:right;font-weight:600}
.row .st{flex:1;text-align:right;color:var(--muted);font-size:13px}
.badges{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.badge{border:1px solid var(--line);border-radius:14px;padding:10px 12px;background:var(--chip)}.badge.off{opacity:.55}
.badge .t{font-weight:600}.badge small{color:var(--muted)}
.strip{display:flex;gap:4px}.strip i{flex:1;max-width:26px;height:26px;display:grid;place-items:center;font-style:normal;font-size:18px;border-radius:8px;background:var(--chip)}.strip i.on{background:none}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.chip{display:inline-block;background:var(--chip);border-radius:99px;padding:2px 9px;margin:2px;font-size:13px}
.btn{display:inline-block;background:linear-gradient(90deg,var(--brand),var(--brand2));color:#fff;text-decoration:none;padding:11px 18px;border-radius:12px;font-weight:600;margin:6px 6px 0 0}
.btn.alt{background:var(--chip);color:var(--ink)}
.scroll{overflow-x:auto}.me{background:rgba(109,40,217,.12)}
.feat{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}.feat div{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
.feat b{display:block;margin-bottom:4px}
footer{color:var(--muted);font-size:13px;text-align:center;margin:24px 0 90px}
nav.tabs{position:fixed;left:0;right:0;bottom:0;display:flex;background:var(--card);border-top:1px solid var(--line);
padding:6px 4px calc(6px + env(safe-area-inset-bottom));z-index:5}
nav.tabs button{flex:1;border:0;background:none;color:var(--muted);font:inherit;font-size:12px;padding:4px 0;border-radius:12px}
nav.tabs button b{display:block;font-size:20px;font-weight:400}nav.tabs button.on{color:var(--brand);background:var(--chip)}
'''

APP_SCRIPT = '''<script>
function tab(name) {
  document.querySelectorAll('[data-tab]').forEach(function (el) { el.hidden = el.dataset.tab !== name; });
  document.querySelectorAll('nav.tabs button').forEach(function (b) { b.classList.toggle('on', b.dataset.go === name); });
  window.scrollTo(0, 0);
}
</script>'''


# PWA: кабинет можно "добавить на главный экран" и открывать как приложение
PWA_HEAD = '''<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#6d28d9">
<link rel="apple-touch-icon" href="/static/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Люми">'''
PWA_SCRIPT = '''<script>
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(function () {});
(function () {
  // ссылка из бота содержит токен; запоминаем его, чтобы иконка на экране открывала кабинет без бота
  if (location.pathname !== '/app') return;
  var q = new URLSearchParams(location.search);
  try {
    if (q.get('t')) localStorage.setItem('lumi_t', q.get('t'));
    else if (localStorage.getItem('lumi_t'))
      location.replace('/app?t=' + encodeURIComponent(localStorage.getItem('lumi_t')));
  } catch (e) {}
})();
var installPrompt = null;
window.addEventListener('beforeinstallprompt', function (e) {
  e.preventDefault(); installPrompt = e;
  var b = document.getElementById('install'); if (b) b.hidden = false;
});
function install() { if (installPrompt) { installPrompt.prompt(); installPrompt = null; } }
</script>'''
MANIFEST = {
    'name': 'Люми — учёба как квест', 'short_name': 'Люми', 'start_url': '/app', 'scope': '/',
    'display': 'standalone', 'background_color': '#f4f1ff', 'theme_color': '#6d28d9', 'lang': 'ru',
    'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
              {'src': '/static/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
              {'src': '/static/icon-maskable.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}],
}
SERVICE_WORKER = '''const CACHE = 'lumi-v1';
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(
  ['/static/icon-192.png', '/static/profile.png']))));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/static/'))
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      const copy = resp.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return resp; })));
  else
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request).then(r => r ||
      new Response('<meta charset="utf-8"><p style="font:16px sans-serif;padding:24px">Нет интернета. Кабинет откроется, когда связь вернётся.</p>',
                   {headers: {'Content-Type': 'text/html; charset=utf-8'}}))));
});
'''


def page(title, body):
    return web.Response(text=f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{esc(title)}</title>
<style>{CSS}</style>{PWA_HEAD}{APP_SCRIPT}{PWA_SCRIPT}</head><body><div class="wrap">{body}
<footer>Люми — ИИ-напарник для учёбы в MAX · данные хранятся на сервере проекта</footer></div></body></html>''',
                        content_type='text/html')


def bar(percent):
    percent = max(0, min(100, percent or 0))
    return f'<div class="bar"><i style="width:{percent}%"></i></div>'


def tile(value, label):
    return f'<div class="tile"><b>{esc(value)}</b><span>{esc(label)}</span></div>'


def svg_bars(points, height=120):
    if not points:
        return ''
    top = max(v for _, v in points) or 1
    width = 100 / len(points)
    bars = ''.join(
        f'<rect x="{i * width + width * 0.15:.2f}%" y="{86 - v / top * 80:.2f}%" width="{width * 0.7:.2f}%" '
        f'height="{v / top * 80:.2f}%" rx="3" fill="url(#g)"><title>{esc(label)}: {v}</title></rect>'
        for i, (label, v) in enumerate(points))
    labels = ''.join(f'<text x="{i * width + width / 2:.2f}%" y="100%" font-size="9" text-anchor="middle" '
                     f'fill="currentColor" opacity=".6">{esc(label)}</text>' for i, (label, _) in enumerate(points) if i % 2 == 0)
    return (f'<svg width="100%" height="{height}" style="overflow:visible"><defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">'
            f'<stop offset="0" stop-color="#ec4899"/><stop offset="1" stop-color="#6d28d9"/></linearGradient></defs>{bars}{labels}</svg>')


def student_html(d, bot_link):
    lvl = d['level']
    progress = 100 if not lvl['next'] else round((d['xp'] - lvl['floor']) / (lvl['next'] - lvl['floor']) * 100)
    s = d['stats']
    topics = ''
    for t in d['topics']:
        # без проверок процент ничего не значит, поэтому вместо полоски пишем статус
        score = (f'{bar(t["mastery"])}<span class="v">{t["mastery"]}%</span>' if t['checks'] >= 2
                 else f'<span class="st">{esc(t["status"])}</span>')
        topics += (f'<div class="row"><span class="name" title="{esc(t["status"])}">{t["icon"]} {esc(t["name"])}</span>'
                   f'{score}</div>')
    topics = topics or '<p class="muted">Пока пусто: напиши боту тему, и она появится здесь.</p>'
    tiers = ['🥉', '🥈', '🥇']
    badges = ''
    for b in d['badges']:
        medal = ''.join(tiers[:b['tier']]) or '🔒'
        target = f'{b["value"]}/{b["next"]} · {esc(b["how"])}' if b['next'] else 'максимум!'
        pct = 100 if not b['next'] else round(min(1, b['value'] / b['next']) * 100)
        badges += (f'<div class="badge {"off" if not b["tier"] else ""}"><div class="t">{b["emoji"]} {esc(b["title"])} {medal}</div>'
                   f'<small>{target}</small>{bar(pct)}</div>')
    badges += ''.join(f'<div class="badge"><div class="t">{x["emoji"]} {esc(x["title"])} 🤫</div><small>секретное</small></div>'
                      for x in d['secrets'])
    if d['secrets_hidden']:
        badges += f'<div class="badge off"><div class="t">❔ Секретные</div><small>ещё {d["secrets_hidden"]}, найди сам</small></div>'
    fire = ''.join('<i class="on">🔥</i>' if on else '<i></i>' for on in d['fire'])
    classes = ''
    for c in d['classes']:
        rows = ''.join(f'<tr class="{"me" if r["me"] else ""}"><td>{i + 1}</td><td>{esc(r["nick"])}</td><td>{r["xp"]} XP</td></tr>'
                       for i, r in enumerate(c['board']))
        classes += f'<div class="card"><h2>🏫 {esc(c["name"])}: рейтинг недели</h2><table>{rows}</table></div>'
    if not classes:
        classes = ('<div class="card"><h2>🏫 Класс</h2><p>Если учитель дал код класса, напиши боту '
                   '<b>/class КОД</b>. Тогда здесь появится рейтинг класса, а задания учителя будут приходить в чат.</p></div>')
    recent = ''.join(f'<span class="chip">{esc(r["topic"] or "квест")} · {round((r["score"] or 0) * 100)}%</span>'
                     for r in d['recent']) or '<span class="muted">пока пусто</span>'
    strong = ', '.join(f'{esc(n)} ({round(m * 100)}%)' for n, m in d['strong']) or 'пока рано судить'
    weak = ', '.join(f'{esc(n)} ({round(m * 100)}%)' for n, m in d['weak']) or 'нет, так держать!'
    back = f'<a class="btn" href="{esc(bot_link)}">💬 В чат с Люми</a>' if bot_link else ''
    back += '<button id="install" class="btn alt" hidden onclick="install()">📲 Установить как приложение</button>'
    return f'''<p id="status" class="muted"></p>
<section data-tab="hero">
<div class="hero"><img src="/static/profile.png" alt=""><div class="in"><h1>{lvl["emoji"]} {esc(d["nick"])}</h1>
<p>{esc(d["group"])} · Уровень {lvl["number"]}: {esc(lvl["title"])} · Сложность {esc(d["difficulty"])}</p>
<div style="margin-top:10px">{bar(progress)}</div><p style="margin-top:6px">{d["xp"]} XP{f" из {lvl['next']}" if lvl["next"] else ""}</p></div></div>
<div class="grid">{tile(f"🔥 {d['streak']}", "дней подряд")}{tile(s.get("quests", 0), "квестов")}
{tile(s.get("bosses", 0), "боссов побеждено")}{tile(s.get("daily", 0), "задач дня")}</div>
<div class="card"><h2>📅 Серия за две недели</h2><div class="strip">{fire}</div>
<p class="muted">🔥 — день, когда ты сам решил задачу, попал по боссу или объяснил тему.
Рекорд серии: {d["best_streak"]} {game.days_word(d["best_streak"])}</p></div>
<div class="card"><h2>🎮 Последние квесты</h2>{recent}</div>{back}
</section>
<section data-tab="know" hidden>
<div class="card"><h2>📚 Мои темы</h2><p class="muted">Все темы, которые мы разбирали, свежие сверху:
✅ получается, 🙂 неплохо, ⚠️ стоит повторить, 🆕 только начали, 📖 разобрали, но ещё не проверяли.
Считаю по квестам, боссам и объяснениям в «Учителе».</p>{topics}</div>
<div class="card"><p>💪 <b>Сильные темы:</b> {strong}</p><p>🎯 <b>Подтянуть:</b> {weak}</p>
<p class="muted">Задача дня через день будет на слабую тему.</p></div>
</section>
<section data-tab="awards" hidden><div class="card"><h2>🏅 Достижения</h2><div class="badges">{badges}</div></div></section>
<section data-tab="class" hidden>{classes}</section>
<nav class="tabs"><button class="on" data-go="hero" onclick="tab('hero')"><b>🦸</b>Герой</button>
<button data-go="know" onclick="tab('know')"><b>📚</b>Темы</button>
<button data-go="awards" onclick="tab('awards')"><b>🏅</b>Награды</button>
<button data-go="class" onclick="tab('class')"><b>🏫</b>Класс</button></nav>'''


def teacher_html(classes, current, token, bot_link):
    tabs = ''.join(f'<a class="btn {"" if current and c["code"] == current["code"] else "alt"}" '
                   f'href="?t={esc(token)}&c={esc(c["code"])}">{esc(c["name"])} · {c["size"]}</a>' for c in classes)
    if not classes:
        return ('<div class="hero"><img src="/static/teacher.png" alt=""><div class="in"><h1>Кабинет учителя</h1>'
                '<p>Создайте класс в боте командой /class — он появится здесь.</p></div></div>')
    body = f'<div class="hero"><img src="/static/teacher.png" alt=""><div class="in"><h1>Кабинет учителя</h1><p>Выберите класс</p></div></div><div>{tabs}</div>'
    if not current:
        return body
    invite = f'{bot_link}?start=class_{current["code"]}' if bot_link else ''
    st = current['students']
    active = sum(1 for s in st if s['last_seen'] and s['last_seen'] > time.time() - 7 * 86400)
    rows = ''
    for s in st:
        weak = ' '.join(f'<span class="chip">{esc(w["name"])} {w["mastery"]}%</span>' for w in s['weak']) or '—'
        rows += (f'<tr><td>{esc(s["name"])}</td><td>{s["level"]}</td><td>{s["xp_week"]}</td><td>{s["quests"]}</td>'
                 f'<td>{s["assignments_done"]}</td><td>{"—" if s["assignments_avg"] is None else str(s["assignments_avg"]) + "%"}</td>'
                 f'<td>🔥{s["streak"]}</td><td>{fmt_day(s["last_seen"])}</td><td>{weak}</td></tr>')
    assignments = ''
    for a in current['assignments']:
        done = round(a['done'] / a['total'] * 100) if a['total'] else 0
        assignments += (f'<tr><td>{esc(a["topic"])}</td><td>{esc(a["created"])}</td><td>{a["done"]}/{a["total"]}{bar(done)}</td>'
                        f'<td>{"—" if a["avg"] is None else str(a["avg"]) + "%"}</td></tr>')
    return body + f'''
<div class="grid">{tile(len(st), "учеников")}{tile(active, "активны за неделю")}{tile(len(current["assignments"]), "заданий")}
{tile(sum(s["xp_week"] for s in st), "XP класса за неделю")}</div>
<div class="card"><h2>🔑 Приглашение</h2><p>Код класса: <b>{esc(current["code"])}</b>. Ученик пишет боту <b>/class {esc(current["code"])}</b>
{f'или открывает ссылку <a href="{esc(invite)}">{esc(invite)}</a>' if invite else ''}.</p>
<a class="btn" href="/teacher/{esc(current["code"])}.csv?t={esc(token)}">⬇️ Скачать CSV</a>
<a class="btn alt" href="/api/v1/class/{esc(current["code"])}?t={esc(token)}">JSON API</a></div>
<div class="card"><h2>📝 Задания</h2><div class="scroll"><table><tr><th>Тема</th><th>Создано</th><th>Выполнили</th><th>Средний результат</th></tr>
{assignments or '<tr><td colspan="4" class="muted">Задайте квест классу в боте: /class → «Задать квест»</td></tr>'}</table></div></div>
<div class="card"><h2>👥 Ученики</h2><div class="scroll"><table><tr><th>Ученик</th><th>Ур.</th><th>XP/нед</th><th>Квестов</th>
<th>Заданий</th><th>Результат</th><th>Серия</th><th>Был</th><th>Подтянуть</th></tr>{rows or '<tr><td colspan="9" class="muted">Пока никого — отправьте ученикам код.</td></tr>'}</table></div></div>'''


def admin_html(m):
    p = analytics.pct
    gain = ''
    if m['gain_before'] is not None:
        gain = tile(f"{p(m['gain_before'])} → {p(m['gain_after'])}", f"точность с 1-й попытки: первые 5 этапов → последние 5 ({m['gain_users']} уч.)")
    topics = ''.join(f'<span class="chip">{esc(t)} · {n}</span>' for t, n in m['top_topics']) or '<span class="muted">нет данных</span>'
    return f'''<div class="hero"><img src="/static/levelup.png" alt=""><div class="in"><h1>Метрики пользы</h1>
<p>Обновлено {datetime.now(game.MSK).strftime("%d.%m.%Y %H:%M")}</p></div></div>
<div class="grid">{tile(m["users"], "пользователей")}{tile(m["dau"], "активны сегодня")}{tile(m["wau"], "за 7 дней")}
{tile(p(m["return_d1"]), "вернулись через сутки+")}{tile(p(m["return_d7"]), "вернулись через неделю+")}</div>
<div class="card"><h2>📈 Активные пользователи по дням</h2>{svg_bars(m["daily_active"])}</div>
<div class="grid">{tile(m["quests_started"], "квестов начато")}{tile(p(m["completion"]), "доходимость квестов")}
{tile(p(m["avg_stars"]), "средний результат")}{tile(p(m["first_try"]), "верно с первой попытки")}{gain}</div>
<div class="grid">{tile(m["boss_battles"], "битв с боссом")}{tile(p(m["boss_wins"]), "побед")}{tile(p(m["teach_avg"]), "понимание в «Учителе»")}
{tile(m["daily_done"], "задач дня")}{tile(m["questions"], "вопросов к ИИ")}</div>
<div class="grid">{tile(m["classes"], "классов")}{tile(m["students_in_classes"], "учеников в классах")}
{tile(m["assignments_done"], "заданий учителя выполнено")}{tile(m["shared_plays"], "квестов друзей пройдено")}</div>
<div class="card"><h2>🔥 Популярные темы</h2>{topics}</div>'''


def landing_html(bot_link, m):
    return f'''<div class="hero"><img src="/static/welcome.png" alt=""><div class="in"><h1>Люми — учёба как квест</h1>
<p>ИИ-напарник в MAX, который отвечает на вопрос «Зачем мне это учить?» не лекцией, а приключением.</p>
{f'<a class="btn" style="background:#fff;color:#6d28d9" href="{esc(bot_link)}">Открыть в MAX</a>' if bot_link else ''}</div></div>
<div class="grid">{tile(m["users"], "учеников и учителей")}{tile(m["quests_done"], "квестов пройдено")}
{tile(analytics.pct(m["first_try"]), "ответов верно с первой попытки")}{tile(m["classes"], "классов")}</div>
<div class="feat">
<div><b>🎮 Квесты под интересы</b>Тема превращается в историю, где ученик — герой, а знания — способ добиться цели.</div>
<div><b>🎯 Под уровень</b>1–4, 5–7, 8–9, 10–11 класс и взрослые; сложность подстраивается по результатам.</div>
<div><b>✅ Проверенные ответы</b>Каждый квест решает второй, независимый ИИ-решатель — ошибки не доходят до детей.</div>
<div><b>⚔️ Боссы и «Учитель»</b>Тест — как битва, а объяснение темы своими словами получает оценку понимания.</div>
<div><b>🏫 Для класса</b>Учитель задаёт квест — он приходит всему классу; результаты в кабинете и CSV.</div>
<div><b>📈 Прогресс</b>Опыт, серии, достижения, список разобранных тем и задачи дня на темы, которые даются хуже.</div></div>'''


class WebApp:
    def __init__(self, store, info):
        self.store = store
        self.info = info  # {'username': ..., 'gigachat': ..., 'started': ...}
        self.secret = store.secret()

    def bot_link(self):
        name = self.info.get('username')
        return f'https://max.ru/{name}' if name else ''

    def auth(self, request, roles):
        found = read_token(self.secret, request.query.get('t', ''))
        if not found or found[1] not in roles:
            raise web.HTTPForbidden(text='Ссылка устарела или неверна. Откройте её заново из бота: /me, /class или /stats.')
        return found[0]

    async def landing(self, request):
        return page('Люми — учёба как квест', landing_html(self.bot_link(), analytics.metrics(self.store)))

    async def student(self, request):
        if not request.query.get('t'):
            return page('Люми', '<div class="card"><p id="status">Открываю кабинет…</p>'
                                '<p class="muted">Если ничего не происходит, открой кабинет кнопкой «📊 Мой кабинет» '
                                'в чате с ботом (/me).</p></div>')
        uid = self.auth(request, ('s', 't'))
        return page('Мой кабинет — Люми', student_html(student_data(self.store, uid), self.bot_link()))

    def teacher_class(self, request, uid, code):
        info = self.store.get_class(code)
        if not info or info['teacher'] != uid:
            raise web.HTTPNotFound(text='Класс не найден')
        return class_data(self.store, code)

    async def teacher(self, request):
        uid = self.auth(request, ('t',))
        classes = self.store.teacher_classes(uid)
        code = request.query.get('c') or (classes[0]['code'] if classes else '')
        current = self.teacher_class(request, uid, code) if code else None
        return page('Кабинет учителя — Люми', teacher_html(classes, current, request.query['t'], self.bot_link()))

    async def teacher_csv(self, request):
        uid = self.auth(request, ('t',))
        data = self.teacher_class(request, uid, request.match_info['code'])
        out = io.StringIO()
        writer = csv.writer(out, delimiter=';')
        writer.writerow(['Ученик', 'Уровень', 'XP всего', 'XP за неделю', 'Квестов', 'Заданий выполнено',
                         'Средний результат заданий, %', 'Серия, дней', 'Последняя активность', 'Слабые темы']
                        + [f'Задание: {a["topic"]} ({a["created"]})' for a in data['assignments']])
        for s in data['students']:
            per = []
            for a in data['assignments']:
                r = a['results'].get(str(s['id']))
                per.append(f'{r["stars"]}/{r["max"]}' if r else '')
            writer.writerow([s['name'], s['level'], s['xp'], s['xp_week'], s['quests'], s['assignments_done'],
                             '' if s['assignments_avg'] is None else s['assignments_avg'], s['streak'],
                             fmt_day(s['last_seen']), ', '.join(f'{w["name"]} {w["mastery"]}%' for w in s['weak'])] + per)
        return web.Response(body=('﻿' + out.getvalue()).encode('utf-8'), content_type='text/csv', charset='utf-8',
                            headers={'Content-Disposition': f'attachment; filename="class_{data["code"]}.csv"'})

    async def admin(self, request):
        self.auth(request, ('a',))
        return page('Метрики — Люми', admin_html(analytics.metrics(self.store)))

    async def api_me(self, request):
        uid = self.auth(request, ('s', 't'))
        return web.json_response(student_data(self.store, uid), dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def api_class(self, request):
        uid = self.auth(request, ('t',))
        return web.json_response(self.teacher_class(request, uid, request.match_info['code']),
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def api_metrics(self, request):
        self.auth(request, ('a',))
        return web.json_response(analytics.metrics(self.store), dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def manifest(self, request):
        return web.json_response(MANIFEST, content_type='application/manifest+json',
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def service_worker(self, request):
        return web.Response(text=SERVICE_WORKER, content_type='application/javascript',
                            headers={'Cache-Control': 'no-cache'})

    async def health(self, request):
        return web.json_response({'ok': True, 'gigachat': self.info.get('gigachat', 'unknown'),
                                  'uptime_s': int(time.time() - self.info.get('started', time.time()))})

    async def static(self, request):
        name = request.match_info['name']
        path = ASSETS / name
        if '/' in name or not name.endswith('.png') or not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={'Cache-Control': 'public, max-age=86400'})

    def application(self):
        app = web.Application()
        app.add_routes([
            web.get('/', self.landing), web.get('/health', self.health), web.get('/static/{name}', self.static),
            web.get('/app', self.student), web.get('/teacher', self.teacher),
            web.get('/teacher/{code}.csv', self.teacher_csv), web.get('/admin', self.admin),
            web.get('/api/v1/me', self.api_me), web.get('/api/v1/class/{code}', self.api_class),
            web.get('/api/v1/metrics', self.api_metrics),
            web.get('/manifest.webmanifest', self.manifest), web.get('/sw.js', self.service_worker)])
        return app


async def start(store, info, host='0.0.0.0', port=8080):
    runner = web.AppRunner(WebApp(store, info).application(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    LOG.info('Веб-интерфейс запущен на порту %s', port)
    return runner
