"""Realize Brain dialogue drafts in each speaker's voice before media dispatch."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.chat_history.identity import DialogIdentities
from sdk.handlers import MessageHandler
from sdk.messages import LLMDialogMessage

from .adapters.llm_adapter import ask_llm_json
from .dialogue_injector import _CONTROL_NAMES, _assistant_payload, _dialog_rows, _scene_hint

if TYPE_CHECKING:
    from .dialogue_injector import DialogueInjector

logger = logging.getLogger(__name__)

_PUNCTUATION_RE = re.compile(r"^[….。·，,、？！?!：:；;\s]+$")
_STAGE_RE = re.compile(r"（[^（）]*）|\([^()]*\)|【[^【】]*】|\[[^\[\]]*\]")
_QUOTES = " \t\r\n\"'“”‘’「」『』"
_INTENT_PREFIX_RE = re.compile(
    r"^(?:(?:语义意图|决策大脑|台词)\s*[：:]|"
    r"(?:她|他)?(?:应一声[，,]\s*(?:说|道)|顺着[她他你对方]{1,2}的话(?:说|道)?|"
    r"(?:轻声|低声|小声|平静地|点头(?:后)?)\s*(?:说|道))\s*[：:，,]?)\s*"
)
_META = (
    "不太想说", "不想聊这个", "换个话题", "作为ai", "作为人工智能", "语言模型", "我倒没想到",
    "顺便一提", "系统设定", "我理解你的感受", "有什么可以帮", "根据你的描述",
)
_INTENT_LEAKS = ("应一声，说", "顺着她的话", "顺着他的话", "决策大脑", "语义意图")
_ECHO_SPLIT_RE = re.compile(r"^[^。！？\n]{1,8}[？?](?:\s*/\s*|\s+)[^。！？\n]{3,}")
_ECHO_OPEN_RE = re.compile(r"^[\u3400-\u9fff]{2,6}?(?:[啊呀嘛吧呢哦么])?(?=…|\.{2,}|[，,。？?！!])")
_CLAUSE_RE = re.compile(r"[，,。！？!?；;\n]+")
_WORD_RE = re.compile(r"\W+", re.UNICODE)


def _sanitize(text: str) -> str:
    line = text.strip(_QUOTES)
    previous = None
    while previous != line:
        previous = line
        line = _STAGE_RE.sub("", line)
    return _INTENT_PREFIX_RE.sub("", line.strip(_QUOTES)).strip(_QUOTES)


def _ngrams(text: str, length: int) -> set[str]:
    return {text[i:i + length] for i in range(len(text) - length + 1)}


def _overlap(left: str, right: str, length: int) -> float:
    a, b = _ngrams(left, length), _ngrams(right, length)
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _hits_off_voice(line: str, word: str) -> bool:
    if word.casefold() in line.casefold():
        return True
    # The persona toolkit also ships anchored regular expressions for individual clauses.
    if not any(ch in word for ch in "^$\\[](|"):
        return False
    try:
        return any(re.search(word, clause.strip(), re.I) for clause in _CLAUSE_RE.split(line))
    except re.error:
        return False


def score_mouth_candidate(
    text: str, *, recent_replies: Sequence[str] = (), off_voice_words: Sequence[str] = (),
    heard_line: str = "",
) -> float:
    """Rank the spoken wording and penalize leaks in the unsanitized candidate."""
    line = _sanitize(text)
    if not line or _PUNCTUATION_RE.fullmatch(line):
        return -20.0
    score = 2.0 if 3 <= len(line) <= 34 else 0.0
    score -= max(0, len(line) - 50) * 0.2
    raw = text.casefold()
    score -= 9.0 * sum(word in raw for word in _META)
    score -= 9.0 * sum(word in raw for word in _INTENT_LEAKS)
    if _INTENT_PREFIX_RE.match(text.strip(_QUOTES)) or _STAGE_RE.search(text):
        score -= 7.0
    if _ECHO_SPLIT_RE.match(line):
        score -= 7.0
    score -= 10.0 * sum(_hits_off_voice(line, word) for word in off_voice_words if word)

    compact = _WORD_RE.sub("", line.casefold())
    for recent in recent_replies:
        prior = _WORD_RE.sub("", recent.casefold())
        if compact and compact == prior:
            score -= 12.0
        elif len(compact) >= 2 and compact[:2] == prior[:2]:
            score -= 3.5
        score -= 3.0 * _overlap(compact, prior, 3) + 3.0 * _overlap(compact, prior, 4)

    heard = _WORD_RE.sub("", heard_line.casefold())
    opening = _ECHO_OPEN_RE.match(line)
    if opening:
        prefix = opening.group()
        if prefix in heard or (len(prefix) > 2 and prefix[-1] in "啊呀嘛吧呢哦么" and prefix[:-1] in heard):
            score -= 9.0
    overlap = _overlap(compact, heard, 3)
    if overlap >= 0.4:
        score -= 6.0 * overlap
    return score


def _run_guard_bridge(guard_mjs: Path | None, toolkit_dir: Path | None, texts: list[str]) -> list[str]:
    if guard_mjs is None or toolkit_dir is None:
        return texts
    try:
        result = subprocess.run(
            ["node", str(guard_mjs), str(toolkit_dir)],
            input=json.dumps({"texts": texts}, ensure_ascii=False),
            capture_output=True, encoding="utf-8", timeout=2.5, check=True,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        payload = json.loads(result.stdout)
        cleaned = payload.get("texts") if isinstance(payload, Mapping) and payload.get("ok") else None
        if isinstance(cleaned, list) and len(cleaned) == len(texts) and all(isinstance(t, str) for t in cleaned):
            return cleaned
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.debug("nanami_story: 台词守卫不可用", exc_info=True)
    return texts


_MOUTH_SYSTEM = (
    "你是当前角色的台词配音演员。把大脑给出的语义草稿说成角色会脱口而出的话。"
    "保留剧情意图、说话对象、关键事实和称呼，按角色声线、年龄感和节奏重新措辞。"
    "直接接话，避免复述上一角色的关键词起头，不重复自己的开头和句式。"
    "语料只用于模仿说法，不照搬事件、人名或台词。只写角色实际说出的话，"
    '去掉舞台动作、意图解释和 AI 客套。返回 JSON：{"candidates":["说法1","说法2","说法3","说法4","说法5","说法6"]}。'
)


class DialogueMouthRewriter:
    """Share the realized lines between the history hook and the media worker."""

    score_mouth_candidate = staticmethod(score_mouth_candidate)

    def __init__(self, persona_adapter, settings_provider, *, llm_fn=None, guard_fn=None) -> None:
        self._persona_adapter = persona_adapter
        self._settings_provider = settings_provider
        self._llm_fn = llm_fn
        self._guard_fn = guard_fn
        self._lock = threading.RLock()
        self._rewrite_lock = threading.Lock()
        self._recent_by_speaker: dict[str, dict[str, deque[str]]] = {}
        self._last_spoken: dict[str, tuple[str, str]] = {}
        self._rewrite_records: dict[str, deque[tuple[str, str, str]]] = {}
        self._by_dialog_id: dict[str, dict[str, str]] = {}
        self._anonymous_records: dict[str, deque[tuple[str, str, str]]] = {}

    def rewrite_utterance(
        self, history_key: str, character_name: str, draft_text: str, *, scene_hint: str = "",
        listener_hint: str = "", enabled: bool = True, dialog_id: str = "",
    ) -> str:
        draft = draft_text.strip()
        name = character_name.strip()
        speaker = name.casefold()
        if not draft or not speaker or speaker in _CONTROL_NAMES or not enabled or _PUNCTUATION_RE.fullmatch(draft):
            return draft
        # The media worker can be speaking while the LLM worker persists its response.
        # Serialize realization so the same durable line is generated once, in scene order.
        with self._rewrite_lock:
            with self._lock:
                cached = self._by_dialog_id.get(history_key, {}).get(dialog_id)
                if cached is not None:
                    return cached
                recent = tuple(self._recent_by_speaker.get(history_key, {}).get(speaker, ()))[-5:]
                last_speaker, last_line = self._last_spoken.get(history_key, ("", ""))
            heard = last_line if last_speaker.casefold() != speaker else ""
            final = self._realize(name, draft, scene_hint, listener_hint, recent, heard)
            with self._lock:
                speakers = self._recent_by_speaker.setdefault(history_key, {})
                speakers.setdefault(speaker, deque(maxlen=6)).append(final)
                self._last_spoken[history_key] = (name, final)
                records = self._rewrite_records.setdefault(history_key, deque(maxlen=60))
                records.append((name, draft, final))
                if dialog_id:
                    identities = self._by_dialog_id.setdefault(history_key, {})
                    identities[dialog_id] = final
                    if len(identities) > 256:
                        identities.pop(next(iter(identities)))
                else:
                    self._anonymous_records.setdefault(history_key, deque(maxlen=60)).append((name, draft, final))
            return final

    def _realize(
        self, name: str, draft: str, scene: str, listener: str, recent: Sequence[str], heard: str,
    ) -> str:
        chosen = _sanitize(draft) or draft
        try:
            settings = self._settings_provider()
            ctx = self._persona_adapter.read_mouth_context(
                name, custom_persona_dir=getattr(settings, "persona_data_dir", ""),
            )
        except Exception:
            logger.debug("nanami_story: 读取角色声线失败", exc_info=True)
            ctx = {}

        off_voice = ctx.get("off_voice_words") or ()
        prompt: dict[str, Any] = {"说话角色": name, "大脑语义草稿（保留意图、对象和事实）": draft}
        for label, value in (
            ("角色声线", ctx.get("soul_text")), ("语料（只参考说话风格）", ctx.get("corpus_pairs")),
            ("避用词句", off_voice[:32]), ("此刻状态", ctx.get("mind_hint")),
            ("自己近几句（避开重复开头和措辞）", [line[:120] for line in recent]),
            ("刚听到的台词（不要复述其关键词开头）", heard[:200]),
            ("眼前场景", scene[:280]), ("说话对象", listener[:64]),
        ):
            if value:
                prompt[label] = value
        try:
            result = (self._llm_fn or ask_llm_json)(
                _MOUTH_SYSTEM, json.dumps(prompt, ensure_ascii=False),
                timeout_sec=7.0, max_tokens=360, temperature=0.78,
            )
            candidates = result.get("candidates") if isinstance(result, Mapping) else None
            if isinstance(candidates, list):
                usable = [(raw, clean) for raw in candidates[:6]
                          if isinstance(raw, str) and (clean := _sanitize(raw)) and not _PUNCTUATION_RE.fullmatch(clean)]
                if usable:
                    _, chosen = max(usable, key=lambda pair: score_mouth_candidate(
                        pair[0], recent_replies=recent, off_voice_words=off_voice, heard_line=heard,
                    ))
        except Exception:
            logger.debug("nanami_story: 台词生成失败，沿用草稿", exc_info=True)
        try:
            cleaned = (self._guard_fn or _run_guard_bridge)(ctx.get("guard_mjs"), ctx.get("toolkit_dir"), [chosen])
            if isinstance(cleaned, list) and cleaned and isinstance(cleaned[0], str):
                return _sanitize(cleaned[0]) or draft
        except Exception:
            logger.debug("nanami_story: 台词守卫失败，沿用当前台词", exc_info=True)
        return chosen

    def complete_turn(
        self, history_key: str, message: Mapping[str, Any], turn_id: str, characters: Sequence[str],
        *, scene_hint: str = "",
    ) -> None:
        """Realize lines still queued for media when the host persists a turn."""
        settings = self._settings_provider()
        if not turn_id or not (settings.enabled and settings.use_llm_compiler and settings.dialogue_mouth_decoupling):
            return
        cast = {name.casefold(): name for name in characters}
        for row, dialog_id in _identified_rows(_assistant_payload(message.get("content")), turn_id):
            name, _, speech = _line_fields(row)
            if name.casefold() in cast and speech:
                self.rewrite_utterance(history_key, cast[name.casefold()], speech, scene_hint=scene_hint,
                                       dialog_id=dialog_id)

    def _replacements(self, history_key: str):
        with self._lock:
            records, anonymous = (
                {(name.casefold(), raw): final for name, raw, final in source.get(history_key, ())}
                for source in (self._rewrite_records, self._anonymous_records)
            )
            return records, dict(self._by_dialog_id.get(history_key, {})), anonymous

    def apply_rewrites_to_messages(self, history_key: str, messages: list[dict[str, Any]]) -> None:
        records, identities, anonymous = self._replacements(history_key)
        if not records:
            return
        assistants = 0
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "assistant" or message.get("tool_calls"):
                continue
            assistants += 1
            turn_id = _latest_turn_id(messages, index)
            self._patch_message(message, turn_id, anonymous if turn_id else records, identities)
            if assistants >= 8:
                break

    @staticmethod
    def _patch_message(message, turn_id, records, identities) -> None:
        content = message.get("content")
        payload = _assistant_payload(content)
        changed = False
        for row, dialog_id in _identified_rows(payload, turn_id):
            name, field, speech = _line_fields(row)
            final = identities.get(dialog_id) or records.get((name.casefold(), speech))
            if final and final != row.get(field):
                row[field] = final
                changed = True
        if changed and isinstance(content, str):
            message["content"] = json.dumps(payload, ensure_ascii=False)

    def apply_rewrites_to_utterances(
        self, history_key: str, utterances: list[dict[str, str]], *,
        message: Mapping[str, Any] | None = None, turn_id: str = "",
    ) -> list[dict[str, str]]:
        records, identities, anonymous = self._replacements(history_key)
        by_line: dict[tuple[str, str], deque[str]] = {}
        if message is not None and turn_id:
            for row, dialog_id in _identified_rows(_assistant_payload(message.get("content")), turn_id):
                name, _, speech = _line_fields(row)
                key = (name.casefold(), speech)
                by_line.setdefault(key, deque()).append(identities.get(dialog_id) or anonymous.get(key, speech))
        result = []
        for row in utterances:
            key = (row["character_name"].strip().casefold(), row["speech"].strip())
            finals = by_line.get(key)
            if message is not None and turn_id:
                final = finals.popleft() if finals else row["speech"]
            else:
                final = records.get(key, row["speech"])
            result.append({**row, "speech": final})
        return result


def _line_fields(row: Mapping[str, Any]) -> tuple[str, str, str]:
    name = row.get("character_name", row.get("name"))
    field = "speech" if "speech" in row else "text"
    speech = row.get(field)
    return (name.strip() if isinstance(name, str) else "", field,
            speech.strip() if isinstance(speech, str) else "")


def _identified_rows(payload: Any, turn_id: str):
    identities = DialogIdentities(turn_id)
    for row in _dialog_rows(payload):
        if isinstance(row, dict):
            yield row, identities.next(row)


def _latest_turn_id(messages: Sequence[Mapping[str, Any]], before: int) -> str:
    return next((str(row.get("_chatTurnId") or "") for row in reversed(messages[:before])
                 if row.get("role") == "user"), "")


class StoryMouthDialogHandler(MessageHandler):
    """Rewrite live lines from the bound cast and hand them to persona and media handlers."""

    def __init__(self, *, injector: DialogueInjector, rewriter: DialogueMouthRewriter, settings_provider) -> None:
        self._injector = injector
        self._rewriter = rewriter
        self._settings_provider = settings_provider
        self._lock = threading.RLock()
        self._seen_keys: dict[tuple[int, str, int | None], LLMDialogMessage] = {}
        self._downstream: list[MessageHandler] | None = None
        self._downstream_failed = False

    @staticmethod
    def _message_key(msg: LLMDialogMessage) -> tuple[int, str, int | None]:
        return id(msg), str(getattr(msg, "dialog_id", "") or ""), getattr(msg, "turn_id", None)

    def _can_rewrite(self, msg: LLMDialogMessage) -> bool:
        name = str(getattr(msg, "name", "") or "").strip().casefold()
        if (not name or name in _CONTROL_NAMES or getattr(msg, "_presentation_replay", False)
                or not str(getattr(msg, "text", "") or "").strip()):
            return False
        settings = self._settings_provider()
        return settings.enabled and settings.use_llm_compiler and settings.dialogue_mouth_decoupling

    def can_handle(self, msg: LLMDialogMessage) -> bool:
        with self._lock:
            if self._downstream_failed or self._message_key(msg) in self._seen_keys:
                return False
        if not self._can_rewrite(msg) or self._injector.is_native_story():
            return False
        session, _, cast = self._injector.active_dialogue_session()
        name = msg.name.strip().casefold()
        if not session or session.get("paused") or not any(name == item.strip().casefold() for item in cast):
            return False
        # A failed rebuild must decline the message so the host can continue its own chain.
        return bool(self._downstream_chain())

    def pre_process(self, msg: LLMDialogMessage) -> None:
        with self._lock:
            key = self._message_key(msg)
            if key in self._seen_keys:
                return
            # Keep a reference so a new message cannot reuse a remembered object id.
            self._seen_keys[key] = msg
            if len(self._seen_keys) > 256:
                self._seen_keys.pop(next(iter(self._seen_keys)))
        session, history_key, cast = self._injector.active_dialogue_session()
        if not session or session.get("paused") or self._injector.is_native_story() or not self._can_rewrite(msg):
            return
        name = next((item.strip() for item in cast if item.strip().casefold() == msg.name.strip().casefold()), "")
        if name:
            msg.text = self._rewriter.rewrite_utterance(
                history_key, name, msg.text, scene_hint=_scene_hint(session),
                enabled=True, dialog_id=str(getattr(msg, "dialog_id", "") or ""),
            )

    def handle(self, msg: LLMDialogMessage) -> None:
        for handler in self._downstream_chain():
            if handler.can_handle(msg):
                handler.pre_process(msg)
                handler.handle(msg)
                handler.post_process(msg)
                return
        logger.warning("nanami_story: 没有下游处理器接住 %r 的台词", msg.name)

    def _downstream_chain(self) -> list[MessageHandler]:
        with self._lock:
            if self._downstream is None:
                try:
                    self._downstream = self._build_downstream()
                except Exception:
                    self._downstream_failed = True
                    self._downstream = []
                    logger.warning("nanami_story: 无法构建台词下游处理器", exc_info=True)
                for handler in self._downstream:
                    try:
                        handler.init()
                    except Exception:
                        logger.debug("nanami_story: 下游处理器初始化失败", exc_info=True)
            return self._downstream

    def _build_downstream(self) -> list[MessageHandler]:
        from application.chat.presentation.handlers.dialog_media import get_dialog_media_handlers

        try:
            from plugin_system.host import get_plugin_dialog_media_handlers

            others = [handler for handler in get_plugin_dialog_media_handlers() if handler is not self]
        except Exception:
            logger.debug("nanami_story: 无法读取插件对话处理器", exc_info=True)
            others = []

        strategy = None
        try:
            from application.runtime.context import try_get_app_runtime

            manager = getattr(try_get_app_runtime(), "llm_manager", None)
            if getattr(manager, "media_selection_mode", None) == "semantic":
                from application.chat.dialog_media import create_asset_lookup_strategy

                strategy = create_asset_lookup_strategy("semantic")
        except Exception:
            logger.debug("nanami_story: 无法读取语义媒体配置", exc_info=True)
        return others + list(get_dialog_media_handlers(asset_lookup_strategy=strategy))
