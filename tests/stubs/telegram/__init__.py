"""Минимальные заглушки python-telegram-bot для офлайн-тестов логики."""


class _Obj:
    def __init__(self, *args, **kwargs):
        self.args = args
        for k, v in kwargs.items():
            setattr(self, k, v)


class Update(_Obj):
    ALL_TYPES = ["all"]


class BotCommand(_Obj):
    pass


class CopyTextButton(_Obj):
    pass


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None, pay=None, copy_text=None, **kw):
        self.text, self.callback_data, self.url, self.pay = text, callback_data, url, pay


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard

    def datas(self):
        return [b.callback_data for row in self.inline_keyboard for b in row if b.callback_data]


class InputMediaPhoto:
    def __init__(self, media, caption=None, parse_mode=None):
        self.media, self.caption = media, caption


class LabeledPrice(_Obj):
    pass
