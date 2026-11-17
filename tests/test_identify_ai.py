"""ai.py 的单元测试：配置、JSON 提取、外部命令调用与重试。"""
from __future__ import annotations

import json
import sys

import pytest

from galpanel2.identify import ai

FAKE_AI = r'''
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "file")
counter = os.environ["FAKE_COUNTER"]
with open(counter, "a", encoding="utf-8") as f:
    f.write("x\n")
if mode == "file":
    prompt = open(args[args.index("--task") + 1], encoding="utf-8").read()
    out = args[args.index("--out") + 1]
else:
    prompt = sys.stdin.buffer.read().decode("utf-8")
    out = None
behaviour = os.environ.get("FAKE_BEHAVIOUR", "")
n = sum(1 for _ in open(counter, encoding="utf-8"))
if behaviour == "fail" or (behaviour == "fail_first" and n == 1):
    sys.exit(3)
if behaviour == "garbage" or (behaviour == "garbage_first" and n == 1):
    reply = "我不知道"
else:
    reply = "前言 " + json.dumps({"ok": True, "mark": "【任务:提取】" in prompt,
                                  "chars": len(prompt)}, ensure_ascii=False) + " 后记"
if out:
    open(out, "w", encoding="utf-8").write(reply)
else:
    sys.stdout.write(reply)
'''


def fake_ai(tmp_path, monkeypatch, behaviour="", mode="file"):
    d = tmp_path / f"fake-{behaviour or 'ok'}-{mode}"
    d.mkdir()
    script = d / "fake_ai.py"
    script.write_text(FAKE_AI, encoding="utf-8")
    counter = d / "calls.txt"
    counter.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_BEHAVIOUR", behaviour)
    monkeypatch.setenv("FAKE_MODE", mode)
    if mode == "file":
        argv = [sys.executable, str(script), "--task", "{prompt}", "--out", "{out}",
                "--dir", "{dir}", "--timeout", "{timeout}"]
    else:
        argv = [sys.executable, str(script)]
    calls = lambda: len(counter.read_text(encoding="utf-8").splitlines())  # noqa: E731
    return ai.AiConfig(argv, mode, 20), calls


def test_unconfigured_reports_help(monkeypatch):
    for key in ('GALPANEL2_AI_BASE_URL', 'GALPANEL2_AI_MODEL', 'GALPANEL2_AI_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ai.AiError, match='GALPANEL2_AI_BASE_URL'):
        ai.load_config(None)


def test_missing_config_is_error(tmp_path):
    with pytest.raises(ai.AiError, match='配置文件不存在:'):
        ai.load_config(str(tmp_path / 'nope.json'))


def test_load_config_reads_valid_file(tmp_path):
    path = tmp_path / "ai.json"
    path.write_text(json.dumps({"argv": ["claude", "-p"], "mode": "stdin", "timeout": 99}),
                    encoding="utf-8")
    cfg = ai.load_config(str(path))
    assert (cfg.argv, cfg.mode, cfg.timeout) == (["claude", "-p"], "stdin", 99)


@pytest.mark.parametrize("bad", [
    {"argv": [], "mode": "file"},
    {"argv": ["x"], "mode": "pipe"},
    {"argv": "x"},
    {"argv": [""]},
    {"argv": ["x"], "timeout": 0},
    {"argv": ["x"], "timeout": "300"},
    [],
])
def test_load_config_rejects_bad_values(tmp_path, bad):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ai.AiError):
        ai.load_config(str(path))


def test_load_config_rejects_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ai.AiError):
        ai.load_config(str(path))


def test_parse_json_accepts_surrounding_text_and_fence():
    text = '说明\n```json\n{"a": {"b": "x}y{"}}\n```\n尾巴 {"c": 2}'
    assert ai.parse_json(text) == {"a": {"b": "x}y{"}}


def test_parse_json_handles_escaped_quotes_and_braces():
    assert ai.parse_json('结论 {"t": "含 \\"引号\\" 和 }"} 完') == {"t": '含 "引号" 和 }'}


def test_parse_json_finds_object_after_stray_brace():
    assert ai.parse_json('前言 { 不是 JSON 后 {"a": 1}') == {"a": 1}


@pytest.mark.parametrize("bad", ["没有", "[1,2]", '{"a": ', "null", '{"a": 1'])
def test_parse_json_rejects_bad_text(bad):
    with pytest.raises(ai.AiError):
        ai.parse_json(bad)


def test_parse_json_rejects_non_dict_braced_value():
    with pytest.raises(ai.AiError):
        ai.parse_json("{1, 2}")


def test_ask_file_mode_writes_log_without_prompt(tmp_path, monkeypatch):
    cfg, calls = fake_ai(tmp_path, monkeypatch)
    log = tmp_path / "ai-log.jsonl"
    result = ai.ask(cfg, "【任务:提取】 机密提示词", log_path=str(log))
    assert result["ok"] is True and result["chars"] == len("【任务:提取】 机密提示词")
    assert calls() == 1
    line = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert line["ok"] is True and line["prompt_chars"] == len("【任务:提取】 机密提示词")
    assert "机密提示词" not in log.read_text(encoding="utf-8")


def test_ask_stdin_mode(tmp_path, monkeypatch):
    cfg, calls = fake_ai(tmp_path, monkeypatch, mode="stdin")
    result = ai.ask(cfg, "【任务:判断】 中文")
    assert result["ok"] is True and result["mark"] is False
    assert calls() == 1


def test_ask_retries_after_exit_failure(tmp_path, monkeypatch):
    cfg, calls = fake_ai(tmp_path, monkeypatch, behaviour="fail_first")
    log = tmp_path / "log.jsonl"
    result = ai.ask(cfg, "prompt", log_path=str(log))
    assert result["ok"] is True and calls() == 2
    entries = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [e["ok"] for e in entries] == [False, True]


