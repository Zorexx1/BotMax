import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image

from media import PhotoError, PublicResolver, image_attachment, normalize_image, validate_image_url
import test_flows as learning_fixtures


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def test_webp_converted_to_png_and_invalid_bytes_rejected(self):
        stream = io.BytesIO()
        Image.new('RGB', (40, 20), 'white').save(stream, format='WEBP')
        data, mime = normalize_image(stream.getvalue())
        self.assertEqual(mime, 'image/png')
        self.assertTrue(data.startswith(b'\x89PNG'))
        with self.assertRaises(PhotoError):
            normalize_image(b'not an image')
        with self.assertRaises(PhotoError):
            normalize_image(b'x' * (10 * 1024 * 1024 + 1))

    def test_private_urls_and_credentials_rejected(self):
        for url in ('http://i.oneme.ru/image', 'https://127.0.0.1/x',
                    'https://[::1]/x', 'https://user:secret@example.com/x',
                    'https://169.254.169.254/latest', 'https://example.com:8080/x'):
            with self.subTest(url=url), self.assertRaises(PhotoError):
                validate_image_url(url)
        self.assertEqual(validate_image_url('https://i.oneme.ru/i?r=test'), 'https://i.oneme.ru/i?r=test')

    def test_phone_rotation_and_transparent_background_preserved(self):
        stream = io.BytesIO()
        source = Image.new('RGB', (40, 20), 'white')
        exif = source.getexif()
        exif[274] = 6  # Фото хранится боком, EXIF требует повернуть при показе.
        source.save(stream, format='JPEG', exif=exif)
        data, _ = normalize_image(stream.getvalue())
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (20, 40))
        stream = io.BytesIO()
        Image.new('RGBA', (40, 20), (0, 0, 0, 0)).save(stream, format='PNG')
        data, _ = normalize_image(stream.getvalue())
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    async def test_private_dns_address_rejected_at_resolution(self):
        resolver = PublicResolver()
        try:
            with patch('aiohttp.resolver.ThreadedResolver.resolve', new=AsyncMock(
                    return_value=[{'host': '10.0.0.1'}])):
                with self.assertRaises(PhotoError):
                    await resolver.resolve('example.com', 443)
        finally:
            await resolver.close()

    def test_multiple_attachments_and_nonimage_files_rejected(self):
        for attachments in ([SimpleNamespace(type='file', filename='test.pdf')],
                            [SimpleNamespace(type='image')] * 2):
            message = SimpleNamespace(body=SimpleNamespace(attachments=attachments))
            with self.assertRaises(PhotoError):
                image_attachment(message)


class PhotoFlowTests(learning_fixtures.FlowTests):
    def photo_event(self):
        return self.message_event('Объясни это задание', [SimpleNamespace(
            type='image', payload=SimpleNamespace(url='https://i.oneme.ru/image?token=private'))])

    async def test_photo_is_analyzed_and_only_text_is_persisted(self):
        self.store.save(1, self.profile)
        with patch('func.download_image', new=AsyncMock(return_value=(b'png', 'image/png'))):
            await self.app.handle(self.photo_event(), 'text')
        self.ai.analyze_image.assert_awaited_once()
        self.assertEqual(self.ai.analyze_image.call_args.args[1:], (b'png', 'image/png', 'Объясни это задание'))
        saved = self.store.get(1)
        self.assertEqual(saved['state'], 'ready')
        self.assertIn('На фото', saved['history'][-1]['content'])
        self.assertNotIn('token=private', str(saved))

    async def test_photo_does_not_interrupt_quest(self):
        await self.app.text(10, self.profile, '/quest')
        self.store.save(1, self.profile)
        with patch('func.download_image', new=AsyncMock()) as download:
            await self.app.handle(self.photo_event(), 'text')
        download.assert_not_awaited()
        self.assertEqual(self.store.get(1)['quest'], self.profile['quest'])
        self.assertIn('/cancel', self.sent_text())

    async def test_download_failure_preserves_pending_analysis(self):
        self.profile['state'] = 'analyze_input'
        self.store.save(1, self.profile)
        with patch('func.download_image', new=AsyncMock(side_effect=PhotoError('Пришли фото ещё раз.'))):
            await self.app.handle(self.photo_event(), 'text')
        self.assertEqual(self.store.get(1), self.profile)
        self.ai.analyze_image.assert_not_awaited()
        self.assertIn('Пришли фото ещё раз.', self.sent_text())
