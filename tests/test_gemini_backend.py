"""Gemini backend tests with a fake client: no key needed, no network."""
import pytest
from google.genai import errors, types

from bugfix_agent.backends import BackendError, GeminiBackend, ToolOutput, Turn
from bugfix_agent.tools import TOOL_SPECS


def response(parts, finish=types.FinishReason.STOP, prompt=100, out=20, thoughts=5):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts))


def rate_limit(per_day=False):
    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else "GenerateRequestsPerMinute"
    return errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED",
                                              "details": [{"quotaId": quota}, {"retryDelay": "7s"}]}})


def overloaded():
    return errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})


def retired():
    return errors.ClientError(404, {"error": {"code": 404, "message": "no longer available", "status": "NOT_FOUND"}})


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []    # contents sent, per call
        self.models = []   # model name used, per call

    def generate_content(self, model, contents, config):
        self.calls.append(contents)
        self.models.append(model)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    sleeps = []

    def _make(outcomes, **kw):
        b = GeminiBackend(sleep=sleeps.append, log=lambda *a: None, **kw)
        b.client = type("C", (), {"models": FakeModels(outcomes)})()
        return b, sleeps
    return _make


def test_parses_tool_calls_text_and_usage(make):
    parts = [types.Part(text="thinking...", thought=True), types.Part(text="Reading the file."),
             types.Part(function_call=types.FunctionCall(id="c1", name="read_file",
                                                         args={"path": "a.py", "start_line": 3.0}))]
    b, _ = make([response(parts)])
    t = b.step("sys", [{"kind": "user", "text": "fix"}], TOOL_SPECS)
    assert t.stop_reason == "tool_use" and t.text == "Reading the file."      # thought text not shown
    assert t.tool_calls[0].input == {"path": "a.py", "start_line": 3}           # 3.0 -> 3
    assert isinstance(t.tool_calls[0].input["start_line"], int)
    assert t.usage.input_tokens == 100 and t.usage.output_tokens == 25 and t.cost_usd == 0.0


def test_missing_call_id_gets_local_id_and_is_not_sent_back(make):
    parts = [types.Part(function_call=types.FunctionCall(name="list_files", args={"path": "."}))]
    b, _ = make([response(parts), response([types.Part(text="done")])])
    t1 = b.step("sys", [{"kind": "user", "text": "fix"}], TOOL_SPECS)
    assert t1.tool_calls[0].id.startswith("local_")
    history = [{"kind": "user", "text": "fix"}, {"kind": "assistant", "turn": t1},
               {"kind": "tool_results", "results": [ToolOutput(t1.tool_calls[0].id, "list_files", "a.py", False)]}]
    t2 = b.step("sys", history, TOOL_SPECS)
    sent = b.client.models.calls[1]
    assert [c.role for c in sent] == ["user", "model", "user"]
    fr = sent[2].parts[0].function_response
    assert fr.id is None and fr.name == "list_files" and fr.response == {"output": "a.py"}
    assert sent[1] is t1.content                                             # model turn passed back unchanged
    assert t2.stop_reason == "end_turn" and t2.text == "done"


def test_error_results_use_error_key(make):
    b, _ = make([response([types.Part(text="ok")])])
    hist = [{"kind": "user", "text": "x"},
            {"kind": "tool_results", "results": [ToolOutput("c9", "read_file", "not a file", True)]}]
    b.step("sys", hist, TOOL_SPECS)
    fr = b.client.models.calls[0][1].parts[0].function_response
    assert fr.response == {"error": "not a file"} and fr.id == "c9"


def test_empty_model_turn_is_skipped(make):
    b, _ = make([response([types.Part(text="ok")])])
    empty = Turn(content=types.Content(role="model", parts=[]), tool_calls=[], text="", stop_reason="end_turn")
    b.step("sys", [{"kind": "user", "text": "x"}, {"kind": "assistant", "turn": empty},
                   {"kind": "user", "text": "nudge"}], TOOL_SPECS)
    assert [c.role for c in b.client.models.calls[0]] == ["user", "user"]


