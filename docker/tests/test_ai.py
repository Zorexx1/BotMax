import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from answers import AI, AIError, FilterError, LearningError, Queue, QuizError

PROFILE = {'group': '8–9 класс', 'interests': ['Игры'], 'filters': '', 'nick': 'Нео'}

RAW_QUEST = {
    'allowed': True, 'title': 'Скин мечты', 'emoji': '🎮', 'role': 'Геймер', 'goal': 'Купить скин',
    'intro': 'Распродажа!', 'finale': 'Готово.', 'lesson': 'Процент — сотая часть.',
    'stages': [
        {'kind': 'calc', 'scene': 'Скин 800 рублей, скидка 25%.', 'question': 'Скидка в рублях?',
         'options': [], 'answer': '200 рублей', 'hints': ['Найди четверть.', 'Раздели на 4.'],
         'success': 'Ура!', 'explain': '800 / 4 = 200.'},
        {'kind': 'choice', 'scene': 'Два магазина.', 'question': 'Где дешевле?',
         'options': ['Магазин А', 'Магазин Б', 'Одинаково'], 'answer': 'магазин б',
         'hints': ['Сравни.', 'Меньше — лучше.'], 'success': 'Сэкономил!', 'explain': 'Б дешевле.'},
        {'kind': 'calc', 'scene': 'Налог 10% к 600 рублям.', 'question': 'Итого в рублях?',
         'options': [], 'answer': '660 рублей', 'hints': ['Найди 10%.', 'Прибавь.'],
         'success': 'Хватило!', 'explain': '600 + 60 = 660.'}]}


def solver(answers, valid=True):
    return json.dumps({'checks': [{'id': i, 'valid': valid, 'answer': a, 'reason': ''}
                                  for i, a in enumerate(answers)]}, ensure_ascii=False)


