class _Any:
    def __init__(self, *a, **k):
        pass
AIORateLimiter = Application = CallbackQueryHandler = CommandHandler = JobQueue = _Any
MessageHandler = PreCheckoutQueryHandler = _Any
class ChatMemberHandler(_Any):
    MY_CHAT_MEMBER = 1
class ContextTypes:
    DEFAULT_TYPE = object
class _F:
    def __and__(self, o): return self
    def __invert__(self): return self
class filters:
    TEXT = COMMAND = SUCCESSFUL_PAYMENT = _F()
