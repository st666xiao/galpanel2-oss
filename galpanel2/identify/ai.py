"""显式配置的 OpenAI 兼容 HTTP / 外部命令适配层，失败重试一次。"""
from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import urlsplit
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class AiError(Exception):
    """AI 调用或回复解析失败。"""


@dataclass
class AiConfig:
    argv: list[str] | None = None
    mode: str = "file"  # "file" | "stdin"
    timeout: int = 300
    backend: str = "command"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "GALPANEL2_AI_API_KEY"


_ATTEMPTS = 2


def load_config(path: str | None) -> AiConfig:
    """显式配置文件优先，否则读取 OpenAI 兼容环境变量。"""
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AiError(f"配置文件不存在: {path}") from None
        except (OSError, ValueError):
            raise AiError("读取 AI 配置失败，请检查 JSON 文件") from None
    else:
        raw = {"backend": "openai", "base_url": os.getenv("GALPANEL2_AI_BASE_URL", ""),
               "model": os.getenv("GALPANEL2_AI_MODEL", "")}
    if not isinstance(raw, dict):
        raise AiError("AI 配置必须是一个 JSON 对象")
    if any(k in raw for k in ("api_key", "key", "token")):
        raise AiError("配置文件不能存放密钥，请使用 api_key_env 指定环境变量名")
    timeout = raw.get("timeout", 300)
    if type(timeout) is not int or timeout <= 0:
        raise AiError("timeout 必须是正整数")
    backend = raw.get("backend", "command" if "argv" in raw else "openai")
    if backend == "command":
        argv, mode = raw.get("argv"), raw.get("mode", "file")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise AiError("argv 必须是非空字符串列表")
        if mode not in ("file", "stdin"):
            raise AiError("mode 必须是 file 或 stdin")
        return AiConfig(list(argv), mode, timeout)
    if backend != "openai":
        raise AiError("backend 必须是 openai 或 command")
    base, model, key_env = raw.get("base_url", ""), raw.get("model", ""), raw.get("api_key_env", "GALPANEL2_AI_API_KEY")
    if not all(isinstance(v, str) and v.strip() for v in (base, model, key_env)):
        raise AiError("请设置 GALPANEL2_AI_BASE_URL、GALPANEL2_AI_MODEL、GALPANEL2_AI_API_KEY，或用 --ai-config 指定配置文件")
    try:
        parsed = urlsplit(base)
        host = parsed.hostname
        parsed.port  # 验证端口格式，不回显可能含凭据的 URL。
    except ValueError:
        raise AiError("base_url 地址格式不正确") from None
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AiError("base_url 必须是无凭据、无查询参数的 HTTP(S) 地址")
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise AiError("base_url 必须使用 HTTPS；HTTP 仅允许 localhost、127.0.0.1、::1")
    if not os.getenv(key_env):
        raise AiError("AI 密钥环境变量未设置，请按 api_key_env 配置")
    return AiConfig(timeout=timeout, backend="openai", base_url=base.rstrip("/"), model=model, api_key_env=key_env)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 不把 Authorization 转发给其他地址。


def _redact(value, key):
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


