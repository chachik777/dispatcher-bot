import asyncio
import email
from email.message import EmailMessage
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _install_stubs():
    """Стабы для telegram и aiohttp, чтобы тестировать парсер без внешних пакетов."""
    if "telegram" not in sys.modules:
        telegram = types.ModuleType("telegram")
        telegram.Bot = MagicMock(return_value=MagicMock())
        telegram.InlineKeyboardButton = MagicMock()
        telegram.InlineKeyboardMarkup = MagicMock()
        sys.modules["telegram"] = telegram

        telegram_ext = types.ModuleType("telegram.ext")
        builder = MagicMock()
        builder.token.return_value = builder
        builder.connect_timeout.return_value = builder
        builder.read_timeout.return_value = builder
        builder.write_timeout.return_value = builder
        builder.build.return_value = MagicMock()
        app_cls = MagicMock()
        app_cls.builder.return_value = builder
        telegram_ext.Application = app_cls
        telegram_ext.MessageHandler = MagicMock()
        telegram_ext.CallbackQueryHandler = MagicMock()
        filters_mock = MagicMock()
        filters_mock.TEXT = 1
        filters_mock.COMMAND = 2
        telegram_ext.filters = filters_mock
        sys.modules["telegram.ext"] = telegram_ext

        telegram_error = types.ModuleType("telegram.error")
        for exc_name in ("ChatMigrated", "TimedOut", "NetworkError", "TelegramError"):
            setattr(telegram_error, exc_name, type(exc_name, (Exception,), {}))
        sys.modules["telegram.error"] = telegram_error

    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.web = MagicMock()
        sys.modules["aiohttp"] = aiohttp


_install_stubs()
import dispatcher_bot as bot  # noqa: E402


