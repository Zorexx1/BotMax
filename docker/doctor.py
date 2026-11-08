# Проверка связи с GigaChat и MAX:
#   docker compose run --rm bot python doctor.py
#   docker compose run --rm bot python doctor.py --quest   (ещё и пробный квест, тратит токены)
# Ключи не печатает, в базу ничего не пишет.
import asyncio
import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

OK, FAIL, WARN = '✅', '❌', '⚠️'
problems = []


def step(mark, title, detail=''):
    print(f'{mark} {title}' + (f'\n   {detail}' if detail else ''), flush=True)
    if mark == FAIL:
        problems.append(title)


def check_env():
    print('\n— 1. Настройки .env —')
    from answers import credentials_from_env
    raw = os.getenv('GIGACHAT_CREDENTIALS', '')
    token = os.getenv('MAX_BOT_TOKEN', '')
    step(OK if token else FAIL, 'MAX_BOT_TOKEN задан' if token else 'MAX_BOT_TOKEN пустой')
    key, fixes, problem = credentials_from_env()
    if not key:
        step(FAIL, 'GIGACHAT_CREDENTIALS пустой')
    elif problem:
        step(FAIL, f'GIGACHAT_CREDENTIALS ({len(raw)} символов): {problem}')
    else:
        step(OK, f'GIGACHAT_CREDENTIALS — правильный Authorization Key ({len(key)} символов)',
             ('Бот сам исправил: ' + ', '.join(fixes) + '. Лучше поправь и в .env') if fixes else '')
    scope = os.getenv('GIGACHAT_SCOPE', 'GIGACHAT_API_PERS')
    step(OK if scope in ('GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP') else WARN,
         f'GIGACHAT_SCOPE = {scope}')
    print(f"   GIGACHAT_MODEL = {os.getenv('GIGACHAT_MODEL') or 'GigaChat-2'}, "
          f"GIGACHAT_BASE_URL = {os.getenv('GIGACHAT_BASE_URL') or 'https://api.giga.chat/v1'}")
    proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy')
    if proxy:
        print('   Для GigaChat используется прокси из HTTPS_PROXY')
    cert = Path(os.getenv('GIGACHAT_CA_BUNDLE_FILE') or Path(__file__).parent / 'certs' / 'russian_trusted_root_ca_pem.crt')
    step(OK if cert.exists() else FAIL, f'Сертификат Минцифры: {cert}', '' if cert.exists() else 'файл не найден')


def where_am_i():
    print('\n— 2. Где находится сервер —')
    try:
        with urllib.request.urlopen('https://ipinfo.io/json', timeout=8) as response:
            info = json.load(response)
        country = info.get('country', '?')
        step(OK if country == 'RU' else WARN, f"Страна сервера: {country} ({info.get('city', '?')}, {info.get('org', '?')})",
             '' if country == 'RU' else 'Сбер часто не пускает к авторизации GigaChat с зарубежных серверов')
    except Exception as exc:
        step(WARN, 'Не удалось определить страну сервера', type(exc).__name__)


def tcp(host, port):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=8) as raw:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # проверяем только сеть, сертификат проверит SDK
            with context.wrap_socket(raw, server_hostname=host):
                pass
        step(OK, f'{host}:{port} доступен ({(time.monotonic() - start) * 1000:.0f} мс)')
        return True
    except Exception as exc:
        step(FAIL, f'{host}:{port} недоступен', f'{type(exc).__name__}: {str(exc)[:150]}')
        return False


