import asyncio
import io
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


class PhotoError(Exception):
    pass


class PublicResolver(aiohttp.resolver.ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await super().resolve(host, port, family)
        if any(not ipaddress.ip_address(record['host']).is_global for record in records):
            raise PhotoError('📷 Пока не получилось открыть фото. Пришли его как изображение ещё раз — попробуем вместе!')
        return records


def validate_image_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
            raise ValueError
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if parsed.hostname.lower() == 'localhost':
            raise ValueError
    except (ValueError, TypeError):
        raise PhotoError('📷 Пока не получилось открыть фото. Пришли его как изображение ещё раз — попробуем вместе!') from None
    return url


def normalize_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise PhotoError('📷 Пришли фото до 10 МБ. Можно вырезать только само задание — так его будет проще разобрать!')
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in ('JPEG', 'PNG', 'WEBP'):
                raise PhotoError('📷 Пришли фото или скриншот в формате JPEG, PNG или WebP — разберём задание вместе!')
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise PhotoError('📷 Фото великовато. Уменьши его до 20 мегапикселей или вырежи только само задание.')
            source.verify()
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            # GigaChat не берёт webp
            out = io.BytesIO()
            oriented = ImageOps.exif_transpose(source).convert('RGBA')
            background = Image.new('RGBA', oriented.size, 'white')
            Image.alpha_composite(background, oriented).convert('RGB').save(out, format='PNG')
            result = out.getvalue()
            if len(result) > MAX_IMAGE_BYTES:
                raise PhotoError('📷 Фото великовато для разбора. Пришли отдельный фрагмент задания — начнём с него!')
            return result, 'image/png'
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise PhotoError('📷 Пока не получается прочитать фото. Пришли более чёткий снимок или скриншот — попробуем ещё раз!') from None


async def download_image(url):
    # без редиректов во внутреннюю сеть и без токена бота в запросе
    try:
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=30, connect=10)) as session:
            for _ in range(4):
                validate_image_url(url)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get('Location', ''))
                        continue
                    response.raise_for_status()
                    if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                        raise PhotoError('📷 Пришли фото до 10 МБ. Можно вырезать только само задание — так его будет проще разобрать!')
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        if len(chunks) > MAX_IMAGE_BYTES:
                            raise PhotoError('📷 Пришли фото до 10 МБ. Можно вырезать только само задание — так его будет проще разобрать!')
                    return await asyncio.to_thread(normalize_image, bytes(chunks))
        raise PhotoError('📷 Пока не получилось открыть фото. Пришли его ещё раз — попробуем вместе!')
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise PhotoError('📷 Фото не загрузилось. Возможно, ссылка уже устарела. Пришли изображение ещё раз — попробуем вместе!') from None


def image_attachment(message):
    attachments = getattr(message.body, 'attachments', None) or []
    if not attachments:
        return None
    if len(attachments) != 1:
        raise PhotoError('📷 Давай разберём по одному заданию! Пришли одно фото или скриншот за раз.')
    attachment = attachments[0]
    if attachment.type == 'file':
        if Path(getattr(attachment, 'filename', '') or '').suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
            raise PhotoError('📷 Помогу с текстом или фото в формате JPEG, PNG, WebP. Если задание в документе, пришли его скриншот!')
        if (getattr(attachment, 'size', 0) or 0) > MAX_IMAGE_BYTES:
            raise PhotoError('📷 Пришли фото до 10 МБ. Можно вырезать только само задание — так его будет проще разобрать!')
    elif attachment.type != 'image':
        raise PhotoError('📝 Пришли текст задания, фото или скриншот в формате JPEG, PNG, WebP. Разберёмся вместе!')
    return attachment.payload.url