class QuestAITests(unittest.IsolatedAsyncioTestCase):
    def ai(self):
        result = AI.__new__(AI)
        result.request = AsyncMock()
        return result

    def test_parse_quest_keeps_choice_key_after_shuffle(self):
        with patch('answers.random.SystemRandom.shuffle', side_effect=lambda items: items.reverse()):
            quest = AI.parse_quest(copy.deepcopy(RAW_QUEST), 3)
        stage = quest['stages'][1]
        self.assertEqual(stage['options'][stage['correct']], 'Магазин Б')
        self.assertEqual(stage['answer'], 'Магазин Б')
        self.assertEqual(quest['stages'][0]['options'], [])

    def test_parse_quest_repairs_small_mistakes(self):
        data = copy.deepcopy(RAW_QUEST)
        data['stages'][0]['hints'] = ['Получится 200, проверь.']          # подсказка выдаёт ответ
        data['stages'][0]['kind'] = 'Вычисление'
        data['stages'][1]['answer'] = 'Б'                                  # ответ буквой
        data['stages'][1]['options'].append('Магазин В')                   # 4 варианта
        data['stages'][2]['explain'] = 'Шаг. ' * 500                       # слишком длинно
        del data['lesson']
        quest = AI.parse_quest(data, 3, 'Проценты')
        first, second, third = quest['stages']
        self.assertEqual(first['kind'], 'calc')
        self.assertEqual(len(first['hints']), 2)
        self.assertFalse(AI.leaks_answer(first['answer'], first['hints']))
        self.assertEqual(second['options'][second['correct']], 'Магазин Б')
        self.assertEqual(len(second['options']), 3)
        self.assertLessEqual(len(third['explain']), 1500)
        self.assertIn('Проценты', quest['lesson'])
        # для задачи дня лишние этапы отрезаем
        self.assertEqual(len(AI.parse_quest(copy.deepcopy(RAW_QUEST), 1)['stages']), 1)

    def test_flat_schema_and_inline_options_like_real_gigachat(self):
        flat = {'allowed': True, 'title': 'Процентный квест: покупка скина в игре', 'emoji': '🎮',
                'role': 'Геймер', 'goal': 'Купить скин', 'intro': 'Распродажа!',
                's1_scene': 'Скин стоит 3500 рублей, скидка 20%.', 's1_kind': 'calc',
                's1_question': 'Найди стоимость скина после скидки в рублях.', 's1_options': [],
                's1_answer': '2 800 рублей', 's1_hint1': 'Найди 20% от цены.', 's1_hint2': 'Вычти скидку.',
                's1_explain': '3500 × 0,2 = 700; 3500 − 700 = 2800.', 's1_success': 'Отлично!',
                's2_scene': 'Денег пока не хватает.', 's2_kind': 'choice',
                's2_question': 'Выбери действие: а) отложить покупку; б) взять кредит у друга; в) поискать ещё скидки',
                's2_options': 'отложить покупку; взять кредит у друга; поискать ещё скидки',
                's2_answer': 'в) поискать ещё скидки', 's2_hint1': 'Подумай.', 's2_hint2': 'Сравни.',
                's2_explain': 'Скидка экономит деньги.', 's2_success': 'Нашёл!',
                'finale': 'Скин куплен.', 'lesson': 'Процент — сотая часть.'}
        quest = AI.parse_quest(AI.unflatten(flat, 3), 3, 'Проценты')
        self.assertEqual(len(quest['stages']), 2)
        choice = quest['stages'][1]
        self.assertEqual(choice['options'][choice['correct']], 'поискать ещё скидки')
        self.assertNotIn('кредит', choice['question'])
        self.assertEqual(quest['stages'][0]['hints'], ['Найди 20% от цены.', 'Вычти скидку.'])
        schema = AI.quest_schema(1)
        self.assertIn('s1_question', schema)
        self.assertNotIn('s2_question', schema)

    def test_parse_quest_rejects_what_cannot_be_repaired(self):
        cases = []
        no_answer = copy.deepcopy(RAW_QUEST)
        del no_answer['stages'][0]['answer']
        cases.append((no_answer, 3))
        cases.append(({**copy.deepcopy(RAW_QUEST), 'stages': []}, 3))
        cases.append(({**copy.deepcopy(RAW_QUEST), 'stages': copy.deepcopy(RAW_QUEST['stages'][:1])}, 3))
        broken_daily = {**copy.deepcopy(RAW_QUEST), 'stages': copy.deepcopy(RAW_QUEST['stages'][1:2])}
        broken_daily['stages'][0]['options'] = ['Магазин Б']
        cases.append((broken_daily, 1))
        for data, stages in cases:
            with self.subTest(data=str(data)[:60]), self.assertRaises(ValueError):
                AI.parse_quest(data, stages)

    def test_broken_choice_stage_is_dropped_when_others_are_fine(self):
        for options, answer in ((['Магазин Б'], 'Магазин Б'), (['Магазин А', 'Магазин Б'], 'Магазин Q')):
            data = copy.deepcopy(RAW_QUEST)
            data['stages'][1].update(options=options, answer=answer)
            quest = AI.parse_quest(data, 3)
            self.assertEqual([s['answer'] for s in quest['stages']], ['200 рублей', '660 рублей'])

    def test_options_in_any_shape(self):
        expected = ['Астана', 'Алматы', 'Шымкент']
        shapes = ('Астана, Алматы, Шымкент', 'А) Астана Б) Алматы В) Шымкент', ['А) Астана', 'Б) Алматы', 'В) Шымкент'],
                  'Астана; Алматы; Шымкент', [{'text': 'Астана'}, {'text': 'Алматы'}, {'text': 'Шымкент'}],
                  ['Астана', 'Алматы', 'Астана', 'Шымкент'])
        for raw in shapes:
            with self.subTest(raw=raw):
                self.assertEqual(AI.as_options(raw), expected)
        self.assertEqual(AI.as_options(['В. И. Ленин', '3.5 кг']), ['В. И. Ленин', '3.5 кг'])
        data = copy.deepcopy(RAW_QUEST)
        data['stages'][1].update(options=[], question='Где дешевле: А) Магазин А Б) Магазин Б В) Одинаково')
        stage = AI.parse_quest(data, 3)['stages'][1]
        self.assertEqual((stage['kind'], stage['answer'], stage['question']), ('choice', 'Магазин Б', 'Где дешевле?'))

    def test_match_answer_with_a_range(self):
        self.assertTrue(AI.match_answer('от 1000 до 1013 гПа', '1000–1013 гПа'))
        self.assertIsNone(AI.match_answer('от 1000 до 1013 гПа', '1000–1020 гПа'))

    def test_match_answer(self):
        cases = [('1 200 рублей', '1200', True), ('200 рублей', '300', False),
                 ('200 рублей', 'ответ: 200 р.', True), ('200', 'было 800, стало 200', True),
                 ('200', '200 или 300', None), ('12 Ом', 'двенадцать', None),
                 ('-5 °C', '5', False), ('-5 °C', '−5', True), ('0,5 кг', '0.5', True),
                 ('660 рублей', '660,004', True), ('Магазин Б', 'магазин б', True), ('7', '', False),
                 ('12 км и 3 часа', '12 км', None)]
        for key, given, expected in cases:
            with self.subTest(key=key, given=given):
                self.assertIs(AI.match_answer(key, given), expected)

    async def test_verify_quest_hides_keys_and_rejects_mismatch(self):
        ai = self.ai()
        quest = AI.parse_quest(copy.deepcopy(RAW_QUEST), 3)
        ai.request.return_value = solver(['200 руб.', 'Магазин Б', '600 рублей'])
        rejected, reasons = await ai.verify_quest(PROFILE, quest)
        self.assertEqual(rejected, [2])
        self.assertIn('660', reasons[0])
        sent = ai.request.call_args.args[1][0]['content']
        self.assertNotIn('660 рублей', sent)
        self.assertNotIn('"answer"', sent)
        # решатель перестраховался (valid=false), но ответ совпал, значит этап оставляем
        ai.request.return_value = solver(['200', 'магазин б', '660'], valid=False)
        self.assertEqual((await ai.verify_quest(PROFILE, quest))[0], [])
        ai.request.return_value = json.dumps({'checks': [{'id': '0', 'valid': True, 'answer': '200'}]})
        self.assertEqual((await ai.verify_quest(PROFILE, quest))[0], [1, 2])
        ai.request.return_value = '{"checks": []}'
        with self.assertRaises(QuizError):
            await ai.verify_quest(PROFILE, quest)

    async def test_create_quest_regenerates_with_feedback(self):
        ai = self.ai()
        good = solver(['200', 'Магазин Б', '660'])
        bad = solver(['100', 'Магазин Б', '660'])
        ai.request.side_effect = [json.dumps(RAW_QUEST), bad, json.dumps(RAW_QUEST), good]
        quest = await ai.create_quest(PROFILE, 'Проценты', 'магазин скинов')
        self.assertEqual(quest['topic'], 'Проценты')
        self.assertEqual(len(quest['stages']), 3)
        retry_context = ai.request.call_args_list[2].args[1]
        self.assertIn('Проверяющий нашёл ошибки', retry_context[-1]['content'])
        self.assertIn('магазин скинов', ai.request.call_args_list[0].args[2])
        self.assertEqual(ai.request.call_args_list[0].kwargs['temperature'], 0.7)

    async def test_create_quest_keeps_only_verified_stages_after_retry(self):
        ai = self.ai()
        bad = solver(['200', 'Магазин Б', '1'])
        ai.request.side_effect = [json.dumps(RAW_QUEST), bad, json.dumps(RAW_QUEST), bad]
        quest = await ai.create_quest(PROFILE, 'Проценты')
        self.assertEqual([s['answer'] for s in quest['stages']], ['200 рублей', 'Магазин Б'])
        all_bad = solver(['1', 'Магазин А', '1'])
        ai.request.side_effect = [json.dumps(RAW_QUEST), all_bad, json.dumps(RAW_QUEST), all_bad]
        with self.assertRaises(LearningError):
            await ai.create_quest(PROFILE, 'Проценты')

    async def test_verified_stages_survive_a_broken_second_attempt(self):
        ai = self.ai()
        ai.request.side_effect = [json.dumps(RAW_QUEST), solver(['200', 'Магазин Б', '1']), '{"сломано": 1}', '{}']
        quest = await ai.create_quest(PROFILE, 'Проценты')
        self.assertEqual([s['answer'] for s in quest['stages']], ['200 рублей', 'Магазин Б'])

    async def test_truncated_structured_answer_is_retried(self):
        ai = AI.__new__(AI)
        ai.queue = Queue(1)
        cut = SimpleNamespace(finish_reason='length', message=SimpleNamespace(content='', function_call=None))
        ok = SimpleNamespace(finish_reason='function_call', message=SimpleNamespace(content='', function_call=
            SimpleNamespace(name='submit_result', arguments={'allowed': True, 'correct': True})))
        ai.client = SimpleNamespace(achat=AsyncMock(side_effect=[
            SimpleNamespace(choices=[cut]), SimpleNamespace(choices=[ok])]))
        stage = AI.parse_quest(copy.deepcopy(RAW_QUEST), 3)['stages'][0]
        self.assertEqual(await ai.check_answer(PROFILE, stage, 'двести'), {'correct': True})
        self.assertIn('короче', ai.client.achat.call_args.args[0].messages[-1].content)

    async def test_filter_refusal_and_broken_structure(self):
        ai = self.ai()
        ai.request.return_value = json.dumps({'allowed': False})
        with self.assertRaises(FilterError):
            await ai.create_quest(PROFILE, 'Ужасы')
        ai.request.return_value = json.dumps({'allowed': True, 'title': 'x'})
        with self.assertRaises(LearningError):
            await ai.create_quest(PROFILE, 'Проценты')
        self.assertIn('строго по схеме', ai.request.call_args.args[1][-1]['content'])

    async def test_topic_card_validation(self):
        ai = self.ai()
        ai.request.return_value = json.dumps({
            'allowed': True, 'kind': 'topic', 'topic': ' Закон  Ома ', 'subject': 'Физика',
            'hook': 'Зарядка телефона — это закон Ома.',
            'quests': [{'emoji': '🔋', 'title': 'Очень длинное название квеста про зарядку телефона',
                        'pitch': 'Почини зарядку.'},
                       {'emoji': '', 'title': 'Гитара', 'pitch': 'Подключи усилитель.'}]})
        card = await ai.topic_card(PROFILE, 'закон ома', [{'role': 'assistant', 'content': 'x'}])
        self.assertEqual(card['topic'], 'Закон Ома')
        self.assertLessEqual(len(card['quests'][0]['title']), 32)
        self.assertEqual(card['quests'][1]['emoji'], '🎮')
        self.assertEqual(ai.request.call_args.args[1][-1]['content'], 'закон ома')
        ai.request.return_value = json.dumps({'allowed': True, 'kind': 'question', 'topic': 'Ом',
                                              'subject': '', 'hook': '', 'quests': []})
        self.assertEqual((await ai.topic_card(PROFILE, 'а почему?'))['kind'], 'question')
        ai.request.return_value = json.dumps({'allowed': True, 'kind': 'joke'})
        with self.assertRaises(LearningError):
            await ai.topic_card(PROFILE, 'привет')

    async def test_teach_score_is_coerced(self):
        ai = self.ai()
        base = {'allowed': True, 'praise': 'Верно.', 'fix': 'Добавь пример.', 'question': 'А если?'}
        ai.request.return_value = json.dumps({**base, 'score': '85'})
        self.assertEqual((await ai.teach(PROFILE, [], 'Проценты'))['score'], 85)
        ai.request.return_value = json.dumps({**base, 'score': 140.2})
        self.assertEqual((await ai.teach(PROFILE, [], 'Проценты'))['score'], 100)

    async def test_check_answer_returns_only_boolean(self):
        ai = self.ai()
        ai.request.return_value = json.dumps({'allowed': True, 'correct': True, 'answer': '660'})
        stage = AI.parse_quest(copy.deepcopy(RAW_QUEST), 3)['stages'][2]
        self.assertEqual(await ai.check_answer(PROFILE, stage, 'шестьсот шестьдесят'), {'correct': True})
        data = json.loads(ai.request.call_args.args[1][0]['content'])
        self.assertEqual(data['student_answer'], 'шестьсот шестьдесят')


