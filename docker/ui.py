import html
import re

from maxapi.types import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from tex import to_text


def h(text):
    return html.escape(str(text), quote=False)


def plain(text):
    return h(to_text(str(text)))


def rich(text):
    # модель любит markdown, MAX понимает html
    lines = []
    for line in h(to_text(str(text))).split('\n'):
        stripped = line.strip()
        heading = re.match(r'^#{1,6}\s*(.+)$', stripped)
        if heading:
            line = '<b>' + heading.group(1).strip('* ') + '</b>'
        elif re.match(r'^[*•]\s+', stripped):
            line = '• ' + re.sub(r'^[*•]\s+', '', stripped)
        line = re.sub(r'\*\*([^*\n]+?)\*\*', r'<b>\1</b>', line)
        line = re.sub(r'__([^_\n]+?)__', r'<b>\1</b>', line)
        lines.append(line)
    return '\n'.join(lines)


def chunks(text, size=3500):
    # режем по строкам, чтобы не разорвать тег (лимит сообщения MAX 4000)
    parts, current = [], ''
    for line in text.split('\n'):
        while len(line) > size:
            if current:
                parts.append(current)
                current = ''
            parts.append(line[:size])
            line = line[size:]
        candidate = current + ('\n' if current else '') + line
        if len(candidate) > size:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current or not parts:
        parts.append(current)
    return parts


def link(text, url):
    return LinkButton(text=text, url=url)


def keyboard(rows):
    # элемент строки: (текст, payload) или готовая кнопка, None пропускаем
    builder = InlineKeyboardBuilder()
    count = 0
    for row in rows:
        buttons = [item if not isinstance(item, tuple) else CallbackButton(text=item[0], payload=item[1])
                   for item in row if item]
        if buttons:
            builder.row(*buttons)
            count += 1
    return [builder.as_markup()] if count else []


def topic_menu():
    # под объяснением: всё про текущую тему, по две кнопки в ряд, чтобы подписи помещались
    return keyboard([[('🎮 Квест по теме', 'go:quest'), ('✍️ Свой сюжет', 'own')],
                     [('🧠 Проще', 'ai:simple'), ('🚀 Подробнее', 'ai:deeper')],
                     [('💡 Пример', 'ai:example'), ('🌍 Зачем это мне', 'go:why')],
                     [('⚔️ Битва с боссом', 'go:boss'), ('🎓 Объясню сам', 'go:teach')],
                     [('🏠 На главную', 'home')]])


def home_menu(extra=(), top=()):
    rows = [list(row) for row in top]
    rows += [[('✨ Новая тема', 'new'), ('🎲 Удиви меня', 'go:random')],
             [('🗓 Задача дня', 'go:daily'), ('🪪 Мой герой', 'go:me')],
             list(extra)]
    return keyboard(rows)


def resume_menu():
    return keyboard([[('▶️ Продолжить', 'resume')], [('🚪 Выйти', 'mode:stop')]])


def events_text(events):
    return ('\n\n' + '\n'.join(events)) if events else ''
