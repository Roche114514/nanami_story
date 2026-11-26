"""Main-chat hooks for dialogue stories, scoped to the active history binding."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .character_chronicle import active_dialogue_beat, active_director_whispers
from .fate_schema import clip_text
from .settings import canonical_history_path, dialogue_character_names, history_has_native_story

logger = logging.getLogger(__name__)

SCRIPT_HEADER = "【命运剧本】"
NATIVE_SCRIPT_HEADER = "【当前剧本进度】"
_SUMMARY_HEADERS = ("【历史摘要】", "【历史对话总结】")
_EMPTY_OBSERVER_DISPLAY = ("",)
_CONTROL_NAMES = {"cot", "stat", "scene", "bgm", "narr", "narration", "narrator", "旁白",
                  "choice", "option", "system", "effect", "cg", "思维链", "选项", "数值", "场景"}
_OBSERVER_CUE_BODY_RE = re.compile(
    r"(?P<metadata>\s*(?:(?:\[本地时间[ \t]+[^\]\r\n]*\]|"
    r"\[当前显示背景\][\s\S]*?\[/当前显示背景\])\s*)*)"
    r"(?P<cue>[（(]剧情继续[）)]|[（(]剧目开场[^）)\r\n]*[）)]?)?\s*"
)
_INTENT_DIRECTIONS = {
    "probe": "试探", "confide": "试着倾诉", "conceal": "暂时保留", "confront": "当面问清",
    "deflect": "缓一缓话题", "propose_shift": "提出下一步", "silent_observe": "先安静观察",
}


def _host_history_file() -> str:
    try:
        from application.runtime.context import try_get_app_runtime

        manager = getattr(try_get_app_runtime(), "llm_manager", None)
        return str(getattr(manager, "_history_file", "") or "")
    except ImportError:
        return ""


def _assistant_payload(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    source = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip(), flags=re.I)
    try:
        return json.loads(source)
    except ValueError:
        return None


def _dialog_rows(payload: Any) -> list[Any]:
    if isinstance(payload, Mapping):
        if "dialog" in payload:
            dialog = payload["dialog"]
            return dialog if isinstance(dialog, list) else []
        if "character_name" in payload or "name" in payload:
            return [payload]
    return payload if isinstance(payload, list) else []


def _assistant_utterances(message: Mapping[str, Any]) -> list[dict[str, str]]:
    if message.get("role") != "assistant" or message.get("tool_calls"):
        return []
    content = message.get("content")
    rows = _dialog_rows(_assistant_payload(content))
    if not rows and isinstance(content, str):
        try:
            from core.chat_history.text import parse_assistant_dialog_content

            rows = parse_assistant_dialog_content(content)
        except (ImportError, ValueError, TypeError):
            pass
    result = []
    for row in rows[:48]:
        if not isinstance(row, Mapping):
            continue
        name = row.get("character_name", row.get("name"))
        speech = row.get("speech", row.get("text"))
        if (isinstance(name, str) and name.strip() and name.strip().casefold() not in _CONTROL_NAMES
                and isinstance(speech, str) and speech.strip()):
            result.append({"character_name": name.strip(), "speech": speech.strip()[:4000]})
    return result


def _text_parts(content: Any) -> list[tuple[int, str]]:
    if not isinstance(content, list):
        return []
    parts = []
    for index, part in enumerate(content):
        value = part.get("text") if isinstance(part, Mapping) else part
        if isinstance(value, str):
            parts.append((index, value))
    return parts


def _user_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(value for _, value in _text_parts(content))
    return str(content or "")


def _user_content(row: Mapping[str, Any]) -> Any:
    display = row.get("display_content")
    if display == _EMPTY_OBSERVER_DISPLAY:
        return ""
    return display or row.get("content") or ""


def _is_summary(row: Mapping[str, Any]) -> bool:
    return _user_text(_user_content(row)).lstrip().startswith(_SUMMARY_HEADERS)


def _user_identity(row: Mapping[str, Any]) -> bytes:
    return json.dumps(
        [row.get("_chatTurnId") or "", _user_content(row)], ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")


def _user_prefixes(messages: Sequence[Mapping[str, Any]], *, completed_only: bool) -> list[str]:
    digest = hashlib.sha256()
    prefixes = [digest.hexdigest()]
    completed = 1
    for row in messages:
        if row.get("role") == "user" and not _is_summary(row):
            digest.update(_user_identity(row) + b"\n")
            prefixes.append(digest.hexdigest())
        elif completed_only and row.get("role") == "assistant" and not row.get("tool_calls"):
            if row.get("content"):
                completed = len(prefixes)
    return prefixes[:completed] if completed_only else prefixes


def _prefix_needs_sync(session: Mapping[str, Any], prefixes: Sequence[str]) -> bool:
    if not prefixes:
        return False
    cursor = session.get("dialogue_history_cursor")
    if cursor is None:
        return True
    count = cursor["user_turn_count"]
    if count >= len(prefixes) or prefixes[count] != cursor["user_history_fingerprint"]:
        return True
    return any(
        count < row["user_turn_count"] < len(prefixes)
        and prefixes[row["user_turn_count"]] == row["user_history_fingerprint"]
        for row in session.get("turn_checkpoints", ())
    )


def _uncompacted_history(messages: list[dict[str, Any]], history_file: str) -> list[dict[str, Any]] | None:
    if not any(_is_summary(row) for row in messages if row.get("role") == "user"):
        return messages
    # The host journals original messages before trimming the prompt. Trust
    # the journal only when it ends with the exact in-memory user suffix.
    try:
        from core.chat_history.storage import chat_history_active_path

        active = chat_history_active_path(history_file)
        journal = Path(f"{active}.tmp")
        saved = json.loads(active.read_text(encoding="utf-8")) if active.is_file() else []
        if not isinstance(saved, list):
            return None
        if journal.is_file():
            saved += [json.loads(line.rstrip(", \r\n")) for line in journal.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
    except (ImportError, OSError, ValueError):
        return None
    if any(not isinstance(row, Mapping) or (row.get("role") == "user" and _is_summary(row)) for row in saved):
        return None
    suffix = [_user_identity(row) for row in messages if row.get("role") == "user" and not _is_summary(row)]
    stored = [_user_identity(row) for row in saved if row.get("role") == "user"]
    return saved if suffix and stored[-len(suffix):] == suffix else None


def _stage_excerpt(value: Any, limit: int) -> str:
    return clip_text(" ".join(str(value or "").split()), limit)


def _scene_hint(session: Mapping[str, Any]) -> str:
    _, node = active_dialogue_beat(session)
    if not node:
        return ""
    return " · ".join(filter(None, (_stage_excerpt(node.get("scene_name"), 48),
                                   _stage_excerpt(node.get("situation"), 180))))


def _insert_stimulus(content: Any, text: str, offset: int, stimulus: str, missing_cue: str) -> Any:
    separator = "\n" if offset and text[offset - 1] not in "\n\r" else ""
    insertion = separator + stimulus + "\n" + missing_cue
    if not isinstance(content, list):
        return text[:offset] + insertion + text[offset:]
    parts = list(content)
    remaining = offset
    for index, value in _text_parts(content):
        if remaining <= len(value):
            updated = value[:remaining] + insertion + value[remaining:]
            parts[index] = {**parts[index], "text": updated} if isinstance(parts[index], Mapping) else updated
            return parts
        remaining -= len(value) + 1
    parts.append({"type": "text", "text": insertion})
    return parts


def _enrich_observer_cue(messages: list[dict[str, Any]], session: Mapping[str, Any]) -> None:
    """Add a stage cue only to the model's user message, preserving its history identity."""
    for index in range(len(messages) - 1, -1, -1):
        row = messages[index]
        if row.get("role") == "user" and not _is_summary(row):
            break
    else:
        return
    content = row.get("content")
    text = _user_text(content)
    match = _OBSERVER_CUE_BODY_RE.fullmatch(text)
    if match is None:
        return
    previous = next((item for item in reversed(messages[:index]) if item.get("role") == "assistant"), {})
    cast = {name.casefold() for name in session.get("dialogue_cast", ())}
    lines = [item for item in _assistant_utterances(previous) if item["character_name"].casefold() in cast]
    if lines:
        stimulus = "[舞台上一拍]" + " / ".join(
            f"{item['character_name']}：{_stage_excerpt(item['speech'], 64)}" for item in lines[-3:]
        )
    else:
        scene = _scene_hint(session)
        if not scene:
            return
        stimulus = "[舞台此刻]" + scene
    updated = dict(row)
    # Host adapters strip this metadata before the request. Reused contexts still hash the original cue.
    updated["display_content"] = _user_content(row) or _EMPTY_OBSERVER_DISPLAY
    missing_cue = "" if match["cue"] else ("（剧情继续）" if session.get("turn_count") else "（剧目开场）")
    updated["content"] = _insert_stimulus(content, text, match.end("metadata"), stimulus, missing_cue)
    messages[index] = updated