class RequestTests(unittest.IsolatedAsyncioTestCase):
    def client(self, message):
        return SimpleNamespace(achat=AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(finish_reason='stop', message=message)])))

    async def test_structured_response_uses_function_arguments(self):
        with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test'}):
            ai = AI()
        result = {'questions': []}
        ai.client = self.client(SimpleNamespace(content='', function_call=SimpleNamespace(
            name='submit_result', arguments=result)))
        text = await ai.request(PROFILE, [], 'Составь тест', schema=AI.quiz_schema())
        self.assertEqual(json.loads(text), result)
        payload = ai.client.achat.call_args.args[0]
        self.assertEqual(payload.function_call.name, 'submit_result')
        required = payload.functions[0].parameters.required
        self.assertEqual(required[:6], ['q1_question', 'q1_explanation', 'q1_answer',
                                        'q1_wrong1', 'q1_wrong2', 'q1_wrong3'])
        self.assertEqual(len(required), 18)
        self.assertEqual(payload.temperature, 0.1)

    async def test_prompt_persona_profile_and_history(self):
        with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test'}):
            ai = AI()
        ai.client = self.client(SimpleNamespace(content='Ответ'))
        p = {'group': '5–7 класс', 'interests': ['Игры'], 'filters': 'без ужасов', 'nick': 'Нео'}
        self.assertEqual(await ai.explain(p, [{'role': 'user', 'content': 'Радуга'}]), 'Ответ')
        system = ai.client.achat.call_args.args[0].messages[0].content
        for part in ('Люми', 'без ужасов', 'Нео', 'LaTeX'):
            self.assertIn(part, system)
        long_history = [{'role': role, 'content': str(i)} for i in range(9) for role in ('user', 'assistant')]
        await ai.explain(p, long_history + [{'role': 'user', 'content': 'Ещё вопрос'}])
        messages = ai.client.achat.call_args.args[0].messages
        self.assertEqual(messages[1].role, 'user')
        self.assertEqual(messages[-1].content, 'Ещё вопрос')

    async def test_request_attaches_image_to_last_user_only(self):
        ai = AI.__new__(AI)
        ai.queue = Queue(1)
        ai.client = self.client(SimpleNamespace(content='Разбор'))
        history = [{'role': 'user', 'content': 'Фото'}]
        await ai.request(PROFILE, history, 'Прочитай фото', attachments=['image-1'], model='vision-test')
        payload = ai.client.achat.call_args.args[0]
        self.assertEqual(payload.messages[-1].attachments, ['image-1'])
        self.assertEqual(payload.model, 'vision-test')
        self.assertNotIn('attachments', history[-1])

    def test_second_instance_is_rejected_before_polling(self):
        from func import acquire_run_lock
        fake = unittest.mock.MagicMock()
        fake.bind.side_effect = OSError('address in use')
        with patch('func.socket.socket', return_value=fake):
            with self.assertRaises(SystemExit):
                acquire_run_lock()
        fake.close.assert_called_once()


