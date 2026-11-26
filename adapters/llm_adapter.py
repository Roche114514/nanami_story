"""独立 LLM JSON 适配器：复用宿主 LLM 运行时，异常时 fail-open 返回 None。"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def default_llm_adapter() -> Any:
    try:
        from application.runtime.context import try_get_app_runtime

        runtime = try_get_app_runtime()
        manager = getattr(runtime, "llm_manager", None) if runtime is not None else None
        adapter = getattr(manager, "llm_adapter", None)
        if adapter is not None:
            return adapter
    except Exception:
        pass

    try:
        from ai.llm.adapter_factory import LLMAdapterFactory
        from config.config_manager import ConfigManager

        manager = ConfigManager()
        provider, model, base_url, api_key = manager.get_llm_api_config()
        if not provider:
            return None
        return LLMAdapterFactory.create_adapter(
            **manager.merged_llm_factory_kwargs(
                provider,
                {"llm_provider": provider, "api_key": api_key, "base_url": base_url, "model": model},
            )
        )
    except Exception:
        return None


def extract_json_object(raw_text: str) -> dict[str, Any] | None:
    text = _THINK_RE.sub("", str(raw_text or "")).strip()
    text = _FENCE_RE.sub("", text).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    start = text.find("{")
    while start >= 0:
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def ask_llm_json(
    system_prompt: str,
    user_prompt: str,
    *,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 20.0,
    max_tokens: int = 2400,
    temperature: float = 0.45,
) -> dict[str, Any] | None:
    adapter = (adapter_provider or default_llm_adapter)()
    if adapter is None:
        return None
    box: dict[str, Any] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            try:
                resp = adapter.chat(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=False,
                    response_format={"type": "json_object"},
                )
            except TypeError:
                resp = adapter.chat(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=False,
                )
            decoded = adapter.decode_response(resp) if hasattr(adapter, "decode_response") else resp
            raw_content = getattr(decoded, "content", None)
            if raw_content is None and hasattr(resp, "choices") and getattr(resp, "choices", None):
                msg = getattr(resp.choices[0], "message", None)
                raw_content = getattr(msg, "content", None)
            raw_text = str(raw_content if raw_content is not None else decoded or "")
            box["result"] = extract_json_object(raw_text)
        except Exception as exc:
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_worker, name="nanami-story-llm", daemon=True)
    thread.start()
    if not done.wait(timeout_sec):
        logger.warning("nanami_story: LLM 调用超时（%.1fs），切换兜底推演", timeout_sec)
        return None
    if "error" in box:
        logger.warning("nanami_story: LLM 调用失败（%s），切换兜底推演", box["error"])
        return None
    res = box.get("result")
    return res if isinstance(res, dict) else None