def _project_dialogue(session: Mapping[str, Any], characters: Sequence[str]) -> str:
    thread, node = active_dialogue_beat(session)
    if not node:
        return ""
    is_observer = session.get("experience_mode") == "observer"
    if is_observer:
        lines = [
            SCRIPT_HEADER,
            "【观剧模式】观众处于第四面墙外静默观剧，不在场内、不是对话对象。严禁向「你」或观众搭话、提问或回应。",
            "【此刻】心理卡用于角色对台上同伴的情绪和说话口吻；没有隐形的首轮用户在场。",
            (
                f"在场角色（{'、'.join(characters)}）请直接彼此交谈与互动，自然推进眼前处境，沿用各自原本的口吻、话量与【此刻】的心理状态；"
                "若收到简短续拍信号（如「（剧情继续）」「（剧目开场…」），仅视为幕间续演提示，绝不要在台词里提及、复述或回应它。"
                if len(characters) > 1
                else "当前为单人独角戏观剧，请角色独自沉浸在当下处境与思绪中自然推进动作或自言自语，不要向「你」或镜头外的观众说话；"
                     "若收到简短续拍信号，仅视为续演提示，绝不要在台词里提及、复述或回应它。"
            ),
            "不要念出幕次、立场标签、剧本标题或指令，也不要复述提示里的句子。尚未付诸行动的心事没有说出口。",
        ]
    else:
        lines = [
            SCRIPT_HEADER,
            "把眼前的处境和心事当成正在经历的事，自然接话。沿用各自原本的口吻、话量与【此刻】的心理状态。",
            "不要念出幕次、立场标签、剧本标题或指令，也不要复述提示里的句子。尚未付诸行动的心事没有说出口。",
        ]
    if len(characters) > 1:
        lines.append("每位角色只知道自己的心事和亲历。别人的私念只有在本人说出口且该角色听到后才会知道。")
    lines.append("直接用各自的措辞、语气和节奏回应。可用短动作或沉默。避免轮流等长发言，"
                 "禁止用「复述对方刚说过的词 + …… + 嗯 / 也是 / 说得也是」起头。")
    scene_ctx = session.get("scene_context") or {}
    if scene_ctx.get("background_mode") == "shinsekai" and scene_ctx.get("opening_background"):
        bg_group = str(scene_ctx.get("opening_background") or "")
        bg_idx = int(node.get("background_scene_index") or scene_ctx.get("background_scene_index") or 0)
        bg_label = str(node.get("scene_name") or "")
        allow_shift = bool(scene_ctx.get("allow_scene_transitions", True))
        lines.append(f"Shinsekai 内置背景组：{bg_group}")
        if bg_idx > 0:
            lines.append(f"当前剧情场景对应内置背景：场景 {bg_idx}（{bg_label}）。若当前画面尚未处于该背景，请在回复开头的场景项切换到场景 {bg_idx}。")
        if allow_shift:
            lines.append("如需转场，请只切换到当前 Shinsekai 背景组内已有的内置场景，不要虚构背景组之外的场景画面。")
        else:
            lines.append("本段剧情保持在当前内置背景场景中，不要切换到其他场景背景。")
    for label, value in (
        ("共同知晓", thread.get("public_incident_fact")), ("场景", node.get("scene_name")),
        ("气氛", node.get("atmosphere")), ("眼前处境", node["situation"]), ("难处", node["dilemma_frame"]),
    ):
        if value:
            lines.append(f"{label}：{value}")
    facts = [_stage_excerpt(fact, 80) for fact in node.get("revealed_facts", [])[-4:] if fact]
    if facts:
        lines.append("已揭晓事实：" + " / ".join(dict.fromkeys(facts)))
    for name in characters:
        aid = f"character:{name.casefold()}"
        ledger = session["ledgers"].get(aid)
        if not ledger:
            continue
        role = (thread.get("resident_roles") or {}).get(aid) or ledger.get("story_role", {})
        lines.append(f"【{name}的视角与心事】")
        if role.get("local_role"):
            lines.append(f"你的身份：{role['local_role']}")
        if role.get("relationship_to_incident"):
            lines.append(f"只有你知道的牵挂：{role['relationship_to_incident']}")
        choice = node.get("participant_choices", {}).get(aid) or {}
        intent = choice.get("dialogue_intent", {})
        direction = dict.fromkeys(concise for value in (
            _INTENT_DIRECTIONS.get(intent.get("kind")), intent.get("target"), intent.get("hint"),
            choice.get("speech_seed"),
        ) if (concise := _stage_excerpt(value, 86).strip("。！？；， ")))
        if direction:
            lines.append("还在考虑的应对方向：" + "，".join(direction))
        reason = choice.get("inner_reason")
        if reason and reason != role.get("relationship_to_incident"):
            lines.append("私下的缘由：" + _stage_excerpt(reason, 100))
        arc = ledger.get("arc_state", {})
        for key, label in (("aspirations", "心愿"), ("promises", "记挂的承诺"),
                           ("secrets", "守着的秘密"), ("turning_points", "难忘的经历")):
            if arc.get(key):
                lines.append(f"{label}：" + "，".join(arc[key][-3:]))
        if arc.get("stance_toward"):
            lines.append("对他人的看法：" + "，".join(f"{key} {value}" for key, value in arc["stance_toward"].items()))
        for row in active_director_whispers(session, name):
            lines.append("此刻可能浮现的念头：" + _stage_excerpt(row["text"], 100))
    return "\n".join(lines)