class PhotoAITests(unittest.IsolatedAsyncioTestCase):
    def ai(self):
        result = AI.__new__(AI)
        result.request = AsyncMock()
        result.queue = Queue(1)
        return result

    async def test_photo_upload_attachment_cleanup(self):
        ai = self.ai()
        ai.client = SimpleNamespace(aupload_file=AsyncMock(return_value=SimpleNamespace(id_='file-1')),
                                    adelete_file=AsyncMock())
        ai.request.return_value = 'На фото задача.'
        with patch.dict('os.environ', {'GIGACHAT_VISION_MODEL': 'vision-test'}):
            await ai.analyze_image(PROFILE, b'photo-bytes', 'image/png', 'Разбери вторую задачу')
        self.assertEqual(ai.client.aupload_file.call_args.args[0], ('task.png', b'photo-bytes', 'image/png'))
        self.assertEqual(ai.request.call_args.kwargs['attachments'], ['file-1'])
        self.assertEqual(ai.request.call_args.kwargs['model'], 'vision-test')
        ai.client.adelete_file.assert_awaited_once_with('file-1')

    async def test_photo_cleanup_runs_when_generation_fails_or_is_cancelled(self):
        for failure in (AIError('timeout'), asyncio.CancelledError()):
            ai = self.ai()
            ai.client = SimpleNamespace(aupload_file=AsyncMock(return_value=SimpleNamespace(id_='file-2')),
                                        adelete_file=AsyncMock())
            ai.request.side_effect = failure
            with self.assertRaises(type(failure)):
                await ai.analyze_image(PROFILE, b'photo', 'image/jpeg')
            ai.client.adelete_file.assert_awaited_once_with('file-2')

    async def test_unsupported_or_oversize_photo_never_uploaded(self):
        ai = self.ai()
        ai.client = SimpleNamespace(aupload_file=AsyncMock())
        for payload, mime in [(b'photo', 'image/svg+xml'), (b'x' * (15 * 1024 * 1024 + 1), 'image/png')]:
            with self.assertRaises(LearningError):
                await ai.analyze_image(PROFILE, payload, mime)
        ai.client.aupload_file.assert_not_awaited()


