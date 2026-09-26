import contextvars
import threading

_current_collector = contextvars.ContextVar("fluxer_usage_collector", default=None)

def _to_int(value):
    try:
        n = int(value)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0

def _pick(d, *names):
    if not isinstance(d, dict):
        return None
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None

def normalize_usage(raw, style="openai"):
    if not isinstance(raw, dict) or not raw:
        return None

    inp = out = reasoning = total = 0

    if style == "anthropic":
        inp = _to_int(raw.get("input_tokens"))
        out = _to_int(raw.get("output_tokens"))

        inp += _to_int(raw.get("cache_creation_input_tokens"))
        inp += _to_int(raw.get("cache_read_input_tokens"))

    elif style == "google":
        inp = _to_int(raw.get("promptTokenCount"))
        cand = _to_int(raw.get("candidatesTokenCount"))
        reasoning = _to_int(raw.get("thoughtsTokenCount"))

        out = cand + reasoning
        total = _to_int(raw.get("totalTokenCount"))

    elif style == "cohere":
        src = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw.get("billed_units")
        if not isinstance(src, dict):
            src = raw
        inp = _to_int(src.get("input_tokens"))
        out = _to_int(src.get("output_tokens"))

    else:
        inp = _to_int(_pick(raw, "prompt_tokens", "input_tokens"))
        out = _to_int(_pick(raw, "completion_tokens", "output_tokens"))
        total = _to_int(raw.get("total_tokens"))
        details = raw.get("completion_tokens_details") or raw.get("output_tokens_details")
        if isinstance(details, dict):
            reasoning = _to_int(details.get("reasoning_tokens"))

    if not total:
        total = inp + out

    if not (inp or out or total):
        return None

    return {
        "input_tokens": inp,
        "output_tokens": out,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "calls": 1,
    }

class UsageCollector:

    def __init__(self):
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.total_tokens = 0
        self.calls = 0
        self.calls_without_usage = 0

    def add(self, usage):
        with self._lock:
            if not usage:
                self.calls_without_usage += 1
                return
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
            self.reasoning_tokens += usage["reasoning_tokens"]
            self.total_tokens += usage["total_tokens"]
            self.calls += usage.get("calls", 1)

    def to_dict(self):
        with self._lock:
            if self.calls == 0:
                return None
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "total_tokens": self.total_tokens,
                "calls": self.calls,

                "partial": self.calls_without_usage > 0,
            }

def start_collecting():
    collector = UsageCollector()
    token = _current_collector.set(collector)
    return collector, token

def stop_collecting(token):
    try:
        _current_collector.reset(token)
    except (ValueError, LookupError):
        _current_collector.set(None)

def record_usage(raw_usage, style="openai"):
    collector = _current_collector.get()
    if collector is None:
        return
    collector.add(normalize_usage(raw_usage, style))