def test_rate_limit_waits_then_succeeds(make):
    b, sleeps = make([rate_limit(), response([types.Part(text="ok")])])
    t = b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS)
    assert t.text == "ok" and sleeps == [7.0]                                # used Google's retryDelay


def test_overloaded_model_switches_to_next(make):
    b, sleeps = make([overloaded(), overloaded(), response([types.Part(text="ok")])], models=["m1", "m2"])
    t = b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS)
    assert b.client.models.models == ["m1", "m1", "m2"]   # one retry on m1, then switch
    assert sleeps == [5.0] and t.model == "m2" and b.model == "m2"


def test_switch_sticks_for_later_steps(make):
    b, _ = make([retired(), response([types.Part(text="a")]), response([types.Part(text="b")])],
                models=["m1", "m2"])
    b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS)
    b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS)
    assert b.client.models.models == ["m1", "m2", "m2"]   # 404: switch at once, no retry


def test_daily_quota_switches_model(make):
    b, sleeps = make([rate_limit(per_day=True), response([types.Part(text="ok")])], models=["m1", "m2"])
    assert b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS).model == "m2" and sleeps == []


def test_all_models_unavailable_stops_run(make):
    b, _ = make([overloaded(), overloaded(), rate_limit(per_day=True)], models=["m1", "m2"])
    with pytest.raises(BackendError, match="No Gemini model available"):
        b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS)


def test_turn_from_other_model_gets_skip_signature(make):
    b, _ = make([response([types.Part(text="ok")])], models=["m2"])
    old = types.Content(role="model", parts=[
        types.Part(text="secret reasoning", thought=True),
        types.Part(text="Reading it."),
        types.Part(function_call=types.FunctionCall(id="c1", name="read_file", args={"path": "a.py"}),
                   thought_signature=b"m1-signature")])
    m1_turn = Turn(content=old, tool_calls=[], text="Reading it.", stop_reason="tool_use", model="m1")
    b.step("sys", [{"kind": "user", "text": "x"}, {"kind": "assistant", "turn": m1_turn},
                   {"kind": "tool_results", "results": [ToolOutput("c1", "read_file", "...", False)]}], TOOL_SPECS)
    sent = b.client.models.calls[0][1]
    assert [p.text for p in sent.parts if p.text] == ["Reading it."]              # hidden reasoning dropped
    fc_part = next(p for p in sent.parts if p.function_call)
    assert fc_part.thought_signature == GeminiBackend.SKIP_SIGNATURE
    assert old.parts[2].thought_signature == b"m1-signature"                       # original not modified


def test_model_string_or_default_list(make):
    assert make([], models="only-one")[0].models == ["only-one"]
    assert make([])[0].models == GeminiBackend.DEFAULT_MODELS


def test_bad_key_message(make):
    b, _ = make([errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}})])
    with pytest.raises(BackendError, match="GEMINI_API_KEY"):
        b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS)


def test_malformed_call_is_retried(make):
    bad = response([types.Part(text="")], finish=types.FinishReason.MALFORMED_FUNCTION_CALL)
    good = response([types.Part(function_call=types.FunctionCall(id="c2", name="list_files", args={"path": "."}))])
    b, _ = make([bad, good])
    t = b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS)
    assert t.stop_reason == "tool_use" and len(b.client.models.calls) == 2


def test_safety_block_is_refusal(make):
    b, _ = make([response([types.Part(text="")], finish=types.FinishReason.SAFETY)])
    assert b.step("sys", [{"kind": "user", "text": "x"}], TOOL_SPECS).stop_reason == "refusal"


def test_effort_max_maps_to_xhigh(make):
    b, _ = make([], effort="max")
    assert b._config("sys", TOOL_SPECS).thinking_config.thinking_level.name == "XHIGH"


def test_network_error_is_retried():
    import httpx2  # the transport google-genai uses here
    from bugfix_agent.backends import _is_network_error
    assert _is_network_error(httpx2.ReadError("Connection reset by peer"))
    assert _is_network_error(ConnectionResetError())
    assert not _is_network_error(ValueError("x"))