class BossQuizTests(unittest.IsolatedAsyncioTestCase):
    # вопросы для босса: генерация, перемешивание, проверка ключей

    async def test_quiz_repairs_invalid_json(self):
        ai = AI.__new__(AI)
        questions = [{'question': 'Q', 'answer': 'a', 'distractors': ['b', 'c', 'd'], 'explanation': 'E'}
                     for _ in range(3)]
        valid = json.dumps({'questions': questions})
        ai.request = AsyncMock(side_effect=['{"questions": [}', 'Тест:\n```json\n' + valid + '\n```'])
        ai.verify_quiz = AsyncMock()
        result = await ai.quiz({}, [{'role': 'user', 'content': 'Радуга'}])
        for q in result:
            self.assertEqual(q['options'][q['correct']], 'a')
        # правильный ответ среди неверных вариантов - убираем его, вопрос остаётся с тремя вариантами
        questions[0]['distractors'][0] = ' A '
        fixed = ai.parse_quiz(json.dumps({'questions': questions}))
        self.assertEqual(len(fixed), 3)
        self.assertEqual(sorted(fixed[0]['options']), ['a', 'c', 'd'])
        questions = [{**q, 'distractors': ['a', 'A']} for q in questions]
        with self.assertRaises(AIError):
            ai.parse_quiz(json.dumps({'questions': questions}))

    def test_flat_quiz_from_server_logs(self):
        # так GigaChat прислал тест на сервере, только в плоских полях: хвосты кавычек и повторы
        raw = {'q1_question': 'Что обязательно указывается в амбулаторном эпикризе?\", ',
               'q1_explanation': 'Диагноз — ключевой элемент.', 'q1_answer': 'Диагноз\", ',
               'q1_wrong1': 'Результаты опросов друзей пациента', 'q1_wrong2': 'Диагноз', 'q1_wrong3': 'План отпуска',
               'q2_question': 'Когда оформляют эпикриз?', 'q2_explanation': 'В конце лечения.',
               'q2_answer': 'После завершения курса лечения', 'q2_wrong1': 'Перед началом лечения',
               'q2_wrong2': 'При первом посещении врача', 'q2_wrong3': ''}
        parsed = AI.parse_quiz(json.dumps(raw, ensure_ascii=False))
        self.assertEqual([q['answer'] for q in parsed], ['Диагноз', 'После завершения курса лечения'])
        self.assertEqual(parsed[0]['question'], 'Что обязательно указывается в амбулаторном эпикризе?')
        self.assertEqual(sorted(parsed[0]['options']),
                         ['Диагноз', 'План отпуска', 'Результаты опросов друзей пациента'])
        self.assertEqual(len(parsed[1]['options']), 3)

    def test_quiz_formats_seen_in_the_wild(self):
        question = {'question': 'Столица Казахстана?', 'explanation': 'С 1997 года.', 'answer': 'Астана'}
        variants = [{**question, 'distractors': 'Алматы, Шымкент, Караганда'},
                    {**question, 'distractors': ['Астана', 'Алматы', 'Шымкент', 'Караганда']},
                    {**question, 'distractors': ['Алматы', 'Шымкент', 'Караганда', 'Актобе']},
                    {'question': question['question'], 'options': ['Алматы', 'Астана', 'Шымкент', 'Караганда'],
                     'correct': 1}]
        for raw in variants:
            with self.subTest(raw=raw):
                parsed = AI.parse_quiz(json.dumps({'questions': [raw]}), count=3)
                self.assertEqual(len(parsed), 1)
                q = parsed[0]
                self.assertEqual(q['options'][q['correct']], 'Астана')
                self.assertEqual(len(q['options']), 4)
        as_string = json.dumps({'questions': json.dumps([variants[0]], ensure_ascii=False)}, ensure_ascii=False)
        self.assertEqual(AI.parse_quiz(as_string)[0]['answer'], 'Астана')

    async def test_quiz_collects_missing_questions_in_next_attempt(self):
        ai = AI.__new__(AI)
        good = {'question': 'Q', 'explanation': 'E', 'answer': 'a', 'distractors': ['b', 'c', 'd']}
        first = json.dumps({'questions': [dict(good, question='Q1'), dict(good, question='Q2'),
                                          {'question': 'плохой'}]})
        second = json.dumps({'questions': [dict(good, question='Q3')]})
        ai.request = AsyncMock(side_effect=[first, second])
        ai.verify_quiz = AsyncMock()
        result = await ai.quiz({}, [{'role': 'user', 'content': 'Радуга'}])
        self.assertEqual([q['question'] for q in result], ['Q1', 'Q2', 'Q3'])
        self.assertIn('Ровно 1 вопроса', ai.request.call_args_list[1].args[2])

    async def test_verifier_rejects_wrong_key(self):
        ai = AI.__new__(AI)
        questions = [{'question': 'Почему закат красный?', 'options': ['Длинный путь света', 'Температура'],
                      'answer': 'Температура', 'correct': 1, 'explanation': 'Длинный путь света.'}]
        ai.request = AsyncMock(return_value=json.dumps({'checks': [
            {'id': 0, 'valid': True, 'answer': 'Длинный путь света'}]}))
        with self.assertRaises(AIError):
            await ai.verify_quiz({}, questions)
        questions[0].update(answer='Длинный путь света', correct=0)
        await ai.verify_quiz({}, questions)
        # решатель назвал номер и текст варианта - это тот же ответ, а не ошибка ключа
        ai.request.return_value = json.dumps({'checks': [{'id': 0, 'valid': True, 'answer': '1) Длинный путь света'}]})
        await ai.verify_quiz({}, questions)

    async def test_partial_rejection_keeps_good_questions(self):
        ai = AI.__new__(AI)

        def raw(names):
            return json.dumps({'questions': [{'question': name, 'answer': 'a',
                'distractors': ['b', 'c', 'd'], 'explanation': 'E'} for name in names]})
        ai.request = AsyncMock(side_effect=[raw(['good1', 'bad', 'good2']), raw(['replacement'])])
        ai.verify_quiz = AsyncMock(side_effect=[QuizError('Два правильных ответа', rejected=[1]), None])
        questions = await ai.quiz({}, [])
        self.assertEqual([q['question'] for q in questions], ['good1', 'good2', 'replacement'])