async def check_gigachat():
    from answers import AI, describe_error, error_hint
    print('\n— 4. GigaChat —')
    ai = AI()
    try:
        try:
            token = await ai.client.aget_token()
            minutes = max(0, (token.expires_at / 1000 - time.time()) / 60)
            step(OK, f'Авторизация прошла, токен действует ~{minutes:.0f} мин')
        except Exception as exc:
            step(FAIL, 'Авторизация не прошла', f'{describe_error(exc)}\n   Что делать: {error_hint(exc)}')
            return
        try:
            models = sorted(m.id_ for m in (await ai.client.aget_models()).data)
            step(OK, 'Доступные модели: ' + ', '.join(models))
            for name in (os.getenv('GIGACHAT_MODEL') or 'GigaChat-2',
                         os.getenv('GIGACHAT_VISION_MODEL') or 'GigaChat-2-Pro'):
                if name not in models:
                    step(WARN, f'Модели {name} нет в списке', 'поменяй значение в .env на одну из доступных')
        except Exception as exc:
            step(FAIL, 'Список моделей не получен', f'{describe_error(exc)}\n   Что делать: {error_hint(exc)}')
        profile = {'group': '8–9 класс', 'interests': ['Игры'], 'filters': '', 'nick': 'Тест'}
        try:
            text = await ai.request(profile, [{'role': 'user', 'content': 'Скажи одним предложением, что такое процент.'}],
                                    'Ответь коротко.', max_tokens=200)
            step(OK, 'Обычный ответ ИИ получен', text[:200])
        except Exception as exc:
            step(FAIL, 'Обычный ответ ИИ не получен', f'{exc}\n   Что делать: {error_hint(exc.__cause__ or exc)}')
            return
        try:
            card = await ai.topic_card(profile, 'проценты')
            step(OK, f"Карточка темы (структурированный ответ): {card['kind']} / {card['topic']}",
                 ' | '.join(q['title'] for q in card['quests']))
        except Exception as exc:
            step(FAIL, 'Структурированный ответ не получен', str(exc)[:300])
        if '--quest' in sys.argv:
            await check_quests(ai, profile)
    finally:
        await ai.close()


async def check_quests(ai, profile):
    print('\n— 5. Пробные квесты (займёт до пары минут) —', flush=True)
    for title, stages in (('Задача дня', 1), ('Квест из 3 этапов', 3)):
        start = time.monotonic()
        try:
            quest = await ai.create_quest(profile, 'Проценты', 'покупка скина в игре со скидкой', stages)
        except Exception as exc:
            step(FAIL, f'{title}: не собрался за {time.monotonic() - start:.0f} с',
                 f'{exc}\n   Причины отказов смотри выше в строках «Ответ ИИ отклонён» и «Проверка квеста»')
            continue
        lines = [f"«{quest['title']}»"] + [
            f"Этап {i}: [{s['kind']}] {s['question']} → {s['answer']}" for i, s in enumerate(quest['stages'], 1)]
        step(OK, f'{title}: собран за {time.monotonic() - start:.0f} с', '\n   '.join(lines))


def main():
    import logging
    logging.basicConfig(level=logging.WARNING, format='   %(levelname)s %(message)s')
    load_dotenv(Path(__file__).with_name('.env'))
    check_env()
    where_am_i()
    print('\n— 3. Сеть —')
    base = (os.getenv('GIGACHAT_BASE_URL') or 'https://api.giga.chat/v1').split('/')[2]
    auth_ok = True
    if os.getenv('HTTPS_PROXY') or os.getenv('https_proxy'):
        print('   Задан HTTPS_PROXY: прямую проверку сети пропускаю, GigaChat проверю через прокси ниже.')
    else:
        auth_ok = tcp('ngw.devices.sberbank.ru', 9443)
        tcp(base, 443)
    tcp('platform-api2.max.ru', 443)
    if not auth_ok:
        print('   → Без ngw.devices.sberbank.ru:9443 бот не получит токен, и ИИ работать не будет.')
    if os.getenv('GIGACHAT_CREDENTIALS'):
        asyncio.run(check_gigachat())
    print('\n— Итог —')
    if problems:
        print(f'{FAIL} Найдены проблемы: ' + '; '.join(problems))
        sys.exit(1)
    print(f'{OK} Всё в порядке: GigaChat отвечает.')


if __name__ == '__main__':
    main()