def _bridge_url_and_token(path: str) -> tuple[str, str] | None:
    base = urlsplit(os.environ.get("SHINSEKAI_MEMORY_SERVICE_URL", ""))
    if base.scheme not in {"http", "https"} or not base.netloc:
        return None
    prefix = base.path.rstrip("/").removesuffix("/api/memory")
    url = urlunsplit((base.scheme, base.netloc, prefix + path, "", ""))
    token = os.environ.get("SHINSEKAI_MEMORY_SERVICE_TOKEN", "")
    return url, token


def _bridge_submit(action: str, values: Mapping[str, Any]) -> bool:
    """Return false only if the Bridge action could not be reached."""
    resolved = _bridge_url_and_token(f"/api/plugins/community.nanami.story/ui/story_lobby/actions/{action}")
    if resolved is None:
        return False
    url, token = resolved
    request = Request(url, data=json.dumps({"values": values}, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "X-Shinsekai-Bridge-Token": token}, method="POST")
    try:
        with urlopen(request, timeout=0.65) as response:
            result = json.load(response).get("result", {})
            if not result.get("ok"):
                logger.debug("Bridge rejected dialogue action %s: %s", action, result.get("error"))
        return True
    except HTTPError as exc:
        return exc.code < 500 and exc.code != 404
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def _bridge_chat_snapshot() -> dict[str, Any]:
    resolved = _bridge_url_and_token("/api/chat/snapshot")
    if resolved is None:
        return {}
    url, token = resolved
    request = Request(url, headers={"X-Shinsekai-Bridge-Token": token}, method="GET")
    try:
        with urlopen(request, timeout=0.65) as response:
            data = json.load(response)
            return dict(data) if isinstance(data, Mapping) else {}
    except (HTTPError, OSError, ValueError, AttributeError, TypeError):
        return {}