def _http_attempt(config: AiConfig, prompt: str) -> dict:
    key = os.getenv(config.api_key_env)
    if not key:
        raise AiError("AI 密钥环境变量未设置")
    body = json.dumps({"model": config.model, "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    try:
        req = urllib.request.Request(config.base_url + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=config.timeout) as resp:
            raw = resp.read().decode("utf-8")
        # 服务端回显密钥也不能进入识别产物或异常。
        reply = json.loads(raw)["choices"][0]["message"]["content"]
        return _redact(parse_json(reply), key)
    except Exception:
        raise AiError("AI HTTP 请求或回复解析失败，请检查端点、模型和凭据") from None


def parse_json(text: str) -> dict:
    """从文本中提取第一个完整的顶层 JSON 对象。

    容忍前后说明文字、```json 代码块，以及字符串里的花括号。
    """
    if not isinstance(text, str):
        raise AiError("回复不是文本")
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except ValueError as exc:
                        raise AiError(f"JSON 解析失败: {exc}") from exc
                    if not isinstance(obj, dict):
                        raise AiError("AI 回复的 JSON 不是对象")
                    return obj
        # 这个 { 没有闭合；尝试下一个（常见于说明文字里的孤立花括号）
        start = text.find("{", start + 1)
    raise AiError("回复里没有完整的 JSON 对象")


def _substitute(value: str, prompt_path: str, out_path: str, work_dir: str, timeout: int) -> str:
    return (value.replace("{prompt}", prompt_path)
                 .replace("{out}", out_path)
                 .replace("{dir}", work_dir)
                 .replace("{timeout}", str(timeout)))


def _write_log(log_path: str | None, *, seconds: float, ok: bool, error: str | None,
               prompt_chars: int) -> None:
    if not log_path:
        return
    line = json.dumps({
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seconds": round(seconds, 3),
        "ok": ok,
        "error": error,
        "prompt_chars": prompt_chars,
    }, ensure_ascii=False)
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # 日志失败不应该让识别失败
        pass


def _one_attempt(config: AiConfig, prompt: str, work_dir: str) -> dict:
    if config.backend == "openai":
        return _http_attempt(config, prompt)
    prompt_path = str(Path(work_dir) / "prompt.md")
    out_path = str(Path(work_dir) / "out.md")
    Path(prompt_path).write_text(prompt, encoding="utf-8")

    argv = [_substitute(a, prompt_path, out_path, work_dir, config.timeout) for a in config.argv]
    timeout = config.timeout + 30
    try:
        if config.mode == "stdin":
            proc = subprocess.run(argv, cwd=work_dir, shell=False, timeout=timeout,
                                  input=prompt.encode("utf-8"), capture_output=True)
        else:
            proc = subprocess.run(argv, cwd=work_dir, shell=False, timeout=timeout,
                                  capture_output=True)
    except subprocess.TimeoutExpired as exc:
        raise AiError(f"AI 命令超时（{config.timeout}s）") from exc
    except OSError as exc:
        raise AiError("AI 命令无法执行，请检查命令及权限") from None

    if proc.returncode != 0:
        raise AiError(f"AI 命令退出码 {proc.returncode}")

    if config.mode == "stdin":
        reply = proc.stdout.decode("utf-8", "replace")
    else:
        try:
            reply = Path(out_path).read_text(encoding="utf-8")
        except OSError:
            reply = ""
        if not reply.strip():
            reply = proc.stdout.decode("utf-8", "replace")
    key = os.getenv(config.api_key_env)
    return _redact(parse_json(reply), key)


def ask(config: AiConfig, prompt: str, *, log_path: str | None = None) -> dict:
    """调用外部 AI 命令，返回 JSON 对象。失败会重试一次。"""
    work_dir = tempfile.mkdtemp(prefix="gp2-ai-")
    last_error = "AI 调用失败"
    try:
        for _ in range(_ATTEMPTS):
            started = time.monotonic()
            try:
                result = _one_attempt(config, prompt, work_dir)
            except AiError as exc:
                last_error = "AI HTTP 请求失败" if config.backend == "openai" else str(exc)
                _write_log(log_path, seconds=time.monotonic() - started, ok=False,
                           error=last_error[:300], prompt_chars=len(prompt))
                continue
            except Exception as exc:  # noqa: BLE001 - 任何异常都按一次失败处理
                last_error = "AI 调用失败"
                _write_log(log_path, seconds=time.monotonic() - started, ok=False,
                           error=last_error[:300], prompt_chars=len(prompt))
                continue
            _write_log(log_path, seconds=time.monotonic() - started, ok=True,
                       error=None, prompt_chars=len(prompt))
            return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    raise AiError(last_error[:300])