class TestZvonokPhraseParsing(unittest.TestCase):
    def test_html_email_preserves_turns_and_skips_style(self):
        html = """
        <html>
          <head><style>body { font-family: Arial; }</style></head>
          <body>
            <p>phone: +79001234567</p>
            <p>call_id: 12345</p>
            <div><b>Робот:</b> Здравствуйте! Что у вас сломалось?</div>
            <div><b>Клиент:</b> Холодильник не морозит</div>
            <div><b>Робот:</b> Назовите адрес</div>
            <div><b>Клиент:</b> На Широтной 120, квартира 45</div>
          </body>
        </html>
        """
        msg = EmailMessage()
        msg["Subject"] = "Заявка zvonok.com"
        msg.add_alternative(html, subtype="html")
        body = bot.extract_body_text(msg)
        self.assertNotIn("font-family", body)
        parsed = bot.parse_zvonok(body)
        self.assertIsNotNone(parsed)
        text, cat = parsed
        self.assertEqual(cat, "refrigerators")
        self.assertIn("Холодильник не морозит", text)
        self.assertIn("Широтная, 120, квартира 45", text)

    def test_cp1251_email_decoding(self):
        msg = email.message.Message()
        msg["Content-Type"] = "text/plain; charset=windows-1251"
        raw_text = (
            "phone: 89123456789\n"
            "Робот: Что случилось?\n"
            "Клиент: Стиральная машина воду не сливает\n"
        )
        msg.set_payload(raw_text.encode("cp1251"))
        body = bot.extract_body_text(msg)
        parsed = bot.parse_zvonok(body)
        self.assertIsNotNone(parsed)
        text, cat = parsed
        self.assertEqual(cat, "appliances")
        self.assertIn("Стиральная машина воду не сливает", text)

    def test_buttons_word_does_not_trigger_computers_pk(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Что сломалось?\n"
            "Клиент: На холодильнике кнопки мигают и не морозит\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "refrigerators")
        self.assertIn("кнопки мигают", text)

    def test_install_washer_does_not_go_to_computers(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Чем помочь?\n"
            "Клиент: Нужно установить стиральную машину на Ленина 10\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "appliances")
        self.assertIn("Ленина, 10", text)

    def test_robot_summary_hijack_vacuum_prevented(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Здравствуйте, что сломалось?\n"
            "Клиент: Пылесос Дайсон щетка не крутится и гудит\n"
            "Робот: Хорошо, я поняла, у вас пылесос. Назовите ваш адрес\n"
            "Клиент: Республики 50\n"
            "Робот: Хорошо, мастер по ремонту перезвонит вам\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "vacuum")
        self.assertIn("щетка не крутится", text)
        self.assertNotIn("мастер по ремонту перезвонит", text)
        self.assertNotIn("Назовите ваш", text)

    def test_price_and_time_questions_do_not_drop_issue(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Какая поломка?\n"
            "Клиент: Здравствуйте, алло, меня слышно? Холодильник потек и не холодит, сколько стоит ремонт и когда приедет мастер?\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "refrigerators")
        self.assertIn("Холодильник потек и не холодит", text)
        self.assertNotIn("сколько стоит", text.lower())

    def test_today_and_verno_do_not_drop_issue(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Что случилось?\n"
            "Клиент: Да-да, телевизор сегодня перестал включаться, звука нет, верно\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "tv")
        self.assertIn("звука нет", text)

    def test_net_is_preserved_in_clean_text(self):
        cleaned = bot.clean_text("Здравствуйте, ну у меня нет звука и нет изображения, спасибо")
        self.assertIn("нет звука", cleaned.lower())
        self.assertIn("нет изображения", cleaned.lower())

    def test_trubka_not_treated_as_price_question(self):
        self.assertFalse(bot.is_price_question("Трубка кондиционера обмерзла"))
        self.assertTrue(bot.is_price_question("Сколько стоит ремонт?"))

    def test_merged_asr_fragments_and_device_hint(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Что сломалось?\n"
            "Клиент: Ну у меня посудомойка Бош\n"
            "Клиент: воду не греет и ошибку выдает\n"
            "Робот: Назовите адрес\n"
            "Клиент: Дом двадцать пять по улице Полевой, третий подъезд, 48 квартира\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "appliances")
        self.assertIn("посудомойка Бош", text)
        self.assertIn("воду не греет", text)
        self.assertIn("Полевая, 25, подъезд 3, квартира 48", text)

    def test_no_fake_house_numbers_from_age_size_or_price(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Что случилось?\n"
            "Клиент: Телевизор диагональ 55, машинке года 3, озвучили тысяч 5, приду после 5 вечера\n"
            "Клиент: Потух экран\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertIn("📍 Адрес: не указано", text)
        self.assertIn("Потух экран", text)

    def test_contextual_refusal_vs_partial_negation(self):
        # Частичное отрицание в фразе не должно сбрасывать заявку
        valid_body = (
            "phone: +79991112233\n"
            "Робот: Что случилось?\n"
            "Клиент: Мастера сказали плату менять не надо, только датчик, холодильник не морозит\n"
            "Клиент: Широтная 10\n"
        )
        self.assertIsNotNone(bot.parse_zvonok(valid_body))

        # Ответ "не надо" на завершающий вопрос ПОСЛЕ указания адреса не должен сбрасывать заявку
        completed_body = (
            "phone: +79991112233\n"
            "Робот: Что сломалось?\n"
            "Клиент: Стиральная машина не сливает\n"
            "Робот: Адрес?\n"
            "Клиент: Ленина 15, кв 2\n"
            "Робот: Еще вопросы остались?\n"
            "Клиент: Нет, не надо, спасибо\n"
        )
        self.assertIsNotNone(bot.parse_zvonok(completed_body))

        # Явный отказ без адреса должен отклоняться
        refused_body = (
            "phone: +79991112233\n"
            "Робот: Оформить заявку на выезд мастера?\n"
            "Клиент: Нет, спасибо, мне это не нужно\n"
        )
        self.assertIsNone(bot.parse_zvonok(refused_body))

        # Автоответчик должен отклоняться
        voicemail_body = (
            "phone: +79991112233\n"
            "Робот: Алло, здравствуйте\n"
            "Клиент: Абонент временно недоступен, оставьте сообщение после сигнала\n"
        )
        self.assertIsNone(bot.parse_zvonok(voicemail_body))

    def test_robot_own_branch_address_ignored_for_exit_categories(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Мы находимся на улице Широтная 99. Что у вас сломалось?\n"
            "Клиент: Холодильник не морозит\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "refrigerators")
        self.assertIn("📍 Адрес: не указано", text)

    def test_excluded_tech_context_safety(self):
        # Телевизор с упоминанием пульта ДУ не должен отсеиваться
        self.assertFalse(bot.is_excluded_tech("💬 Проблема: Телевизор не реагирует на пульт ДУ", "tv"))
        # Ноутбук с шумящим вентилятором не должен отсеиваться
        self.assertFalse(bot.is_excluded_tech("💬 Проблема: У ноутбука сильно шумит вентилятор", "computers"))
        # Плойка (в т.ч. без явного слова PS5) не отсеивается, пока нет парикмахерского контекста
        self.assertFalse(bot.is_excluded_tech("💬 Проблема: Игровая плойка PS5 не включается", "console"))
        self.assertFalse(bot.is_excluded_tech("💬 Проблема: Плойка не включается", "console"))
        self.assertEqual(bot.detect_category("Плойка не включается, к телевизору не подключается"), "console")
        # Плойка с парикмахерским контекстом ("для волос", "локоны", "завивка") отсеивается
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Не греет плойка для волос", "other"))
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Сломалась плойка для завивки локонов", "other"))
        # Обычный водонагреватель и бойлер ЧИНИМ (appliances, не исключаются)
        self.assertFalse(bot.is_excluded_tech("💬 Проблема: Водонагреватель не греет воду", "appliances"))
        self.assertFalse(bot.is_excluded_tech("💬 Проблема: Течёт электрический бойлер", "appliances"))
        self.assertEqual(bot.detect_category("Водонагреватель не греет"), "appliances")
        self.assertEqual(bot.detect_category("Бойлер перестал нагревать"), "appliances")
        # Всё, что связано с газом, а также фен и сломанный пульт — отсеиваются
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Сломался фен", "other"))
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Газовая плита не зажигается", "appliances"))
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Газовый водонагреватель тухнет", "appliances"))
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Газовая колонка не зажигается", "speaker"))
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Газовый котёл выдаёт ошибку", "other"))
        self.assertTrue(bot.is_excluded_tech("💬 Проблема: Сломался пульт ду от телевизора", "tv"))
        # Слово "Стефана" не должно срабатывать на "фен"
        self.assertFalse(bot.is_excluded_tech("💬 Проблема: Не включается\n📍 Адрес: Стефана, 5", "tv"))

    def test_contextual_flat_answer(self):
        body = (
            "phone: +79991112233\n"
            "Робот: Что случилось?\n"
            "Клиент: Кондиционер капает с внутреннего блока\n"
            "Робот: Какая улица и дом?\n"
            "Клиент: Монтажников 14\n"
            "Робот: А какая квартира?\n"
            "Клиент: 72\n"
        )
        text, cat = bot.parse_zvonok(body)
        self.assertEqual(cat, "cond")
        self.assertIn("Монтажников, 14, квартира 72", text)

    def test_street_inflections_and_spoken_50_let(self):
        body1 = (
            "phone: +79991112233\n"
            "Робот: Что случилось?\n"
            "Клиент: Холодильник не морозит, на Московском тракте 120 корпус 2, 3 подъезд, 5 этаж, кв 45\n"
        )
        text1, cat1 = bot.parse_zvonok(body1)
        self.assertEqual(cat1, "refrigerators")
        self.assertIn("Московский тракт, 120, корпус 2, подъезд 3, этаж 5, квартира 45", text1)

        body2 = (
            "phone: +79991112233\n"
            "Робот: Что случилось?\n"
            "Клиент: Телевизор не включается, Пятьдесят лет Октября 62\n"
        )
        text2, cat2 = bot.parse_zvonok(body2)
        self.assertEqual(cat2, "tv")
        self.assertIn("50 лет Октября, 62", text2)

    def test_category_priority_console_vs_tv_and_contact_phone(self):
        self.assertEqual(bot.detect_category("Приставка PS5 к телевизору не подключается"), "console")
        self.assertEqual(bot.detect_category("Телевизор не работает, перезвоните на телефон"), "tv")
        self.assertEqual(bot.detect_category("Разбил экран на телефоне"), "phone")
        self.assertEqual(bot.detect_category("Крупная бытовая техника"), "appliances")

    def test_imap_marks_seen_on_rejected_zvonok_and_split_turn_excluded(self):
        msg_refusal = EmailMessage()
        msg_refusal["From"] = "noreply@zvonok.com"
        msg_refusal["Subject"] = "Call report"
        msg_refusal.set_content(
            "phone: +79991112233\n"
            "call_id: 1\n"
            "Робот: Оформить выезд мастера?\n"
            "Клиент: Нет, спасибо, мне это не нужно\n"
        )

        msg_split_fen = EmailMessage()
        msg_split_fen["From"] = "noreply@zvonok.com"
        msg_split_fen["Subject"] = "Call report"
        msg_split_fen.set_content(
            "phone: +79991112244\n"
            "call_id: 2\n"
            "Робот: Какая техника сломалась?\n"
            "Клиент: Фен\n"
            "Робот: Что именно случилось?\n"
            "Клиент: Не включается\n"
        )

        fake_mail = MagicMock()
        fake_mail.search.return_value = ("OK", [b"1 2"])
        fake_mail.fetch.side_effect = [
            ("OK", [(b"1 (RFC822)", msg_refusal.as_bytes())]),
            ("OK", [(b"2 (RFC822)", msg_split_fen.as_bytes())]),
        ]

        orig_connect = bot._connect_to_imap
        orig_loop = bot.main_loop
        try:
            bot._connect_to_imap = MagicMock(return_value=fake_mail)
            bot.main_loop = MagicMock()
            bot._imap_task()
        finally:
            bot._connect_to_imap = orig_connect
            bot.main_loop = orig_loop

        # Оба отклонённых письма должны быть помечены \Seen, чтобы не парситься бесконечно каждую минуту
        stored_nums = [call.args[0] for call in fake_mail.store.call_args_list]
        self.assertEqual(stored_nums, [b"1", b"2"])

    def test_other_parsers_no_regression(self):
        msg_text, cat = bot.build_message_from_json({
            "name": "Иван",
            "phone": "89001112233",
            "category": "Стиральная машина",
            "problem": "Не отжимает",
        })
        self.assertEqual(cat, "appliances")
        self.assertIn("+7 (900) 111-22-33", msg_text)

    def test_real_screenshots_regression(self):
        # Реальный пример 1 (IMG_2471 / IMG_2474): PS5 + адрес филиала с опечаткой 'Шширотной'
        ps5_body = (
            "Пришла заявка\n"
            "Телефон: +79829165618\n"
            "Разговор: Робот: Здравствуйте. Это Эльвира из сервисного центра. Что у вас случилось\n"
            "Клиент: Плейстейшн 5 почистить от пыли и заменить жидкий металл.\n"
            "Робот: Плейстейшн пят — это игровая приставка, её мы принимаем только в филиале, "
            "ждем васлеширотной 29 Корпус 1 дробь 1, будни с 9 до 19. Вход со стороны широтой, "
            "рядом со стоматологией, Зеленое крыльцо, 2 этаж.\n"
            "Робот: Проверьте: игровая приставка сониплейстейшн 5, чистка от пыли, "
            "филиал на Шширотной К. Мастер перезвонит через 15 минут.\n"
        )
        t1, c1 = bot.parse_zvonok(ps5_body)
        self.assertEqual(c1, "console")
        self.assertIn("Плейстейшн 5 почистить от пыли и заменить жидкий металл", t1)
        self.assertNotIn("Шширотной", t1)
        self.assertIn("📍 Адрес: филиал (привоз)", t1)

        # Реальный пример 2 (IMG_2470 / IMG_2473): микроволновка + 'Алло, нужно...' + собственный адрес филиала
        mw_body = (
            "Пришла заявка\n"
            "Телефон: +79324753821\n"
            "Разговор: Робот: Здравствуйте. Это Эльвира из сервисного центра. Я\n"
            "Клиент: Алло.\n"
            "Робот: Алло, я на линии. Что случилось с вашей техникой?\n"
            "Клиент: Алло, нужно отремонтировать микроволновку.\n"
            "Робот: Микроволновк — это мелкая техника, её можно принять только в нашем филиале. У вас есть марка?\n"
            "Робот: Хорошо, самсунг. Ждем вас в филиале на широтной 29 Корпус 1 дробь 1 будни от 9 до 19.\n"
            "___________\n"
            "phone: +79324753821\n"
            "duration: 43\n"
        )
        t2, c2 = bot.parse_zvonok(mw_body)
        self.assertEqual(c2, "microwave")
        self.assertIn("💬 Проблема: нужно отремонтировать микроволновку (Самсунг)", t2)
        self.assertNotIn("29", t2)
        self.assertIn("📍 Адрес: филиал (привоз)", t2)

        # Реальный пример 3 (IMG_2469 / IMG_2472): ноутбук Asus + отказ от выдуманной Нечеволодарской + привоз
        asus_body = (
            "Пришла заявка\n"
            "Телефон: +79058208723\n"
            "Разговор: Робот: Здравствуйте. Это Эльвира из сервисного центра. Что у вас случилось.\n"
            "Клиент: Не работает клавиатура на ноутбуке.\n"
            "Робот: Поняла, проблема с клавиатурой ноутбука? Марко устройство помните.\n"
            "Клиент: Да, конечно, ноутбук, Асус, модель. Сейчас секунду, подскажу.\n"
            "Робот: Отлично. Вам удобнее?\n"
            "Клиент: Н 750 Джи.\n"
            "Робот: Поняла, адрес для выезда, улица Нечеволодарская, дом 7, Корпус 5. Уточните, пожалуйста, номер квартиры.\n"
            "Клиент: Нет, адрес неправильный. Я бы хотел узнать стоимость ремонта приблизительно.\n"
            "Робот: Замена клавиатуры на ноутбуке от 1500 рублей. Оформляем заявку?\n"
            "Клиент: К вам подъехать можно перевезти ноутбук?,\n"
            "Робот: Хорошо, ждем вас в филиале на Шширотной, 29 Корпус 1 дробь 1 будние от 9 до 19.\n"
            "Клиент: Спасибо.\n"
            "Робот: Проверьте: ноутбук Асус, проблема, не работает клавиатура, адрес не указан, мастер перезвонит через 15 минут.\n"
        )
        t3, c3 = bot.parse_zvonok(asus_body)
        self.assertEqual(c3, "computers")
        self.assertIn("💬 Проблема: Не работает клавиатура на ноутбуке (Асус)", t3)
        self.assertNotIn("Нечеволодарская", t3)
        self.assertIn("📍 Адрес: филиал (привоз)", t3)

    def test_ambiguous_street_protection_and_confirmed_addresses(self):
        # Омонимичные прилагательные без адресного контекста НЕ должны стать улицами
        for phrase in (
            "Машинка новая, не сливает воду, 3 года",
            "Старая посудомойка не греет",
            "У меня северная сторона, кондиционер не холодит",
            "Техническая проблема с компьютером, зависает",
        ):
            t, _ = bot.parse_zvonok(f"phone: +79991112233\nКлиент: {phrase}\n")
            self.assertIn("📍 Адрес: не указано", t, msg=f"Ложный адрес для: {phrase}")

        # Но те же слова С явным маркером "улица" или номером дома — корректно распознаются
        t_ok1, _ = bot.parse_zvonok("phone: +79991112233\nКлиент: Улица Новая 14, холодильник не морозит\n")
        self.assertIn("📍 Адрес: Новая, 14", t_ok1)

        t_ok2, _ = bot.parse_zvonok(
            "phone: +79991112233\n"
            "Робот: На какой улице живете?\n"
            "Клиент: На Дружбы\n"
            "Робот: Какой дом?\n"
            "Клиент: сто двадцать восемь\n"
            "Робот: Какой корпус?\n"
            "Клиент: второй\n"
            "Робот: Что случилось?\n"
            "Клиент: Стиралка не отжимает\n"
        )
        self.assertIn("📍 Адрес: Дружбы, 128, корпус 2", t_ok2)

    def test_fuzzy_asr_streets_ungluing_and_prefix_trimming(self):
        t1, c1 = bot.parse_zvonok("phone: +79991112233\nКлиент: наширотной 120, подъезд 3, квартира 15, стиралка не крутит\n")
        self.assertEqual(c1, "appliances")
        self.assertIn("📍 Адрес: Широтная, 120, подъезд 3, квартира 15", t1)
        self.assertIn("💬 Проблема: стиралка не крутит", t1)

        t2, c2 = bot.parse_zvonok("phone: +79991112233\nКлиент: Улица Фидюнинского 62, телевизар не включается\n")
        self.assertEqual(c2, "tv")
        self.assertIn("📍 Адрес: Федюнинского, 62", t2)
        self.assertIn("💬 Проблема: телевизар не включается", t2)

        t3, c3 = bot.parse_zvonok("phone: +79991112233\nКлиент: По Мельникайти 100, корпус 2, холодильник шумит\n")
        self.assertEqual(c3, "refrigerators")
        self.assertIn("📍 Адрес: Мельникайте, 100, корпус 2", t3)

        t4, c4 = bot.parse_zvonok("phone: +79991112233\nКлиент: На улице Пермяково 54, не морозит холодильник\n")
        self.assertEqual(c4, "refrigerators")
        self.assertIn("📍 Адрес: Пермякова, 54", t4)

    def test_dialog_state_model_isolation_and_multi_symptoms(self):
        # Цифра из ответа на вопрос "Какая модель?" ("А 52") не должна попасть в адрес
        t1, c1 = bot.parse_zvonok(
            "phone: +79991112233\n"
            "Клиент: Разбил экран на телефоне\n"
            "Робот: Какая модель телефона?\n"
            "Клиент: А 52\n"
            "Робот: Какая улица?\n"
            "Клиент: Республики 25\n"
        )
        self.assertEqual(c1, "phone")
        self.assertIn("📍 Адрес: Республики, 25", t1)
        self.assertNotIn("52", t1.split("📍 Адрес:")[1])

        # Сборка нескольких уникальных симптомов и подтверждение категории роботом
        t2, c2 = bot.parse_zvonok(
            "phone: +79991112233\n"
            "Клиент: Стиральная машина Индезит не отжимает\n"
            "Робот: Поняла, еще что-то?\n"
            "Клиент: И ещё вода из-под люка течёт\n"
            "Робот: Адрес?\n"
            "Клиент: Менделеева 16 квартира 40\n"
        )
        self.assertEqual(c2, "appliances")
        self.assertIn("не отжимает; вода из-под люка течёт", t2)
        self.assertIn("16, квартира 40", t2)

        t3, c3 = bot.parse_zvonok(
            "phone: +79991112233\n"
            "Робот: У вас холодильник, верно?\n"
            "Клиент: Да\n"
            "Робот: Что случилось?\n"
            "Клиент: Не морозит вообще\n"
        )
        self.assertEqual(c3, "refrigerators")
        self.assertIn("холодильник — Не морозит вообще", t3)

    def test_craftum_and_bothelp_category_isolation(self):
        craft_text, craft_cat = bot.parse_craftum(
            "Имя\nАлексей\nТелефон\n89001112233\n"
            "Какая услуга вас интересует?\nне указана\n"
            "https://service-blizko72.ru/\n"
        )
        self.assertEqual(craft_cat, "other")
        self.assertIn("+7 (900) 111-22-33", craft_text)

        bh_text, bh_cat = bot.parse_bothelp(
            "Имя: Ольга\nphone1: 79001112233\ndevice_brand: Bosch\nproblem: Посудомойка не сливает\n"
        )
        self.assertEqual(bh_cat, "appliances")
        self.assertIn("Bosch", bh_text)


class TestMasterChatCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.active_requests.clear()
        bot.accepted_requests.clear()

    async def asyncTearDown(self):
        for req in list(bot.active_requests.values()):
            t = req.get("timer")
            if t:
                t.cancel()
        bot.active_requests.clear()
        bot.accepted_requests.clear()

    def _make_query(self, data: str, chat_id: int = -1001, msg_id: int = 555, delete_side_effect=None, msg_text=None):
        msg = MagicMock()
        msg.chat_id = chat_id
        msg.message_id = msg_id
        msg.text = msg_text
        msg.caption = None
        msg.delete = AsyncMock(side_effect=delete_side_effect)

        user = MagicMock()
        user.username = "master_ivan"
        user.first_name = "Ivan"

        query = MagicMock()
        query.data = data
        query.message = msg
        query.from_user = user
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        return update, query, msg

    async def test_stale_reject_and_accept_delete_message_from_master_chat(self):
        # Неактивная заявка: нажатие "Не беру" должно удалить сообщение из чата мастеров
        upd_rej, q_rej, msg_rej = self._make_query("reject_999999")
        await bot.handle_callback(upd_rej, None)
        q_rej.answer.assert_awaited_once()
        msg_rej.delete.assert_awaited_once()
        q_rej.edit_message_reply_markup.assert_not_awaited()

        # Неактивная заявка: нажатие "Принимаю" также должно удалить устаревшее сообщение
        upd_acc, q_acc, msg_acc = self._make_query("accept_999998")
        await bot.handle_callback(upd_acc, None)
        q_acc.answer.assert_awaited_once()
        msg_acc.delete.assert_awaited_once()
        q_acc.edit_message_reply_markup.assert_not_awaited()

        # Если удалить нельзя (например, нет прав или >48ч), убираются кнопки (fallback)
        upd_fb, q_fb, msg_fb = self._make_query("reject_999997", delete_side_effect=RuntimeError("Too old"))
        await bot.handle_callback(upd_fb, None)
        msg_fb.delete.assert_awaited_once()
        q_fb.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    async def test_active_reject_deletes_current_and_forwards_or_escalates(self):
        sent_next = MagicMock(chat_id=-1002, message_id=777)
        with patch.object(bot.bot, "send_message", AsyncMock(return_value=sent_next)) as send_mock:
            bot.active_requests[12345] = {
                "text": "🚨 Новая заявка!\n📞 Телефон: +79001112233",
                "current_group_index": 0,
                "groups": [-1001, -1002],
                "timer": None,
                "category": "computers",
                "chat_id": -1001,
                "tg_message_id": 555,
                "transitioning": False,
            }
            upd1, q1, msg1 = self._make_query("reject_12345", chat_id=-1001, msg_id=555)
            await bot.handle_callback(upd1, None)

            msg1.delete.assert_awaited_once()
            self.assertEqual(bot.active_requests[12345]["current_group_index"], 1)
            self.assertEqual(bot.active_requests[12345]["chat_id"], -1002)
            self.assertEqual(bot.active_requests[12345]["tg_message_id"], 777)
            self.assertFalse(bot.active_requests[12345]["transitioning"])

            # Нажатие на старую карточку после передачи должно удалить её и НЕ двигать индекс повторно
            upd_old, q_old, msg_old = self._make_query("reject_12345", chat_id=-1001, msg_id=555)
            await bot.handle_callback(upd_old, None)
            msg_old.delete.assert_awaited_once()
            self.assertEqual(bot.active_requests[12345]["current_group_index"], 1)

            # Отказ в последней группе удаляет карточку и эскалирует в GENERAL_GROUP
            upd2, q2, msg2 = self._make_query("reject_12345", chat_id=-1002, msg_id=777)
            await bot.handle_callback(upd2, None)
            msg2.delete.assert_awaited_once()
            self.assertNotIn(12345, bot.active_requests)
            self.assertEqual(send_mock.await_args_list[-1].kwargs["chat_id"], bot.GENERAL_GROUP)
            self.assertIn("Никто не принял", send_mock.await_args_list[-1].kwargs["text"])

    async def test_timer_expiry_deletes_old_master_message_before_next_group(self):
        sent_next = MagicMock(chat_id=-1002, message_id=888)
        with patch.object(bot.bot, "delete_message", AsyncMock()) as del_mock, \
             patch.object(bot.bot, "send_message", AsyncMock(return_value=sent_next)) as send_mock, \
             patch.object(bot.asyncio, "sleep", AsyncMock()):
            bot.active_requests[22222] = {
                "text": "🚨 Новая заявка!\n📞 Телефон: +79001112233",
                "current_group_index": 0,
                "groups": [-1001, -1002],
                "timer": None,
                "category": "computers",
                "chat_id": -1001,
                "tg_message_id": 600,
                "transitioning": False,
            }
            await bot.start_timer(22222)
            del_mock.assert_awaited_once_with(chat_id=-1001, message_id=600)
            send_mock.assert_awaited_once()
            self.assertEqual(bot.active_requests[22222]["chat_id"], -1002)
            self.assertEqual(bot.active_requests[22222]["tg_message_id"], 888)

    async def test_double_or_concurrent_accept_does_not_delete_pinned_message(self):
        with patch.object(bot.bot, "pin_chat_message", AsyncMock()) as pin_mock, \
             patch.object(bot.bot, "send_message", AsyncMock()) as send_mock:
            bot.active_requests[33333] = {
                "text": "🚨 Новая заявка (BotHelp)!\n👤 Имя: Дарья\n📞 Телефон: +79001112233",
                "current_group_index": 0,
                "groups": [-1001, -1002],
                "timer": None,
                "category": "computers",
                "chat_id": -1001,
                "tg_message_id": 555,
                "transitioning": False,
            }
            upd1, q1, msg1 = self._make_query("accept_33333", chat_id=-1001, msg_id=555)
            upd2, q2, msg2 = self._make_query("accept_33333", chat_id=-1001, msg_id=555)
            upd3, q3, msg3 = self._make_query("reject_33333", chat_id=-1001, msg_id=555)

            # Имитируем параллельное двойное нажатие "Принимаю" и одновременный "Не беру" на ту же карточку
            await asyncio.gather(
                bot.handle_callback(upd1, None),
                bot.handle_callback(upd2, None),
                bot.handle_callback(upd3, None),
            )

            q1.edit_message_text.assert_awaited_once()
            pin_mock.assert_awaited_once_with(chat_id=-1001, message_id=555, disable_notification=True)
            send_mock.assert_awaited_once()
            msg1.delete.assert_not_awaited()
            msg2.delete.assert_not_awaited()
            msg3.delete.assert_not_awaited()
            self.assertIn(33333, bot.accepted_requests)

    async def test_accepted_text_guard_prevents_delete_even_after_restart(self):
        # Даже если память бота сброшена после перезапуска, карточка с "✅ Заявку принял ..." не удаляется
        upd_acc, q_acc, msg_acc = self._make_query(
            "accept_44444",
            msg_text="✅ Заявку принял @Danil_Tumen\n\n🚨 Новая заявка!",
        )
        upd_rej, q_rej, msg_rej = self._make_query(
            "reject_44444",
            msg_text="✅ Заявку принял @Danil_Tumen\n\n🚨 Новая заявка!",
        )
        await bot.handle_callback(upd_acc, None)
        await bot.handle_callback(upd_rej, None)
        msg_acc.delete.assert_not_awaited()
        msg_rej.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
