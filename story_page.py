"""故事引擎前端大厅、命运丝线编年史 RPC、配置页与聊天工具条注册。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .character_chronicle import active_dialogue_beat
from .fate_runtime import dialogue_stage_cue
from .fate_schema import redact_fate_session_for_ui
from .settings import canonical_history_path, dialogue_character_names, history_has_native_story

logger = logging.getLogger(__name__)

LOBBY_PAGE_ID = "story_lobby"
SETTINGS_PAGE_ID = "story_settings"


def _err(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": str(msg or "操作失败")}


def register_pages(
    register: Any,
    *,
    code_dir: Path,
    fate_runtime: Any,
    settings_store: Any,
    world_adapter: Any = None,
    persona_adapter: Any = None,
    host_chat_adapter: Any = None,
    background_adapter: Any = None,
) -> None:
    """向宿主注册故事引擎页面、命运丝线 RPC、设置页与聊天工具条按钮。"""
    from sdk.types import (
        FrontendChatUIContribution,
        FrontendConfigAction,
        FrontendConfigContribution,
        FrontendPageContribution,
    )

    world = world_adapter or getattr(fate_runtime, "_world", None)
    persona = persona_adapter or getattr(fate_runtime, "_persona", None)

    def _safe_run(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        def _runner(values: Mapping[str, Any]) -> dict[str, Any]:
            try:
                return fn(dict(values or {}))
            except ValueError as exc:
                return _err(str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception("nanami_story: 前端动作执行异常")
                return _err(f"{type(exc).__name__}: {exc}")

        return _runner

    def _sessions_list() -> list[dict[str, Any]]:
        if fate_runtime is None:
            return []
        return [redact_fate_session_for_ui(s) for s in fate_runtime.list_sessions()]

    def _realms_list() -> list[dict[str, Any]]:
        if world is None or not world.available():
            return []
        try:
            res = world.list_realms()
            return list(res.get("realms") or ())
        except Exception:
            return []

    def _act_get_lobby_state(_v: dict[str, Any]) -> dict[str, Any]:
        cfg = settings_store.get()
        chars = []
        if persona is not None:
            chars = persona.list_available_characters(
                custom_persona_dir=str(getattr(cfg, "persona_data_dir", "") or ""),
            )
        sessions = _sessions_list()
        if fate_runtime is not None:
            backgrounds = fate_runtime.list_host_backgrounds()
        else:
            backgrounds = background_adapter.list_backgrounds() if background_adapter is not None else []
        return {
            "ok": True,
            "world_available": bool(world is not None and world.available()),
            "characters": chars,
            "backgrounds": backgrounds,
            "realms": _realms_list(),
            "sessions": sessions,
            "fate_sessions": sessions,
            "settings": cfg.to_dict(),
            "dialogue_presets": fate_runtime.dialogue_presets.list() if fate_runtime else [],
            "chat_bindings": fate_runtime.list_chat_bindings() if fate_runtime else {},
        }

    def _act_set_dialogue_mouth(v: dict[str, Any]) -> dict[str, Any]:
        enabled = v.get("dialogue_mouth_decoupling")
        if not isinstance(enabled, bool):
            raise ValueError("请选择是否启用对话双层解耦。")
        cfg = settings_store.update({"dialogue_mouth_decoupling": enabled})
        return {"ok": True, "settings": cfg.to_dict()}

    def _act_create_fate_story(v: dict[str, Any]) -> dict[str, Any]:
        sess = fate_runtime.start_standalone_stage(v)
        redacted = redact_fate_session_for_ui(sess)
        sessions = _sessions_list()
        return {
            "ok": True,
            "session": redacted,
            "sessions": sessions,
            "fate_sessions": sessions,
            "realms": _realms_list(),
        }

    def _act_attach_fate_world(v: dict[str, Any]) -> dict[str, Any]:
        realm_sid = str(v.get("realm_space_id") or v.get("space_id") or "").strip()
        sess = fate_runtime.attach_world(
            realm_sid,
            auto_pluck=bool(v.get("auto_pluck", True)),
            theme_compass=str(v.get("theme_compass") or "auto"),
            prompt_hint=str(v.get("prompt_hint") or ""),
            reveal_inner_thoughts=bool(v.get("reveal_inner_thoughts", False)),
            use_llm=v.get("use_llm") if "use_llm" in v else None,
        )
        redacted = redact_fate_session_for_ui(sess)
        sessions = _sessions_list()
        return {
            "ok": True,
            "session": redacted,
            "sessions": sessions,
            "fate_sessions": sessions,
        }

    def _act_pluck_fate_thread(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("session_id") or "").strip()
        if not sid:
            raise ValueError("需要提供 session_id。")
        res = fate_runtime.pluck_thread(
            sid,
            theme_compass=str(v.get("theme_compass") or "auto"),
            prompt_hint=str(v.get("prompt_hint") or ""),
            use_llm=v.get("use_llm") if "use_llm" in v else None,
        )
        redacted = redact_fate_session_for_ui(res["session"])
        sessions = _sessions_list()
        thread = res["thread"]
        if redacted.get("mode") == "dialogue":
            thread = next(t for t in redacted["threads"] if t["thread_id"] == thread["thread_id"])
        return {
            "ok": True,
            "session": redacted,
            "thread": thread,
            "sessions": sessions,
            "fate_sessions": sessions,
        }

    def _act_tick_fate_session(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("session_id") or "").strip()
        if not sid:
            fate_runtime.tick_all()
            sessions = _sessions_list()
            return {"ok": True, "sessions": sessions, "fate_sessions": sessions}
        sess = fate_runtime.tick_session(
            sid,
            force_evaluate=bool(v.get("force_evaluate", False)),
            use_llm=v.get("use_llm") if "use_llm" in v else None,
            sync_models=bool(v.get("sync_models", True)),
        )
        if sess is None:
            raise ValueError(f"命运会话不存在：{sid}")
        redacted = redact_fate_session_for_ui(sess)
        sessions = _sessions_list()
        return {
            "ok": True,
            "session": redacted,
            "sessions": sessions,
            "fate_sessions": sessions,
        }

    def _act_toggle_fate_thoughts(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("session_id") or "").strip()
        if not sid:
            raise ValueError("需要提供 session_id。")
        cur = fate_runtime.get_session(sid)
        if cur is None:
            raise ValueError(f"命运会话不存在：{sid}")
        target = bool(v["reveal"]) if "reveal" in v else (not bool(cur.get("reveal_inner_thoughts", False)))
        sess = fate_runtime.set_reveal_inner_thoughts(sid, target)
        redacted = redact_fate_session_for_ui(sess)
        sessions = _sessions_list()
        return {
            "ok": True,
            "session": redacted,
            "sessions": sessions,
            "fate_sessions": sessions,
        }

    def _act_record_fate_player_stance(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("session_id") or "").strip()
        if not sid:
            raise ValueError("需要提供 session_id。")
        sess = fate_runtime.record_player_stance(
            sid,
            str(v.get("stance") or ""),
            note=str(v.get("note") or ""),
            thread_id=str(v.get("thread_id") or ""),
        )
        redacted = redact_fate_session_for_ui(sess)
        sessions = _sessions_list()
        return {
            "ok": True,
            "session": redacted,
            "sessions": sessions,
            "fate_sessions": sessions,
        }

    def _act_switch_to_story_world(v: dict[str, Any]) -> dict[str, Any]:
        realm_sid = str(v.get("realm_space_id") or v.get("space_id") or "").strip()
        if not realm_sid:
            sid = str(v.get("session_id") or "").strip()
            sess = fate_runtime.get_session(sid) if sid else None
            realm_sid = str((sess or {}).get("realm_space_id") or "")
        if not realm_sid:
            raise ValueError("缺少目标故事世界 ID。")
        if world is None:
            raise RuntimeError("世界桥接器尚未就绪。")
        world.switch_world(realm_sid)
        return {"ok": True, "realm_space_id": realm_sid}

    def _act_detach_fate_session(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("session_id") or "").strip()
        if not sid:
            raise ValueError("需要提供 session_id。")
        res = fate_runtime.detach_session(sid, delete_world=bool(v.get("delete_world", False)))
        sessions = _sessions_list()
        return {
            **res,
            "sessions": sessions,
            "fate_sessions": sessions,
            "realms": _realms_list(),
            "chat_bindings": fate_runtime.list_chat_bindings(),
        }

    def _dialogue_result(session: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "session": redact_fate_session_for_ui(session), "sessions": _sessions_list(),
                "chat_bindings": fate_runtime.list_chat_bindings()}

    def _session_id(v: dict[str, Any]) -> str:
        sid = str(v.get("session_id") or "").strip()
        if not sid:
            raise ValueError("需要提供 session_id。")
        return sid

    def _reject_native_story(v: dict[str, Any]) -> None:
        if v.get("kind") == "story" or history_has_native_story(str(v.get("history_path") or "")):
            raise ValueError("此对话正在使用 Shinsekai 原生剧本，请选择普通对话。")

    def _act_preview_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "draft": fate_runtime.preview_dialogue_story(v)}

    def _act_trigger_dialogue_opening(v: dict[str, Any]) -> dict[str, Any]:
        _reject_native_story(v)
        path = canonical_history_path(str(v.get("history_path") or ""))
        session = fate_runtime.get_bound_dialogue_session(path) if path else None
        if (not settings_store.get().enabled or not session or session["session_id"] != _session_id(v)
                or session.get("paused") or not active_dialogue_beat(session)[1]):
            raise ValueError("请先打开已绑定且未暂停的剧本对话。")
        cue = dialogue_stage_cue(session, is_opening=True)
        if not host_chat_adapter or not host_chat_adapter.submit_text(cue):
            raise ValueError("当前对话无法接收开场引子。")
        fate_runtime.mark_observer_cue_dispatched(session["session_id"], session.get("turn_count", 0), path)
        return {"ok": True}

    def _act_set_dialogue_experience_mode(v: dict[str, Any]) -> dict[str, Any]:
        return _dialogue_result(
            fate_runtime.set_dialogue_experience_mode(
                _session_id(v), str(v.get("experience_mode") or "co_play"),
            )
        )

    def _act_set_dialogue_background(v: dict[str, Any]) -> dict[str, Any]:
        return _dialogue_result(fate_runtime.set_dialogue_background(
            _session_id(v), background_mode=str(v.get("background_mode") or "custom"),
            opening_background=str(v.get("opening_background") or ""),
            opening_scene_index=v.get("opening_scene_index", 0),
            allow_scene_transitions=bool(v.get("allow_scene_transitions", True)),
        ))

    def _act_toggle_dialogue_autoplay(v: dict[str, Any]) -> dict[str, Any]:
        sid = _session_id(v)
        enabled = bool(v.get("enabled", False))
        path = canonical_history_path(str(v.get("history_path") or ""))
        session = fate_runtime.set_dialogue_observer_autoplay(
            sid,
            enabled=enabled,
            interval_sec=v.get("observer_interval_sec"),
            history_path=path,
            max_auto_turns=v.get("max_auto_turns"),
            persist_preference=True,
        )
        if enabled and v.get("trigger_now"):
            if path and session.get("linked_history_path") != path:
                fate_runtime.set_dialogue_observer_autoplay(sid, enabled=False)
                raise ValueError("请先打开此剧本绑定的对话。")
            cue = dialogue_stage_cue(session, is_opening=(int(session.get("turn_count") or 0) == 0))
            if host_chat_adapter and host_chat_adapter.submit_text(cue):
                fate_runtime.mark_observer_cue_dispatched(
                    sid,
                    session.get("turn_count", 0),
                    path or session.get("linked_history_path", ""),
                    count_as_auto=True,
                )
                session = fate_runtime.get_session(sid) or session
        return _dialogue_result(session)

    def _act_step_dialogue_observer(v: dict[str, Any]) -> dict[str, Any]:
        _reject_native_story(v)
        path = canonical_history_path(str(v.get("history_path") or ""))
        session = fate_runtime.get_bound_dialogue_session(path) if path else None
        if (not settings_store.get().enabled or not session or session["session_id"] != _session_id(v)
                or session.get("paused") or not active_dialogue_beat(session)[1]):
            raise ValueError("请先打开已绑定且未暂停的剧本对话。")
        cue = dialogue_stage_cue(session, is_opening=(int(session.get("turn_count") or 0) == 0))
        if not host_chat_adapter or not host_chat_adapter.submit_text(cue):
            raise ValueError("当前对话无法接收续演信号。")
        fate_runtime.mark_observer_cue_dispatched(session["session_id"], session.get("turn_count", 0), path)
        return _dialogue_result(fate_runtime.get_session(session["session_id"]) or session)

    def _act_report_active_chat(v: dict[str, Any]) -> dict[str, Any]:
        fate_runtime.report_active_chat(
            str(v.get("history_path") or ""),
            ready=bool(v.get("ready", False)),
        )
        return {"ok": True}

    def _act_create_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        _reject_native_story(v)
        session = fate_runtime.start_dialogue_story(v)
        result = _dialogue_result(session)
        if v.get("trigger_opening"):
            try:
                _act_trigger_dialogue_opening({**v, "session_id": session["session_id"]})
                result["opening_submitted"] = True
            except ValueError as exc:
                result["opening_error"] = str(exc)
        return result

    def _act_bind_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        _reject_native_story(v)
        history = str(v.get("history_path") or "").strip()
        if not history:
            raise ValueError("请选择要绑定的对话。")
        return _dialogue_result(fate_runtime.bind_dialogue_session(
            _session_id(v), history, conversation_id=str(v.get("conversation_id") or ""),
            characters=dialogue_character_names(v.get("characters")),
        ))

    def _act_unbind_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        return _dialogue_result(fate_runtime.bind_dialogue_session(_session_id(v), ""))

    def _act_pause_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        return _dialogue_result(fate_runtime.toggle_dialogue_pause(_session_id(v), v.get("paused")))

    def _act_whisper(v: dict[str, Any]) -> dict[str, Any]:
        return _dialogue_result(fate_runtime.inject_director_whisper(
            _session_id(v), str(v.get("text") or ""), target=str(v.get("target") or "").strip(),
            duration_turns=int(v.get("duration_turns") or 2),
        ))

    def _act_advance_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        trigger_cue = bool(v.get("trigger_observer_cue"))
        session = fate_runtime.advance_dialogue_beat(
            _session_id(v), force_evaluate=True, use_llm=v.get("use_llm"),
            sync_models=trigger_cue or bool(v.get("sync_models", True)),
        )
        result = _dialogue_result(session)
        if trigger_cue:
            req_path = canonical_history_path(str(v.get("history_path") or ""))
            linked_path = session.get("linked_history_path")
            if (settings_store.get().enabled and session.get("mode") == "dialogue"
                    and session.get("experience_mode") == "observer" and not session.get("paused")
                    and active_dialogue_beat(session)[1] and linked_path and (not req_path or req_path == linked_path)
                    and host_chat_adapter and host_chat_adapter.available()):
                turn_count = int(session.get("turn_count") or 0)
                cue = dialogue_stage_cue(session, is_opening=(turn_count == 0))
                submitted = host_chat_adapter.submit_text(cue)
                result["cue_submitted"] = submitted
                if submitted:
                    fate_runtime.mark_observer_cue_dispatched(session["session_id"], turn_count, linked_path)
                else:
                    result["cue_error"] = "当前对话未处于可接收状态，后台剧情已推进但未能推送续演信号。"
        return result

    def _act_pluck_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        return _dialogue_result(fate_runtime.pluck_dialogue_thread(
            _session_id(v), theme_compass=str(v.get("theme_compass") or "auto"),
            prompt_hint=str(v.get("prompt_hint") or ""), use_llm=v.get("use_llm"),
        )["session"])

    def _prefixes(v: dict[str, Any]) -> list[str]:
        raw = v.get("user_history_prefixes") or []
        if not isinstance(raw, list) or not all(isinstance(value, str) and len(value) == 64 for value in raw):
            raise ValueError("无效的对话历史指纹。")
        return raw

    def _expected_cursor(v: dict[str, Any]) -> Mapping[str, Any] | None:
        cursor = v.get("expected_history_cursor")
        if cursor is not None and not isinstance(cursor, Mapping):
            raise ValueError("无效的对话检查点。")
        return cursor

    def _act_ingest_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        raw = v.get("assistant_utterances")
        utterances = [row for row in raw[:48] if isinstance(row, Mapping)] if isinstance(raw, list) else []
        session = fate_runtime.ingest_host_dialogue_turn(
            _session_id(v), history_path=str(v.get("history_path") or ""), user_text=str(v.get("user_text") or ""),
            assistant_utterances=utterances, present_characters=dialogue_character_names(v.get("present_characters")),
            user_history_prefixes=_prefixes(v), turn_signature=str(v.get("turn_signature") or "")[:128],
            use_llm=v.get("use_llm"), sync_models=bool(v.get("sync_models", False)),
            expected_history_cursor=_expected_cursor(v),
        )
        return {"ok": True, "revision": session["revision"]}

    def _act_sync_dialogue(v: dict[str, Any]) -> dict[str, Any]:
        session = fate_runtime.sync_dialogue_checkpoint(
            _session_id(v), history_path=str(v.get("history_path") or ""), user_history_prefixes=_prefixes(v),
            characters=dialogue_character_names(v.get("characters")),
            expected_history_cursor=_expected_cursor(v),
        )
        return {"ok": True, "revision": session["revision"]}

    def _act_list_presets(_v: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "dialogue_presets": fate_runtime.dialogue_presets.list()}

    def _act_save_preset(v: dict[str, Any]) -> dict[str, Any]:
        raw = v.get("preset")
        if v.get("session_id"):
            session = fate_runtime.get_session(_session_id(v)) or {}
            raw = session.get("dialogue_preset")
        if not isinstance(raw, Mapping):
            raise ValueError("需要提供剧本预设。")
        preset = fate_runtime.dialogue_presets.save(raw, preset_id=str(v.get("preset_id") or ""))
        return {**_act_list_presets(v), "preset": preset}

    def _act_delete_preset(v: dict[str, Any]) -> dict[str, Any]:
        fate_runtime.dialogue_presets.delete(str(v.get("preset_id") or ""))
        return _act_list_presets(v)

    def _act_export_preset(v: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "json": fate_runtime.dialogue_presets.export_json(str(v.get("preset_id") or ""))}

    def _act_import_preset(v: dict[str, Any]) -> dict[str, Any]:
        preset = fate_runtime.dialogue_presets.import_json(v.get("json"))
        return {**_act_list_presets(v), "preset": preset}

    action_specs: list[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]] = [
        ("get_lobby_state", "获取命运故事大厅状态", _act_get_lobby_state),
        ("set_dialogue_mouth", "保存对话双层解耦设置", _act_set_dialogue_mouth),
        ("create_fate_story", "新建由命运丝线驱动的故事舞台", _act_create_fate_story),
        ("attach_fate_world", "挂载常驻小镇开启命运丝线", _act_attach_fate_world),
        ("pluck_fate_thread", "轻拨新命运丝线", _act_pluck_fate_thread),
        ("tick_fate_session", "推演命运丝线节点", _act_tick_fate_session),
        ("toggle_fate_thoughts", "切换角色心路可见性", _act_toggle_fate_thoughts),
        ("record_fate_player_stance", "记录玩家节点表态", _act_record_fate_player_stance),
        ("switch_to_story_world", "切到故事世界画布", _act_switch_to_story_world),
        ("detach_fate_session", "解绑或删除命运故事舞台", _act_detach_fate_session),
        ("preview_dialogue_story", "预览对话剧本开局", _act_preview_dialogue),
        ("create_dialogue_story", "创建对话剧本", _act_create_dialogue),
        ("bind_dialogue_session", "绑定对话剧本", _act_bind_dialogue),
        ("unbind_dialogue_session", "解除对话绑定", _act_unbind_dialogue),
        ("toggle_dialogue_pause", "暂停或恢复剧本注入", _act_pause_dialogue),
        ("inject_director_whisper", "推入导演耳语", _act_whisper),
        ("advance_dialogue_beat", "推进对话剧情", _act_advance_dialogue),
        ("pluck_dialogue_thread", "开启下一篇对话剧本", _act_pluck_dialogue),
        ("ingest_dialogue_turn", "记录主对话实录", _act_ingest_dialogue),
        ("sync_dialogue_checkpoint", "同步剧本检查点", _act_sync_dialogue),
        ("list_dialogue_presets", "列出对话剧本预设", _act_list_presets),
        ("save_dialogue_preset", "保存对话剧本预设", _act_save_preset),
        ("delete_dialogue_preset", "删除对话剧本预设", _act_delete_preset),
        ("export_dialogue_preset", "导出对话剧本预设", _act_export_preset),
        ("import_dialogue_preset", "导入对话剧本预设", _act_import_preset),
        ("trigger_dialogue_opening", "推入开场引子", _act_trigger_dialogue_opening),
        ("set_dialogue_experience_mode", "切换对话剧本观剧模式", _act_set_dialogue_experience_mode),
        ("set_dialogue_background", "切换对话剧本背景", _act_set_dialogue_background),
        ("toggle_dialogue_autoplay", "切换自动连续观剧", _act_toggle_dialogue_autoplay),
        ("step_dialogue_observer", "观剧续演一轮", _act_step_dialogue_observer),
        ("report_active_chat", "同步当前活跃对话", _act_report_active_chat),
    ]

    actions = [
        FrontendConfigAction(
            id=aid,
            label=label,
            variant="ghost",
            order=float(idx + 1),
            run=_safe_run(fn),
        )
        for idx, (aid, label, fn) in enumerate(action_specs)
    ]

    story_html = code_dir / "web" / "story.html"
    if story_html.is_file() and hasattr(register, "register_frontend_page"):
        register.register_frontend_page(
            FrontendPageContribution(
                page_id=LOBBY_PAGE_ID,
                title="故事引擎",
                entry=str(story_html),
                kind="tools",
                description="以「命运丝线」驱动活态故事世界，让人格在节点上自主选择并滚动长出自己的故事。",
                order=66.0,
            )
        )

    register.register_frontend_config_page(
        FrontendConfigContribution(
            page_id=LOBBY_PAGE_ID,
            title="故事引擎",
            kind="tools",
            description="开启对话剧本、新建故事小镇或挂载常驻小镇，查看角色的记事与抉择。",
            schema=[],
            load_values=lambda: {
                "world_available": bool(world is not None and world.available()),
                "active_stories": len([s for s in fate_runtime.list_sessions() if s.get("status") == "active"]),
            },
            save_values=lambda _v: None,
            actions=actions,
            order=66.0,
        )
    )

    settings_schema = [
        {
            "id": "story_runtime",
            "title": "命运丝线故事引擎",
            "fields": [
                {"key": "enabled", "type": "boolean", "label": "启用命运丝线引擎", "description": "关闭后不再后台消费事件流、记录角色见闻或推演节点"},
                {"key": "poll_interval_sec", "type": "number", "label": "世界事件轮询间隔（秒）", "min": 0.5, "max": 30.0, "step": 0.5},
                {"key": "use_llm_compiler", "type": "boolean", "label": "使用大模型实时编织命运节点与角色抉择", "description": "开启后由模型结合人物底色生成节点局势与自由选择；失败或离线自动回退性格推演"},
                {"key": "dialogue_mouth_decoupling", "type": "boolean", "label": "开启对话大脑-嘴巴双层解耦", "description": "按各角色声线改写台词，减少重复与 AI 味。随大模型编织功能启用。"},
                {
                    "key": "default_experience_mode",
                    "type": "select",
                    "label": "新建故事默认体验模式",
                    "options": [
                        {"value": "co_play", "label": "自然融入（玩家自由漫游与互动影响丝线）"},
                        {"value": "observer", "label": "纯观剧见证（全员自主抉择推进故事）"},
                    ],
                },
                {"key": "persona_data_dir", "type": "text", "label": "人格模型数据目录（可选覆盖）", "placeholder": "留空自动探测 data/nanami"},
            ],
        }
    ]
    register.register_frontend_config_page(
        FrontendConfigContribution(
            page_id=SETTINGS_PAGE_ID,
            title="故事引擎设置",
            kind="settings",
            description="配置命运丝线引擎的事件轮询间隔、实时编织模型与默认体验模式。",
            schema=settings_schema,
            load_values=lambda: settings_store.get().to_dict(),
            save_values=lambda values: settings_store.update(values),
            order=66.0,
        )
    )

    if hasattr(register, "register_frontend_chat_ui"):
        try:
            register.register_frontend_chat_ui(
                FrontendChatUIContribution(
                    contribution_id="open_story",
                    slot="chat-toolbar",
                    title="故事引擎",
                    description="打开故事引擎大厅：新建命运故事或挂载常驻小镇",
                    icon="book-open",
                    presentation="button",
                    variant="ghost",
                    order=66.0,
                    action={"type": "open-plugin-page", "page_id": LOBBY_PAGE_ID, "mode": "overlay"},
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("nanami_story: 注册聊天工具条按钮失败", exc_info=True)
