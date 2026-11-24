"""Model backends.

The agent keeps a provider-neutral history; each backend turns it into its own
API format. History items:
  {"kind": "user", "text": str}
  {"kind": "assistant", "turn": Turn}          # turn.content is the provider's own reply, passed back unchanged
  {"kind": "tool_results", "results": [ToolOutput, ...]}
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# Claude prices, USD per million tokens: input, output, cache read, cache write (5-minute cache).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass
class ToolOutput:
    id: str
    name: str
    output: str
    is_error: bool


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Turn:
    content: Any             # provider-native reply, appended back unchanged
    tool_calls: list[ToolCall]
    text: str                # visible text the model wrote, for the log
    stop_reason: str | None  # end_turn, tool_use, max_tokens, refusal, pause_turn
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    cost_usd: float = 0.0


class Backend(Protocol):
    name: str

    def step(self, system: str, history: list[dict], tools: list[dict]) -> Turn: ...


class BackendError(Exception):
    """A model call failed in a way the run cannot recover from."""


def cost_usd(model: str, u: Usage) -> float:
    p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])  # unknown model: price high
    return (u.input_tokens * p_in + u.output_tokens * p_out
            + u.cache_read_tokens * p_read + u.cache_write_tokens * p_write) / 1_000_000


# --------------------------------------------------------------------------- Gemini
def _is_network_error(err: BaseException) -> bool:
    """True for dropped connections and timeouts (httpx, httpcore or socket level)."""
    if isinstance(err, (ConnectionError, TimeoutError)):
        return True
    return type(err).__module__.split(".")[0].startswith(("httpx", "httpcore"))


class GeminiBackend:
    """Google Gemini API (free tier). Needs GEMINI_API_KEY in the environment."""

    REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}

    # Tried in order; the first that answers is used until it fails. Newest and strongest first.
    DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                      "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    # Google's documented placeholder for function calls replayed to a model that did not make them.
    SKIP_SIGNATURE = b"skip_thought_signature_validator"

    def __init__(self, models: list[str] | str | None = None, effort: str | None = None,
                 max_output_tokens: int = 32_000, max_retries: int = 6, retries_per_model: int = 1,
                 min_interval_s: float = 0.0, sleep=time.sleep, log=print):
        from google import genai
        from google.genai import errors, types
        self._types, self._errors = types, errors
        self.client = genai.Client()  # reads GEMINI_API_KEY (or GOOGLE_API_KEY)
        if isinstance(models, str):
            models = [models]
        self.models = list(models or self.DEFAULT_MODELS)
        self._idx = 0
        self.effort = effort
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries            # waits for per-minute rate limits, per call
        self.retries_per_model = retries_per_model  # extra tries on 503 before switching model
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._log = log
        self._last_call = 0.0
        self._n = 0
        self.name = "gemini:" + ",".join(self.models)

    @property
    def model(self) -> str:
        return self.models[self._idx]

    def _foreign(self, content):
        """A model turn made by a different model: keep text and calls, drop hidden reasoning,
        and mark function calls with Google's skip-validation signature."""
        t = self._types
        parts = []
        for p in content.parts:
            if p.thought:
                continue
            if p.function_call:
                parts.append(t.Part(function_call=p.function_call, thought_signature=self.SKIP_SIGNATURE))
            elif p.text:
                parts.append(t.Part(text=p.text))
        return t.Content(role="model", parts=parts) if parts else None

    # history -> Gemini contents, for the model about to be called
    def _contents(self, history: list[dict], model: str | None = None) -> list:
        t = self._types
        model = model or self.model
        out = []
        for h in history:
            if h["kind"] == "user":
                out.append(t.Content(role="user", parts=[t.Part(text=h["text"])]))
            elif h["kind"] == "assistant":
                turn = h["turn"]
                content = turn.content
                if content is not None and content.parts:
                    if turn.model and turn.model != model:
                        content = self._foreign(content)
                    if content is not None:
                        out.append(content)  # same model: unchanged, keeps its thought signatures
            else:
                parts = []
                for r in h["results"]:
                    payload = {"error": r.output} if r.is_error else {"output": r.output}
                    fr = t.FunctionResponse(name=r.name, response=payload,
                                            id=None if r.id.startswith("local_") else r.id)
                    parts.append(t.Part(function_response=fr))
                out.append(t.Content(role="user", parts=parts))
        return out

    def _config(self, system: str, tools: list[dict]):
        t = self._types
        decls = [t.FunctionDeclaration(name=s["name"], description=s["description"],
                                       parameters_json_schema=s["input_schema"]) for s in tools]
        kwargs = dict(
            system_instruction=system,
            tools=[t.Tool(function_declarations=decls)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),  # our loop runs tools
            max_output_tokens=self.max_output_tokens,
        )
        if self.effort:
            level = "XHIGH" if self.effort == "max" else self.effort.upper()  # Gemini has no MAX level
            kwargs["thinking_config"] = t.ThinkingConfig(thinking_level=level)
        return t.GenerateContentConfig(**kwargs)

    @staticmethod
    def _retry_delay(err) -> float | None:
        """Read Google's suggested wait (e.g. '27s') from a 429 error, if present."""
        m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(getattr(err, "details", "")))
        return float(m.group(1)) if m else None

    def _call(self, history: list[dict], config):
        """Call the current model; on overload, retirement or daily quota, move to the next model.
        Returns (model_used, response)."""
        e = self._errors
        rate_waits = 0
        net_errors = 0
        while True:
            model = self.model
            contents = self._contents(history, model)
            server_errors = 0
            delay = 5.0
            while True:
                wait = self.min_interval_s - (time.monotonic() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
                self._last_call = time.monotonic()
                try:
                    return model, self.client.models.generate_content(model=model, contents=contents, config=config)
                except e.ClientError as err:
                    if err.code == 429:
                        text = str(err.details)
                        if "PerDay" in text or "per day" in text.lower():
                            reason = "daily free quota used up"
                            break
                        rate_waits += 1
                        if rate_waits > self.max_retries:
                            raise BackendError(f"Still rate limited after {self.max_retries} waits.") from err
                        pause = self._retry_delay(err) or 10.0 * rate_waits
                        self._log(f"  ({model}: rate limited, waiting {pause:.0f} s)")
                        self._sleep(pause)
                        continue
                    if err.code == 404:
                        reason = "not available to this key"
                        break
                    if err.code in (401, 403):
                        raise BackendError("Gemini rejected the API key: check GEMINI_API_KEY.") from err
                    raise BackendError(f"Gemini error {err.code} on {model}: {err.message}") from err
                except e.ServerError as err:
                    server_errors += 1
                    if server_errors > self.retries_per_model:
                        reason = f"overloaded ({err.code})"
                        break
                    self._sleep(delay)
                    delay *= 2
                except Exception as err:  # dropped connection or timeout: retry the same model
                    if not _is_network_error(err):
                        raise
                    net_errors += 1
                    if net_errors > 4:
                        raise BackendError(f"Network problem talking to Gemini: {err}") from err
                    self._log(f"  ({model}: network error, retrying in {10 * net_errors} s)")
                    self._sleep(10.0 * net_errors)
            if self._idx + 1 >= len(self.models):
                raise BackendError(f"No Gemini model available right now (last: {model}, {reason}). "
                                   "Free models are often busy; try again later.")
            self._idx += 1
            self._log(f"  ({model} {reason}; switching to {self.model})")

    @staticmethod
    def _coerce(args: dict, schema: dict) -> dict:
        """Gemini may send whole numbers as floats (10.0); turn them back into ints."""
        props = schema.get("properties", {})
        out = dict(args)
        for k, v in out.items():
            if props.get(k, {}).get("type") == "integer" and isinstance(v, float) and v.is_integer():
                out[k] = int(v)
        return out

    def step(self, system: str, history: list[dict], tools: list[dict]) -> Turn:
        config = self._config(system, tools)
        schemas = {s["name"]: s["input_schema"] for s in tools}
        for _ in range(3):  # retry if the model emits a malformed function call
            model_used, resp = self._call(history, config)
            cand = resp.candidates[0] if resp.candidates else None
            reason = cand.finish_reason.name if cand and cand.finish_reason else "STOP"
            if reason != "MALFORMED_FUNCTION_CALL":
                break

        um = resp.usage_metadata
        usage = Usage(
            input_tokens=(um.prompt_token_count or 0) if um else 0,
            output_tokens=((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0,
            cache_read_tokens=(um.cached_content_token_count or 0) if um else 0,
        )
        if cand is None or cand.content is None:
            stop = "refusal" if cand is None or reason in self.REFUSALS else "end_turn"
            return Turn(content=None, tool_calls=[], text="", stop_reason=stop, usage=usage, model=model_used)

        calls, texts = [], []
        for part in cand.content.parts or []:
            if part.function_call:
                fc = part.function_call
                self._n += 1
                args = self._coerce(dict(fc.args or {}), schemas.get(fc.name, {}))
                calls.append(ToolCall(fc.id or f"local_{self._n}", fc.name, args))
            elif part.text and not part.thought:
                texts.append(part.text)
        if reason == "MAX_TOKENS":
            stop = "max_tokens"
        elif reason in self.REFUSALS:
            stop = "refusal"
        else:
            stop = "tool_use" if calls else "end_turn"
        return Turn(content=cand.content, tool_calls=calls, text="\n".join(texts), stop_reason=stop,
                    usage=usage, model=model_used, cost_usd=0.0)  # free tier


# --------------------------------------------------------------------------- Claude
class ClaudeBackend:
    """Claude through the Anthropic Messages API (beta surface, for refusal fallbacks). Paid."""

    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "medium",
                 max_tokens: int = 32_000, strict_tools: bool = True):
        import anthropic  # imported here so other backends need no Anthropic key
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.strict_tools = strict_tools
        self.name = f"claude:{model}"

    def _tools(self, tools: list[dict]) -> list[dict]:
        out = []
        for t in tools:
            spec = {**t, "eager_input_streaming": True}  # stream large edits; inputs validated in tools.py
            if self.strict_tools:
                spec["strict"] = True
            out.append(spec)
        return out

    @staticmethod
    def _messages(history: list[dict]) -> list[dict]:
        msgs = []
        for h in history:
            if h["kind"] == "user":
                msgs.append({"role": "user", "content": h["text"]})
            elif h["kind"] == "assistant":
                msgs.append({"role": "assistant", "content": h["turn"].content})  # unchanged
            else:
                msgs.append({"role": "user", "content": [  # all results in one message
                    {"type": "tool_result", "tool_use_id": r.id, "content": r.output, "is_error": r.is_error}
                    for r in h["results"]]})
        return msgs

    def step(self, system: str, history: list[dict], tools: list[dict]) -> Turn:
        a = self._anthropic
        params = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=self._messages(history),
            tools=self._tools(tools),
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},          # cache the growing conversation prefix
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                          # retry on another model if this one declines
        )
        last_err: Exception | None = None
        for _ in range(2):  # one retry if the stream returns tool JSON the SDK cannot parse
            try:
                with self.client.beta.messages.stream(**params) as stream:
                    msg = stream.get_final_message()
                break
            except ValueError as e:  # invalid tool-input JSON from eager streaming
                last_err = e
            except a.AuthenticationError as e:
                raise BackendError("Authentication failed: set ANTHROPIC_API_KEY in your terminal.") from e
            except a.RateLimitError as e:
                raise BackendError(f"Rate limited after SDK retries: {e.message}") from e
            except a.APIStatusError as e:
                raise BackendError(f"API error {e.status_code}: {e.message}") from e
            except a.APIConnectionError as e:
                raise BackendError(f"Network error: {e}") from e
        else:
            raise BackendError(f"Model returned unparseable tool input twice: {last_err}")

        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        served_by = getattr(msg, "model", None) or self.model
        calls = [ToolCall(b.id, b.name, b.input) for b in msg.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in msg.content if b.type == "text")
        return Turn(content=list(msg.content), tool_calls=calls, text=text, stop_reason=msg.stop_reason,
                    usage=usage, model=served_by, cost_usd=cost_usd(served_by, usage))
