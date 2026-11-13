import json
MODE = {"fail": False}
class _Msg:
    def __init__(self, c): self.content = c
class _Choice:
    def __init__(self, c): self.message = _Msg(c)
class _Resp:
    def __init__(self, c): self.choices = [_Choice(c)]
class _Completions:
    def create(self, model, messages, max_tokens, temperature):
        if MODE["fail"]:
            raise RuntimeError("api down")
        data = json.loads(messages[1]["content"])
        body = ("**Текст** толкования. " * 40) + "\n\n\n" + ("Абзац про " + json.dumps(data, ensure_ascii=False)[:80] + ". ") * 5
        return _Resp(body)
class _Chat:
    completions = _Completions()
class OpenAI:
    def __init__(self, **k):
        self.chat = _Chat()