if __name__ == '__main__':
    unittest.main()


class CredentialsTests(unittest.TestCase):
    # как обычно портят Authorization Key при вставке (ключи выдуманные)
    CID = '11111111-2222-3333-4444-555555555555'
    SECRET = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

    def setUp(self):
        import base64
        self.key = base64.b64encode(f'{self.CID}:{self.SECRET}'.encode()).decode()

    def test_good_key_and_common_paste_mistakes_are_fixed(self):
        from answers import normalize_credentials
        self.assertEqual(normalize_credentials(self.key), (self.key, [], ''))
        for raw in (f'"{self.key}"', f'Basic {self.key}', f' {self.key[:50]}\n{self.key[50:]}\r',
                    self.key.rstrip('='), f'{self.CID}:{self.SECRET}', f'﻿{self.key}'):
            with self.subTest(raw=raw):
                key, fixes, problem = normalize_credentials(raw)
                self.assertEqual((key, problem), (self.key, ''))
                self.assertTrue(fixes)

    def test_secret_instead_of_key_and_garbage_are_reported(self):
        from answers import normalize_credentials
        self.assertIn('Client Secret', normalize_credentials(self.SECRET)[2])
        self.assertIn('не читается', normalize_credentials('ключ-из-кабинета!')[2])
        self.assertIn('обрезан', normalize_credentials(self.key[:60])[2])
        self.assertEqual(normalize_credentials('')[2], 'ключ пустой')

    def test_client_id_and_secret_pair_from_env(self):
        from answers import credentials_from_env
        with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': '', 'GIGACHAT_CLIENT_ID': self.CID,
                                       'GIGACHAT_CLIENT_SECRET': self.SECRET}):
            self.assertEqual(credentials_from_env()[0], self.key)


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_waits_for_live_requests(self):
        from answers import BACKGROUND
        ai = AI.__new__(AI)
        ai.queue = Queue(2)
        order = []

        async def live():
            async with ai.slot():
                order.append('live start')
                await asyncio.sleep(0.05)
                order.append('live end')

        async def background():
            BACKGROUND.set(True)
            await asyncio.sleep(0.01)
            async with ai.slot():
                order.append('background')
        await asyncio.gather(live(), background())
        self.assertEqual(order, ['live start', 'live end', 'background'])

    async def test_person_interrupts_background_when_no_room(self):
        from answers import BACKGROUND
        queue = Queue(1)
        started = asyncio.Event()

        async def background():
            BACKGROUND.set(True)
            async with queue.turn(background=True):
                started.set()
                await asyncio.sleep(10)
        job = asyncio.create_task(background())
        await started.wait()
        async with asyncio.timeout(1):
            async with queue.turn():
                self.assertEqual(queue.running, 1)
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual((queue.running, queue.people, queue.background), (0, 0, set()))

    async def test_background_leaves_a_place_for_people(self):
        from answers import BACKGROUND
        queue = Queue(2)
        inside = []

        async def background():
            BACKGROUND.set(True)
            async with queue.turn(background=True):
                inside.append(queue.running)
                await asyncio.sleep(0.05)
        await asyncio.gather(background(), background())
        self.assertEqual(inside, [1, 1])  # по одному, второе место свободно

    async def test_single_stream_background_waits_for_a_pause(self):
        from answers import BACKGROUND
        queue = Queue(1)
        queue.QUIET, queue.POLL = 0.3, 0.05
        async with queue.turn():
            pass
        started = asyncio.get_running_loop().time()

        async def background():
            BACKGROUND.set(True)
            async with queue.turn(background=True):
                return asyncio.get_running_loop().time() - started
        self.assertGreaterEqual(await background(), 0.25)

    async def test_rate_limit_with_parallel_requests_switches_to_one(self):
        queue = Queue(2)
        async with queue.turn():
            async with queue.turn():
                queue.crowded()
        self.assertEqual(queue.limit, 1)
        order = []

        async def person(name):
            async with queue.turn():
                order.append(name + ' start')
                await asyncio.sleep(0.01)
                order.append(name + ' end')
        await asyncio.gather(person('a'), person('b'))
        self.assertEqual(order, ['a start', 'a end', 'b start', 'b end'])

    async def test_rate_limit_is_retried(self):
        ai = AI.__new__(AI)
        ai.queue = Queue(1)
        ok = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='Ответ'))])
        busy = Exception('url', 429, b'{"message":"Too many requests"}', {})
        ai.client = SimpleNamespace(achat=AsyncMock(side_effect=[busy, ok]))
        with patch('answers.asyncio.sleep', new=AsyncMock()):
            self.assertEqual(await ai.request(PROFILE, [{'role': 'user', 'content': 'x'}], 'y'), 'Ответ')
        self.assertEqual(ai.client.achat.await_count, 2)


class QuestPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_topics_without_calculations_get_choice_stages(self):
        ai = AI.__new__(AI)
        ai._learning_object = AsyncMock(side_effect=LearningError('стоп'))
        for stages in (1, 3):
            with self.assertRaises(LearningError):
                await ai.create_quest(PROFILE, 'Казахи', stages=stages)
            instruction = ai._learning_object.call_args.args[2]
            self.assertIn('все этапы kind=choice', instruction)
            self.assertNotIn('Хотя бы два этапа kind=calc', instruction)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def ai(self, behaviour):
        ai = AI.__new__(AI)
        ai.queue = Queue(1)
        ai.client = SimpleNamespace(astream=behaviour)
        return ai

    @staticmethod
    def chunk(text):
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

    async def test_stream_retries_only_before_first_words(self):
        calls = []

        async def flaky(payload):
            calls.append(1)
            if len(calls) == 1:
                raise Exception('url', 429, b'{"message":"Too many requests"}', {})
            for part in ('Казахи — ', 'коренной народ Казахстана.'):
                yield self.chunk(part)
        ai = self.ai(flaky)
        with patch('answers.asyncio.sleep', new=AsyncMock()):
            parts = [text async for text in ai.stream(PROFILE, [{'role': 'user', 'content': 'казахи'}], 'x')]
        self.assertEqual(parts[-1], 'Казахи — коренной народ Казахстана.')
        self.assertEqual(len(calls), 2)

        async def broken(payload):
            yield self.chunk('Начало')
            raise Exception('url', 429, b'{}', {})
        with self.assertRaises(AIError):
            [text async for text in self.ai(broken).stream(PROFILE, [], 'x')]

    def test_explanation_starts_with_direct_answer(self):
        ai = AI.__new__(AI)
        task = ai.explain_task({'group': '10–11 класс'})
        self.assertIn('Казахи — это', task)
        self.assertIn('уточни', task)
        self.assertIn('без формул', task)
        from answers import PERSONA
        self.assertIn('нейтрально и уважительно', PERSONA)
        self.assertNotIn('суперсила', PERSONA)
