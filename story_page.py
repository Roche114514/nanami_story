"""故事引擎前端大厅、命运丝线编年史 RPC、配置页与聊天工具条注册。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .fate_schema import redact_fate_session_for_ui

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
        return {
            "ok": True,
            "world_available": bool(world is not None and world.available()),
            "characters": chars,
            "realms": _realms_list(),
            "sessions": sessions,
            "fate_sessions": sessions,
            "settings": cfg.to_dict(),
        }

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
        return {
            "ok": True,
            "session": redacted,
            "thread": res["thread"],
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
        }

    action_specs: list[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]] = [
        ("get_lobby_state", "获取命运故事大厅状态", _act_get_lobby_state),
        ("create_fate_story", "新建由命运丝线驱动的故事舞台", _act_create_fate_story),
        ("attach_fate_world", "挂载常驻小镇开启命运丝线", _act_attach_fate_world),
        ("pluck_fate_thread", "轻拨新命运丝线", _act_pluck_fate_thread),
        ("tick_fate_session", "推演命运丝线节点", _act_tick_fate_session),
        ("toggle_fate_thoughts", "切换角色心路可见性", _act_toggle_fate_thoughts),
        ("record_fate_player_stance", "记录玩家节点表态", _act_record_fate_player_stance),
        ("switch_to_story_world", "切到故事世界画布", _act_switch_to_story_world),
        ("detach_fate_session", "解绑或删除命运故事舞台", _act_detach_fate_session),
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
            description="新建命运故事小镇或挂载常驻小镇，查看全角色非全知记事与抉择轨迹。",
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