def _cli_history_file(argv: Sequence[str] | None = None) -> str:
    args = list(sys.argv[1:] if argv is None else argv)
    flags = ("--history", "--his")
    for idx, token in enumerate(args):
        text = str(token or "")
        for flag in flags:
            if text == flag and idx + 1 < len(args):
                return str(args[idx + 1] or "").strip()
            prefix = f"{flag}="
            if text.startswith(prefix):
                return text[len(prefix):].strip()
    return ""


def _is_bridge_child_process() -> bool:
    return ("SHINSEKAI_CHAT_LAUNCH_CONFIG" not in os.environ
            and os.environ.get("SHINSEKAI_MEMORY_SERVICE_OWNER") == "0")


class DialogueInjector:
    def __init__(self, fate_runtime, settings_provider, *, sync_models: bool = False, mouth_rewriter: Any = None) -> None:
        self._runtime = fate_runtime
        self._settings_provider = settings_provider
        self._sync_models = sync_models
        self._mouth_rewriter = mouth_rewriter
        self._native_story = False
        self._ready_watcher_started = False
        self.character_names: list[str] = []
        self.session_id = ""
        self.history_file = ""

    def _start_ready_reporter(self, fallback_history: str) -> None:
        if self._ready_watcher_started:
            return
        self._ready_watcher_started = True
        stop = threading.Event()

        def _watch() -> None:
            for _ in range(120):
                host_path = _host_history_file()
                if host_path:
                    # Wait briefly so wire_streaming_session & command handler finish attaching.
                    stop.wait(1.2)
                    clean = canonical_history_path(host_path or fallback_history)
                    if clean:
                        _bridge_submit("report_active_chat", {"history_path": clean, "ready": True})
                    return
                stop.wait(0.25)

        threading.Thread(target=_watch, name="nanami-story-chat-ready", daemon=True).start()

    def init_chat(self, context) -> None:
        self.character_names = dialogue_character_names(context.character_names)
        self.session_id = str(context.session_id or "")
        self._native_story = False
        try:
            config = json.loads(os.environ.get("SHINSEKAI_CHAT_LAUNCH_CONFIG", "{}"))
        except ValueError:
            config = {}
        config = config if isinstance(config, Mapping) else {}
        self.history_file = str(config.get("history") or "").strip()
        if not self.session_id:
            endpoint = str(config.get("stream_endpoint") or config.get("init_stream_endpoint") or "")
            self.session_id = parse_qs(urlsplit(endpoint).query).get("sessionId", [""])[0]
        if not self.history_file:
            self.history_file = _cli_history_file()
        host_history = _host_history_file()
        if not self.history_file:
            self.history_file = host_history
        is_bridge_child = _is_bridge_child_process()
        if is_bridge_child and (not self.history_file or not self.session_id):
            snapshot = _bridge_chat_snapshot()
            if not self.history_file:
                self.history_file = str(snapshot.get("historyPath") or "").strip()
            if not self.session_id:
                self.session_id = str(snapshot.get("sessionId") or "").strip()
        session = self._bound_session()
        if self.history_file:
            _bridge_submit("report_active_chat", {"history_path": canonical_history_path(self.history_file)})
        if is_bridge_child and not host_history and os.environ.get("SHINSEKAI_MEMORY_SERVICE_URL", ""):
            self._start_ready_reporter(self.history_file)
        if session:
            status = "已暂停" if session.get("paused") else "已就绪"
            context.report(message=f"命运剧本《{session['title']}》{status}")

    def _bound_session(self) -> dict[str, Any] | None:
        if not self._settings_provider().enabled:
            return None
        if not self.history_file:
            self.history_file = _host_history_file()
        return self._runtime.get_bound_dialogue_session(self.history_file) if self.history_file else None

    def is_native_story(self) -> bool:
        return self._native_story or history_has_native_story(self.history_file)

    def active_dialogue_session(self) -> tuple[dict[str, Any] | None, str, list[str]]:
        session = self._bound_session()
        return session, canonical_history_path(self.history_file), self.character_names or (
            session.get("dialogue_cast", []) if session else []
        )

    def before_chat(self, context) -> None:
        messages = context.messages
        messages[:] = [row for row in messages if not (
            row.get("role") == "system" and str(row.get("content", "")).startswith(SCRIPT_HEADER)
        )]
        self._native_story = any(NATIVE_SCRIPT_HEADER in str(row.get("content", "")) for row in messages)
        if self._native_story:
            return
        session = self._bound_session()
        if not session or session.get("paused"):
            return
        # The host registers its native script hook after plugin initialization.
        # Its prompt may not have been inserted when our hook runs.
        if history_has_native_story(self.history_file):
            self._native_story = True
            return
        history = _uncompacted_history(messages, self.history_file)
        prefixes = _user_prefixes(history, completed_only=True) if history is not None else []
        names = self.character_names or session.get("dialogue_cast", [])
        if _prefix_needs_sync(session, prefixes) or names != session.get("dialogue_cast"):
            values = {"session_id": session["session_id"], "history_path": canonical_history_path(self.history_file),
                      "characters": names, "user_history_prefixes": prefixes,
                      "expected_history_cursor": session.get("dialogue_history_cursor") or {}}
            if _bridge_submit("sync_dialogue_checkpoint", values):
                session = self._bound_session()
            else:
                session = self._runtime.sync_dialogue_checkpoint(**values)
        if not session or session.get("paused"):
            return
        if self._mouth_rewriter is not None:
            self._mouth_rewriter.apply_rewrites_to_messages(canonical_history_path(self.history_file), messages)
        if session.get("experience_mode") == "observer":
            _enrich_observer_cue(messages, session)
        block = _project_dialogue(session, names)
        if block:
            index = next((i for i, row in enumerate(messages) if row.get("role") != "system"), len(messages))
            messages.insert(index, {"role": "system", "content": block})

    def message_added(self, context) -> None:
        if context.role != "assistant" or self._native_story:
            return
        utterances = _assistant_utterances(context.message)
        if not utterances or any(NATIVE_SCRIPT_HEADER in str(row.get("content", "")) for row in context.messages):
            return
        session = self._bound_session()
        if not session or session.get("paused") or history_has_native_story(self.history_file):
            return
        names = self.character_names or session.get("dialogue_cast", [])
        known = {name.casefold() for name in names}
        utterances = [row for row in utterances if row["character_name"].casefold() in known]
        if not utterances:
            return
        user = next((row for row in reversed(context.messages) if row.get("role") == "user"
                     and not _is_summary(row)), {})
        if self._mouth_rewriter is not None:
            history_key = canonical_history_path(self.history_file)
            turn_id = str(user.get("_chatTurnId") or "")
            self._mouth_rewriter.complete_turn(
                history_key, context.message, turn_id, names, scene_hint=_scene_hint(session),
            )
            utterances = self._mouth_rewriter.apply_rewrites_to_utterances(
                history_key, utterances, message=context.message, turn_id=turn_id,
            )
        user_text = clip_text(_user_content(user), 4000)
        history = _uncompacted_history(context.messages, self.history_file)
        prefixes = _user_prefixes(history, completed_only=False) if history is not None else []
        signature = hashlib.sha256(json.dumps(
            [prefixes[-1] if prefixes else "", user.get("_chatTurnId"), user_text, utterances],
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest()
        values = {"session_id": session["session_id"], "history_path": canonical_history_path(self.history_file),
                  "user_text": user_text, "assistant_utterances": utterances, "present_characters": names,
                  "user_history_prefixes": prefixes, "turn_signature": signature,
                  "expected_history_cursor": session.get("dialogue_history_cursor") or {}}
        if not _bridge_submit("ingest_dialogue_turn", values):
            self._runtime.ingest_host_dialogue_turn(**values, sync_models=self._sync_models)
