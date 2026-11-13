class TelegramError(Exception):
    pass
class BadRequest(TelegramError):
    pass
class Forbidden(TelegramError):
    pass
class NetworkError(TelegramError):
    pass
class TimedOut(NetworkError):
    pass
class RetryAfter(TelegramError):
    retry_after = 1