def test_ask_retries_after_garbage(tmp_path, monkeypatch):
    cfg, calls = fake_ai(tmp_path, monkeypatch, behaviour="garbage_first")
    assert ai.ask(cfg, "prompt")["ok"] is True
    assert calls() == 2


def test_ask_gives_up_after_two_failures(tmp_path, monkeypatch):
    cfg, calls = fake_ai(tmp_path, monkeypatch, behaviour="fail")
    with pytest.raises(ai.AiError) as info:
        ai.ask(cfg, "prompt")
    assert calls() == 2
    assert len(str(info.value)) <= 400


def test_ask_error_cleans_temporary_directory(tmp_path, monkeypatch):
    import glob
    import tempfile
    cfg, _ = fake_ai(tmp_path, monkeypatch, behaviour="garbage")
    before = set(glob.glob(str(__import__("pathlib").Path(tempfile.gettempdir()) / "gp2-ai-*")))
    with pytest.raises(ai.AiError):
        ai.ask(cfg, "prompt")
    after = set(glob.glob(str(__import__("pathlib").Path(tempfile.gettempdir()) / "gp2-ai-*")))
    assert after <= before


class FakeResponse:
    def __init__(self, text):
        self.text = text
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return self.text.encode('utf-8')


def http_config(monkeypatch):
    monkeypatch.setenv('GALPANEL2_AI_BASE_URL', 'https://example.test/v1')
    monkeypatch.setenv('GALPANEL2_AI_MODEL', 'test-model')
    monkeypatch.setenv('GALPANEL2_AI_API_KEY', 'secret-not-in-output')
    return ai.load_config(None)


def test_http_request_and_redacted_response(monkeypatch, tmp_path):
    cfg = http_config(monkeypatch)
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert timeout == 300
            reply = json.dumps({'answer': 'secret-not-in-output'})
            return FakeResponse(json.dumps({'choices': [{'message': {'content': reply}}]}))
    monkeypatch.setattr(ai.urllib.request, 'build_opener', lambda *a: Opener())
    log = tmp_path / 'http.jsonl'
    result = ai.ask(cfg, 'hello', log_path=str(log))
    assert result == {'answer': '[redacted]'}
    request = seen[0]
    assert request.full_url == 'https://example.test/v1/chat/completions'
    assert request.method == 'POST'
    assert request.get_header('Authorization') == 'Bearer secret-not-in-output'
    assert json.loads(request.data) == {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hello'}]}
    assert 'secret-not-in-output' not in log.read_text()


def test_http_error_cannot_leak_key(monkeypatch, tmp_path):
    cfg = http_config(monkeypatch)
    class Opener:
        def open(self, *a, **kw):
            raise OSError('secret-not-in-output')
    monkeypatch.setattr(ai.urllib.request, 'build_opener', lambda *a: Opener())
    log = tmp_path / 'errors.jsonl'
    with pytest.raises(ai.AiError) as error:
        ai.ask(cfg, 'hello', log_path=str(log))
    assert 'secret-not-in-output' not in str(error.value) + log.read_text()
    assert len(log.read_text().splitlines()) == 2


@pytest.mark.parametrize('extra', [{'api_key': 'secret'}, {'base_url': 'https://u:p@example.test'}, {'backend': 'unknown'}])
def test_http_rejects_unsafe_config(monkeypatch, tmp_path, extra):
    http_config(monkeypatch)
    path = tmp_path / 'cfg.json'
    path.write_text(json.dumps({'backend': 'openai', 'base_url': 'https://example.test', 'model': 'm', **extra}))
    with pytest.raises(ai.AiError):
        ai.load_config(str(path))


def test_http_redacts_json_escaped_secret(monkeypatch):
    cfg = http_config(monkeypatch)
    secret = 'secret"with\\escapes'
    monkeypatch.setenv('GALPANEL2_AI_API_KEY', secret)
    class Opener:
        def open(self, *a, **kw):
            reply = json.dumps({'nested': [secret], secret: 'value'})
            return FakeResponse(json.dumps({'choices': [{'message': {'content': reply}}]}))
    monkeypatch.setattr(ai.urllib.request, 'build_opener', lambda *a: Opener())
    assert ai.ask(cfg, 'prompt') == {'nested': ['[redacted]'], '[redacted]': 'value'}


@pytest.mark.parametrize('url', [
    'https://example.test/v1', 'http://localhost:8000/v1',
    'http://127.0.0.1:8000/v1', 'http://[::1]:8000/v1',
])
def test_allowed_http_schemes(monkeypatch, url):
    http_config(monkeypatch)
    monkeypatch.setenv('GALPANEL2_AI_BASE_URL', url)
    assert ai.load_config(None).base_url == url


@pytest.mark.parametrize('url', [
    'http://example.test/v1', 'http://192.168.1.2/v1',
    'http://localhost.example.test/v1', 'http://127.0.0.1.example.test/v1',
    'http://[::2]/v1', 'ftp://localhost/v1', 'https://[bad/v1',
    'https://example.test:bad/v1',
])
def test_rejected_http_schemes(monkeypatch, url):
    http_config(monkeypatch)
    monkeypatch.setenv('GALPANEL2_AI_BASE_URL', url)
    with pytest.raises(ai.AiError):
        ai.load_config(None)


@pytest.mark.parametrize('field', ['api_key', 'key', 'token'])
def test_command_rejects_secret_fields(tmp_path, field):
    path = tmp_path / 'command.json'
    path.write_text(json.dumps({'backend': 'command', 'argv': ['example'], field: 'secret'}))
    with pytest.raises(ai.AiError, match='不能存放密钥'):
        ai.load_config(str(path))
