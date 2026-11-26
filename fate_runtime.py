"""Fate Weaver v3 主控制器：附着活态小镇或开放舞台，驱动记事、自主抉择与滚动编排。"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .adapters.background_adapter import (
    BACKGROUND_SCENE_KEYS, BackgroundAdapter, background_scene_fields, resolve_catalog_scene,
)
from .adapters.llm_adapter import ask_llm_json
from .adapters.persona_adapter import PersonaAdapter
from .adapters.world_adapter import WorldAdapter
from .beat_weaver import (
    build_fate_overlay, evaluate_and_weave_dialogue_beat, preview_dialogue_opening,
    weave_dialogue_opening, weave_next_beat, weave_opening_thread,
)
from .character_chronicle import (
    active_dialogue_beat,
    apply_choice_to_ledger,
    ingest_dialogue_turn,
    ingest_world_events,
    resolve_thread_lead_ids,
    sync_cast_ledgers,
)
from .choice_engine import decide_actor_choice, decide_dialogue_actor_choice
from .fate_schema import (
    NATIVE_RESIDENT_ARCHETYPES,
    STANCE_LABELS,
    VALID_EXPERIENCE_MODES,
    VALID_STANCES,
    clip_text,
    dialogue_needs_review,
    make_character_ledger,
    make_fate_session,
    normalize_max_nodes,
)
from .settings import (
    ChatBindingsStore, DialoguePresetStore, atomic_write_json, canonical_history_path,
    dialogue_character_names, dialogue_write_lock, file_mtime_ns, normalize_dialogue_background,
    normalize_dialogue_preset, read_json_object,
)

logger = logging.getLogger(__name__)

OBSERVER_CONTINUE_CUE = "（剧情继续）"
OBSERVER_DEFAULT_MAX_AUTO_TURNS = 0
OBSERVER_PENDING_TIMEOUT_SEC = 180.0
OBSERVER_MAX_SUBMIT_FAILURES = 3


def _normalize_observer_max_turns(value: Any, default: int = OBSERVER_DEFAULT_MAX_AUTO_TURNS) -> int:
    if value is None:
        return default
    try:
        return max(0, min(1000, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _clamp_observer_interval(value: Any, default: float = 8.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return max(3.0, min(60.0, parsed))


def dialogue_stage_cue(session: Mapping[str, Any], *, is_opening: bool = False) -> str:
    """根据体验模式与当前轮次生成推入宿主聊天的开场/续演信号。"""
    if session.get("experience_mode") == "observer":
        if is_opening or int(session.get("turn_count") or 0) == 0:
            if len(session.get("dialogue_cast") or ()) > 1:
                return "（剧目开场，在场角色自然展开互动）"
            return "（剧目开场，独自沉浸在当下处境中展开）"
        return OBSERVER_CONTINUE_CUE
    return "就从此刻开始吧。"


def _infer_stage_atmosphere(title: str, world_prompt: str, outline: str) -> tuple[float, str]:
    """从标题与背景描述推断开局时间段（日/暮/夜）与天气。"""
    text = f"{title} {world_prompt} {outline}".strip()
    if re.search(r"(?:深夜|午夜|半夜|雨夜|夜里|夜晚|夜)", text):
        hour = 21.0
    elif re.search(r"(?:黄昏|傍晚|夕阳)", text):
        hour = 18.5
    else:
        hour = 14.0

    if re.search(r"(?:暴雨|雷雨|大雨|雾雨|雨夜|下雨|雨)", text):
        weather = "rain"
    elif re.search(r"(?:暴雪|大雪|下雪|雪夜|雪)", text):
        weather = "snow"
    elif re.search(r"(?:大雾|浓雾|迷雾|雾)", text):
        weather = "fog"
    else:
        weather = ""
    return hour, weather


class FateSessionStore:
    """命运会话原子持久化仓储。"""

    def __init__(self, data_root: Path) -> None:
        self._dir = Path(data_root) / "fate_sessions"
        self._mtimes: dict[Path, int] = {}

    def save(self, session: dict[str, Any]) -> None:
        sid = str(session.get("session_id") or "").strip()
        if not sid:
            return
        path = self._dir / f"{sid}.json"
        revision = int(session.get("revision", 0)) + 1
        atomic_write_json(path, {**session, "revision": revision})
        session["revision"] = revision
        self._mtimes[path] = file_mtime_ns(path)

    def delete(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        path = self._dir / f"{sid}.json"
        if path.is_file():
            try:
                path.unlink()
                self._mtimes.pop(path, None)
            except OSError:
                logger.debug("删除命运会话文件失败：%s", path, exc_info=True)

    def load_all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        self._mtimes.clear()
        self.reload_if_changed(out)
        return out

    def reload_if_changed(self, sessions: dict[str, dict[str, Any]]) -> None:
        paths = {p for p in self._dir.glob("*.json") if not p.name.startswith(".")}
        for path in self._mtimes.keys() - paths:
            self._mtimes.pop(path)
            sessions.pop(path.stem, None)
        for path in sorted(paths):
            stamp = file_mtime_ns(path)
            if stamp is None or self._mtimes.get(path) == stamp:
                continue
            raw = read_json_object(path)
            if raw and raw.get("session_id") == path.stem and raw.get("version") == 3:
                sessions[path.stem] = raw
                self._mtimes[path] = stamp


class FateRuntime:
    """线程安全的命运丝线（Fate Weaver v3）控制器。"""

    def __init__(
        self,
        world_adapter: WorldAdapter,
        persona_adapter: PersonaAdapter,
        data_root: Path,
        *,
        settings_provider: Callable[[], Any] | None = None,
        llm_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
        background_adapter: BackgroundAdapter | None = None,
    ) -> None:
        self._world = world_adapter
        self._persona = persona_adapter
        self._background_adapter = background_adapter if background_adapter is not None else BackgroundAdapter(Path.cwd())
        self._data_root = Path(data_root)
        self._settings_provider = settings_provider
        self._llm_fn = llm_fn
        self._store = FateSessionStore(self._data_root)
        self._chat_bindings = ChatBindingsStore(self._data_root)
        self.dialogue_presets = DialoguePresetStore(self._data_root)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._model_jobs = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nanami-fate-weaver")
        self._model_slots = threading.BoundedSemaphore(8)
        self._inflight: set[tuple[str, ...]] = set()
        self._dialogue_realms: set[str] = set()
        self._closed = False
        self._pending_dialogue: dict[str, tuple[bool, bool]] = {}
        self._active_chat_history_path: str = ""
        self._observer_autoplay_state: dict[str, dict[str, Any]] = {}
        self._pending_observer_openings: dict[str, str] = {}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._model_jobs.shutdown(wait=False, cancel_futures=True)
            for realm in list(self._dialogue_realms):
                self._clear_dialogue_focus_locked(realm)

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """等待后台进行的命运编排/角色抉择异步任务完成（用于测试或同步刷盘）。"""
        deadline = time.monotonic() + max(0.05, float(timeout))
        while time.monotonic() < deadline:
            with self._lock:
                if not self._inflight:
                    return True
            time.sleep(0.02)
        with self._lock:
            return not self._inflight

    def _custom_persona_dir(self) -> str:
        if self._settings_provider is None:
            return ""
        try:
            cfg = self._settings_provider()
            return str(getattr(cfg, "persona_data_dir", "") or "")
        except Exception:
            return ""

    def _effective_use_llm(self, override: bool | None = None) -> bool:
        if override is not None:
            return bool(override)
        if self._llm_fn is not None:
            return True
        if self._settings_provider is not None:
            try:
                cfg = self._settings_provider()
                return bool(getattr(cfg, "use_llm_compiler", True))
            except Exception:
                return True
        return False

    def _submit_async(self, key: tuple[str, ...], work: Callable[[], None]) -> bool:
        with self._lock:
            if key in self._inflight:
                return False
            if not self._model_slots.acquire(blocking=False):
                return False
            self._inflight.add(key)

        def _runner() -> None:
            try:
                work()
            except Exception:
                logger.debug("nanami_story.fate_runtime 异步任务失败: %s", key, exc_info=True)
            finally:
                with self._lock:
                    self._inflight.discard(key)
                self._model_slots.release()

        try:
            self._model_jobs.submit(_runner)
            return True
        except RuntimeError:
            with self._lock:
                self._inflight.discard(key)
            self._model_slots.release()
            return False

    def _tail_event_cursor(self, realm_id: str) -> int:
        cursor = 0
        for _ in range(40):
            res = self._world.poll_events(after_seq=cursor, limit=500, realm_id=realm_id)
            evs = res.get("events") or ()
            nxt = int(res.get("next_seq") or cursor)
            if not evs or nxt <= cursor or len(evs) < 500:
                return max(cursor, nxt)
            cursor = nxt
        return cursor

    def _register_overlay_cb(self, session_id: str, realm_id: str) -> None:
        self._world.register_overlay_handler(
            realm_id,
            lambda req, sid=session_id: self._handle_overlay_action(sid, req),
        )

    def _push_overlay_locked(self, session: Mapping[str, Any], world_state: Mapping[str, Any] | None = None) -> None:
        if not self._world.available():
            return
        realm = str(session.get("realm_space_id") or "")
        if not realm:
            return
        try:
            self._world.set_overlay(realm, build_fate_overlay(session, world_state))
        except (RuntimeError, ValueError):
            logger.debug("推送命运编年史面板失败：%s", realm, exc_info=True)

    def restore_sessions(self, *, restore_world: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            self._sessions = self._store.load_all()
            self._observer_autoplay_state.clear()
            self._pending_observer_openings.clear()
            for sid, sess in self._sessions.items():
                if sess.get("mode") == "dialogue":
                    if sess.get("observer_autoplay") is not False or sess.get("observer_status_note"):
                        sess["observer_autoplay"] = False
                        sess["observer_status_note"] = ""
                        if restore_world:
                            self._store.save(sess)
                    continue
                if not restore_world or sess.get("status") != "active":
                    continue
                realm = str(sess.get("realm_space_id") or "")
                if realm and self._world.available():
                    self._register_overlay_cb(sid, realm)
                    self._push_overlay_locked(sess)
                    if self._sync_dialogue_focus_locked(sess):
                        self._store.save(sess)
                    if sess.get("experience_mode") == "observer":
                        roster = [
                            aid for aid, lg in (sess.get("ledgers") or {}).items()
                            if isinstance(lg, Mapping) and not lg.get("is_player")
                        ]
                        if roster:
                            try:
                                self._world.set_background_roster(realm, roster)
                            except Exception:
                                logger.debug("恢复观剧角色运行队列失败：%s", realm, exc_info=True)
            return [copy.deepcopy(s) for s in self._sessions.values()]

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._store.reload_if_changed(self._sessions)
            rows = sorted(
                self._sessions.values(),
                key=lambda s: float(s.get("updated_at") or s.get("created_at") or 0.0),
                reverse=True,
            )
            return [copy.deepcopy(r) for r in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._store.reload_if_changed(self._sessions)
            s = self._sessions.get(str(session_id or "").strip())
            return copy.deepcopy(s) if s is not None else None

    def get_session_for_realm(self, realm_space_id: str) -> dict[str, Any] | None:
        target = str(realm_space_id or "").strip()
        if not target:
            return None
        with self._lock:
            for sess in self._sessions.values():
                if sess.get("status") == "active" and sess.get("realm_space_id") == target:
                    return copy.deepcopy(sess)
            return None

    def list_chat_bindings(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._chat_bindings.reload_if_changed())

    def get_bound_dialogue_session(self, history_path: str) -> dict[str, Any] | None:
        path = canonical_history_path(history_path)
        with self._lock:
            binding = self._chat_bindings.reload_if_changed().get(path)
            session = self.get_session(binding["session_id"]) if binding else None
            if (session and session.get("mode") == "dialogue" and session.get("status") == "active"
                    and session.get("linked_history_path") == path):
                return session
        return None

    def _dialogue_session_locked(self, session_id: str, history_path: str = "") -> dict[str, Any]:
        self._store.reload_if_changed(self._sessions)
        session = self._sessions.get(session_id)
        if not session or session.get("mode") != "dialogue":
            raise ValueError("找不到对话剧本。")
        if history_path:
            path = canonical_history_path(history_path)
            binding = self._chat_bindings.reload_if_changed().get(path) or {}
            if session.get("linked_history_path") != path or binding.get("session_id") != session_id:
                raise ValueError("对话剧本的绑定已变更。")
        return session

    def _save_dialogue_locked(self, session: dict[str, Any]) -> None:
        session["updated_at"] = time.time()
        self._store.save(session)
        self._sessions[session["session_id"]] = session

    def _sync_dialogue_cast(self, session: dict[str, Any], characters: Sequence[str]) -> bool:
        names = dialogue_character_names(characters)
        if not names:
            return False
        changed = session.get("dialogue_cast") != names
        session["dialogue_cast"] = names
        for name in names:
            aid = f"character:{name.casefold()}"
            if aid not in session["ledgers"]:
                profile = self._persona.read_character_profile(name, custom_persona_dir=self._custom_persona_dir())
                session["ledgers"][aid] = make_character_ledger(
                    aid, name, host_character=name, persona_profile=profile,
                    story_role={"role_category": "participant", "local_role": "谈话的参与者"},
                )
                changed = True
        return changed

    def list_host_backgrounds(self) -> list[dict[str, Any]]:
        return self._background_adapter.list_backgrounds()

    def _dialogue_background_catalog(
        self, source: Mapping[str, Any], *, required: bool = False,
    ) -> dict[str, Any] | None:
        if source.get("background_mode") != "shinsekai":
            return None
        name = str(source.get("opening_background") or "")
        catalog = self._background_adapter.get_background(name) if name else None
        if catalog and catalog.get("scenes"):
            return catalog
        if required:
            if not name:
                raise ValueError("请选择 Shinsekai 内置背景组。")
            raise ValueError(f"Shinsekai 内置背景组「{name}」不存在或没有可用场景。")
        return None

    def _preview_dialogue_story(
        self, raw: Mapping[str, Any], spec: Mapping[str, Any], catalog: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not spec["characters"]:
            raise ValueError("请至少选择一位参与对话的角色。")
        recent = raw.get("recent_chat_turns")
        return preview_dialogue_opening(
            title=spec["title"], theme_compass=spec["theme_compass"], outline=spec["outline"],
            characters=spec["characters"], recent_chat_turns=recent if isinstance(recent, list) else (),
            custom_draft=spec["custom_draft"] or None, experience_mode=spec["experience_mode"],
            use_llm=self._effective_use_llm(raw.get("use_llm")), llm_fn=self._llm_fn,
            background_catalog=catalog, opening_scene_index=spec["opening_scene_index"],
            allow_scene_transitions=spec["allow_scene_transitions"],
        )

    def preview_dialogue_story(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        spec = normalize_dialogue_preset(raw)
        return self._preview_dialogue_story(raw, spec, self._dialogue_background_catalog(spec, required=True))

    def start_dialogue_story(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        spec = normalize_dialogue_preset(raw)
        exp_mode = spec["experience_mode"]
        catalog = self._dialogue_background_catalog(spec, required=True)
        if catalog:
            spec["opening_background"] = catalog["name"]
        draft = self._preview_dialogue_story(raw, spec, catalog)
        spec["custom_draft"] = draft
        player_id = "" if exp_mode == "observer" else "player"
        ledgers = {} if exp_mode == "observer" else {player_id: make_character_ledger(player_id, "玩家", is_player=True)}
        session = make_fate_session(
            mode="dialogue", title=spec["title"], experience_mode=exp_mode, player_actor_id=player_id,
            reveal_inner_thoughts=bool(raw.get("reveal_inner_thoughts", False)),
            ledgers=ledgers, scene_context=normalize_dialogue_background(spec),
        )
        preferred = exp_mode == "observer" and bool(raw.get("observer_autoplay", spec["observer_autoplay"]))
        spec["observer_autoplay"] = preferred
        session["dialogue_preset"] = spec
        session["observer_autoplay_preferred"] = preferred
        session["observer_autoplay"] = False
        session["observer_interval_sec"] = spec["observer_interval_sec"]
        session["observer_status_note"] = ""
        self._sync_dialogue_cast(session, spec["characters"])
        thread = weave_dialogue_opening(
            session, title=spec["title"], theme_compass=draft["theme_compass"], max_nodes=spec["max_nodes"],
            custom_draft=draft, experience_mode=exp_mode, use_llm=False,
            background_catalog=catalog, opening_scene_index=spec["opening_scene_index"],
            allow_scene_transitions=spec["allow_scene_transitions"],
        )
        self._add_dialogue_thread(session, thread, use_llm=self._effective_use_llm(raw.get("use_llm")))
        with self._lock, dialogue_write_lock(self._data_root):
            if raw.get("history_path"):
                self._bind_dialogue_locked(session, str(raw["history_path"]), str(raw.get("conversation_id") or ""))
                if exp_mode == "observer" and raw.get("observer_autoplay") and session["linked_history_path"]:
                    session["observer_autoplay"] = True
                    self._save_dialogue_locked(session)
                    self._active_chat_history_path = session["linked_history_path"]
                    self._observer_autoplay_state[session["session_id"]] = {
                        "pending_turn": None,
                        "last_dispatched_at": 0.0,
                        "last_turn_at": time.monotonic(),
                        "auto_turns": 0,
                        "max_auto_turns": OBSERVER_DEFAULT_MAX_AUTO_TURNS,
                        "submit_failures": 0,
                    }
            else:
                self._save_dialogue_locked(session)
            return copy.deepcopy(session)

    def _bind_dialogue_locked(self, session: dict[str, Any], history_path: str, conversation_id: str) -> None:
        path = canonical_history_path(history_path)
        sid = session["session_id"]
        bindings = dict(self._chat_bindings.reload_if_changed())
        other = bindings.get(path)
        if other and other.get("session_id") != sid:
            self._store.reload_if_changed(self._sessions)
            existing = self._sessions.get(other.get("session_id"))
            if existing and existing.get("linked_history_path") == path:
                raise ValueError("此对话已有绑定的剧本，请先解除原绑定。")
        original = self._sessions.get(sid)
        bindings = {key: row for key, row in bindings.items() if row.get("session_id") != sid}
        if path != session.get("linked_history_path", ""):
            for key in ("dialogue_history_cursor", "dialogue_checkpoint_base", "dialogue_turn_signatures"):
                session.pop(key, None)
            session["turn_checkpoints"] = []
            session["observer_autoplay"] = False
            session["observer_status_note"] = ""
            self._observer_autoplay_state.pop(sid, None)
            self._pending_observer_openings.pop(sid, None)
        session["linked_history_path"] = path
        session["linked_conversation_id"] = conversation_id if path else ""
        if path:
            bindings[path] = {
                "session_id": sid, "history_path": path, "conversation_id": conversation_id,
                "characters": session["dialogue_cast"], "updated_at": time.time(),
            }
        self._save_dialogue_locked(session)
        try:
            self._chat_bindings.replace(bindings)
        except OSError:
            if original:
                self._save_dialogue_locked({**original, "revision": session["revision"]})
            else:
                self._store.delete(sid)
                self._sessions.pop(sid, None)
            raise

    def bind_dialogue_session(
        self, session_id: str, history_path: str, *, conversation_id: str = "", characters: Sequence[str] = (),
    ) -> dict[str, Any]:
        with self._lock, dialogue_write_lock(self._data_root):
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
            self._sync_dialogue_cast(session, characters)
            self._bind_dialogue_locked(session, history_path, conversation_id)
            return copy.deepcopy(session)

    def toggle_dialogue_pause(self, session_id: str, paused: bool | None = None) -> dict[str, Any]:
        with self._lock, dialogue_write_lock(self._data_root):
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
            session["paused"] = not session.get("paused") if paused is None else bool(paused)
            if session["paused"]:
                session["observer_autoplay"] = False
                self._observer_autoplay_state.pop(session_id, None)
                self._pending_observer_openings.pop(session_id, None)
            self._save_dialogue_locked(session)
            return copy.deepcopy(session)

    def inject_director_whisper(
        self, session_id: str, text: str, *, target: str = "", duration_turns: int = 2,
    ) -> dict[str, Any]:
        direction = clip_text(text, 240)
        if not direction:
            raise ValueError("请输入导演耳语。")
        with self._lock, dialogue_write_lock(self._data_root):
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
            if target and target.casefold() not in {name.casefold() for name in session["dialogue_cast"]}:
                raise ValueError("导演耳语的对象不在当前对话中。")
            turn = session["turn_count"]
            whispers = [row for row in session["director_whispers"] if row["expires_after_turn"] > turn]
            whispers.append({"text": direction, "target": target, "expires_after_turn":
                             turn + max(1, min(24, int(duration_turns)))})
            session["director_whispers"] = whispers[-12:]
            self._save_dialogue_locked(session)
            return copy.deepcopy(session)

    def _add_dialogue_thread(self, session: dict[str, Any], thread: dict[str, Any], *, use_llm: bool) -> None:
        session["threads"].append(thread)
        for aid, role in thread["resident_roles"].items():
            session["ledgers"][aid]["story_role"].update(role)
        self._prepare_dialogue_node(session, thread, thread["nodes"][0], use_llm=use_llm)

    def _prepare_dialogue_node(self, session: dict[str, Any], thread: dict[str, Any], node: dict[str, Any],
                               *, use_llm: bool) -> None:
        scene_context = session["scene_context"]
        scene_context.update(scene_name=node["scene_name"], atmosphere=node["atmosphere"])
        for key in BACKGROUND_SCENE_KEYS:
            if key in node:
                scene_context[key] = node[key]
            else:
                scene_context.pop(key, None)
        for name in session["dialogue_cast"]:
            aid = f"character:{name.casefold()}"
            choice = decide_dialogue_actor_choice(
                session, thread, node, aid, self._persona, use_llm=use_llm, llm_fn=self._llm_fn,
                custom_persona_dir=self._custom_persona_dir(),
            )
            node["participant_choices"][aid] = choice
            apply_choice_to_ledger(session["ledgers"][aid], choice)

    def pluck_dialogue_thread(
        self, session_id: str, *, theme_compass: str = "auto", prompt_hint: str = "", use_llm: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
        if active_dialogue_beat(session)[0]:
            raise ValueError("请先收束当前对话篇章。")
        latest = session["threads"][-1]
        spec = session["dialogue_preset"]
        scene_context = session.get("scene_context") or {}
        title = f"{session['title'][:46]} · {len(session['threads']) + 1}"
        current_cast = {f"character:{name.casefold()}" for name in session["dialogue_cast"]}
        recent = [
            {"text": f"{line['character_name']}：{line['speech']}", "role": "assistant"}
            for node in latest["nodes"] for line in node["recent_dialogue_lines"]
            if current_cast.issubset(line["audience"])
        ][-8:]
        eff_llm = self._effective_use_llm(use_llm)
        thread = weave_dialogue_opening(
            session, title=title, theme_compass=latest["theme_compass"] if theme_compass == "auto" else theme_compass,
            max_nodes=spec["max_nodes"], outline=prompt_hint, recent_chat_turns=recent, use_llm=eff_llm, llm_fn=self._llm_fn,
            background_catalog=self._dialogue_background_catalog(scene_context),
            opening_scene_index=scene_context.get("opening_scene_index", 0),
            allow_scene_transitions=scene_context.get("allow_scene_transitions", True),
        )
        for aid, role in thread["resident_roles"].items():
            prior = latest["resident_roles"].get(aid, {})
            if role.get("local_role") == "谈话的参与者" and prior.get("local_role"):
                role["local_role"] = prior["local_role"]
            if not role.get("relationship_to_incident") and prior.get("relationship_to_incident"):
                role["relationship_to_incident"] = prior["relationship_to_incident"]
        self._add_dialogue_thread(session, thread, use_llm=eff_llm)
        with self._lock, dialogue_write_lock(self._data_root):
            live = self._dialogue_session_locked(session_id)
            if live["revision"] != session["revision"]:
                raise ValueError("剧本状态已更新，请再试一次。")
            self._refresh_dialogue_checkpoint(session)
            self._save_dialogue_locked(session)
            return {"session": copy.deepcopy(session), "thread": copy.deepcopy(thread)}

    _DIALOGUE_CHECKPOINT_FIELDS = (
        "threads", "ledgers", "chronicle_archive", "scene_context", "turn_count", "dialogue_turn_signatures",
    )

    def _dialogue_checkpoint(self, session: Mapping[str, Any], cursor: Mapping[str, Any]) -> dict[str, Any]:
        return {**cursor, "state": {key: copy.deepcopy(session[key]) for key in self._DIALOGUE_CHECKPOINT_FIELDS
                                    if key in session}}

    def _refresh_dialogue_checkpoint(self, session: dict[str, Any]) -> None:
        cursor = session.get("dialogue_history_cursor")
        if cursor is None:
            return
        checkpoint = self._dialogue_checkpoint(session, cursor)
        previous = [row for row in session["turn_checkpoints"] if not all(row.get(k) == v for k, v in cursor.items())]
        session["turn_checkpoints"] = (previous + [checkpoint])[-24:]

    def _restore_dialogue_prefix(self, session: dict[str, Any], prefixes: Sequence[str]) -> bool:
        if not prefixes:
            return False

        def matches(row: Mapping[str, Any]) -> bool:
            count = row["user_turn_count"]
            return 0 <= count < len(prefixes) and prefixes[count] == row["user_history_fingerprint"]

        cursor = session.get("dialogue_history_cursor")
        if cursor is None:
            index = len(prefixes) - 1
            cursor = {"user_turn_count": index, "user_history_fingerprint": prefixes[index]}
            session["dialogue_history_cursor"] = cursor
            session["dialogue_checkpoint_base"] = self._dialogue_checkpoint(session, cursor)
            return True
        base = session["dialogue_checkpoint_base"]
        checkpoint = max(
            (row for row in reversed(session["turn_checkpoints"]) if matches(row)),
            key=lambda row: row["user_turn_count"], default=base,
        )
        if matches(cursor) and checkpoint["user_turn_count"] <= cursor["user_turn_count"]:
            return False
        for key in self._DIALOGUE_CHECKPOINT_FIELDS:
            session.pop(key, None)
        session.update(copy.deepcopy(checkpoint["state"]))
        # Keep session-wide background settings when revisiting an older chat branch.
        background = normalize_dialogue_background(session.get("dialogue_preset") or {})
        if normalize_dialogue_background(session.get("scene_context") or {}) != background:
            self._apply_dialogue_background(session, background, self._dialogue_background_catalog(background))
        index = checkpoint["user_turn_count"] if matches(checkpoint) else len(prefixes) - 1
        session["dialogue_history_cursor"] = {
            "user_turn_count": index, "user_history_fingerprint": prefixes[index],
        }
        return True

    def sync_dialogue_checkpoint(
        self, session_id: str, *, user_history_prefixes: Sequence[str] = (),
        characters: Sequence[str] = (), history_path: str = "",
        expected_history_cursor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock, dialogue_write_lock(self._data_root):
            session = copy.deepcopy(self._dialogue_session_locked(session_id, history_path))
            if session["paused"] or (expected_history_cursor is not None
                                     and expected_history_cursor != (session.get("dialogue_history_cursor") or {})):
                return session
            changed = self._restore_dialogue_prefix(session, user_history_prefixes)
            if self._sync_dialogue_cast(session, characters) or changed:
                self._save_dialogue_locked(session)
            return session

    def ingest_host_dialogue_turn(
        self, session_id: str, *, user_text: str, assistant_utterances: Sequence[Mapping[str, Any]],
        present_characters: Sequence[str], user_history_prefixes: Sequence[str] = (),
        turn_signature: str = "", history_path: str = "", use_llm: bool | None = None,
        sync_models: bool = False, expected_history_cursor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock, dialogue_write_lock(self._data_root):
            if history_path:
                self._active_chat_history_path = canonical_history_path(history_path)
            session = copy.deepcopy(self._dialogue_session_locked(session_id, history_path))
            if session["paused"] or (turn_signature and turn_signature in session.get("dialogue_turn_signatures", ())):
                return session
            if expected_history_cursor is not None and expected_history_cursor != (session.get("dialogue_history_cursor") or {}):
                # Delayed Bridge requests must not replay over a newer reply or fork.
                return session
            if user_history_prefixes:
                index = len(user_history_prefixes) - 1
                cursor = {"user_turn_count": index, "user_history_fingerprint": user_history_prefixes[index]}
                if (session.get("dialogue_history_cursor") == cursor and index
                        and any(all(row.get(k) == v for k, v in cursor.items()) for row in session["turn_checkpoints"])):
                    return session
                self._restore_dialogue_prefix(session, user_history_prefixes[:-1] if user_text else user_history_prefixes)
                session["dialogue_history_cursor"] = cursor
            self._sync_dialogue_cast(session, present_characters)
            lines = ingest_dialogue_turn(
                session, user_text=user_text, assistant_utterances=assistant_utterances,
                present_characters=present_characters, turn_index=session["turn_count"] + 1,
            )
            if not lines:
                return copy.deepcopy(self._sessions[session_id])
            if turn_signature:
                session["dialogue_turn_signatures"] = (session.get("dialogue_turn_signatures", []) + [turn_signature])[-48:]
            if session_id in self._observer_autoplay_state:
                tracker = self._observer_autoplay_state[session_id]
                tracker["pending_turn"] = None
                tracker["last_turn_at"] = time.monotonic()
                tracker["submit_failures"] = 0
            self._pending_observer_openings.pop(session_id, None)
            self._refresh_dialogue_checkpoint(session)
            self._save_dialogue_locked(session)
        return self.advance_dialogue_beat(session_id, use_llm=use_llm, sync_models=sync_models, force_evaluate=False)

    def _evaluate_dialogue_snapshot(self, session_id: str, *, force_evaluate: bool, use_llm: bool) -> None:
        with self._lock:
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
        thread, node = active_dialogue_beat(session)
        if not node or (session["paused"] and not force_evaluate):
            return
        result = evaluate_and_weave_dialogue_beat(
            session, force_evaluate=force_evaluate, use_llm=use_llm, llm_fn=self._llm_fn,
            background_catalog=self._dialogue_background_catalog(session.get("scene_context") or {}),
        )
        facts = list(dict.fromkeys(node["revealed_facts"] + result.get("revealed_facts", [])))[-24:]
        note = result.get("player_stance_note")
        action = result["action"]
        if action == "continue_scene" and facts == node["revealed_facts"] and not note:
            return
        node["revealed_facts"] = facts
        if note:
            node["player_stance"] = {"note": note, "stance_label": "对话表态"}
        if action != "continue_scene":
            node.update(status="resolved", outcome_summary=result["outcome_summary"])
        if action == "next_node":
            next_node = result["next_node"]
            thread["nodes"].append(next_node)
            thread.update(active_node_id=next_node["node_id"], phase=next_node["phase"])
            self._prepare_dialogue_node(session, thread, next_node, use_llm=use_llm)
        elif action == "resolve":
            chapter = result["chapter_chronicle"]
            thread.update(status="resolved", phase="afterglow", active_node_id="", chapter_chronicle=chapter)
            session["chronicle_archive"].append({**chapter, "thread_id": thread["thread_id"]})
            if session.get("observer_autoplay"):
                session["observer_autoplay"] = False
                session["observer_status_note"] = "当前篇章已落幕，自动观剧已停止"
                self._observer_autoplay_state.pop(session_id, None)
        with self._lock, dialogue_write_lock(self._data_root):
            live = self._dialogue_session_locked(session_id)
            if live["revision"] == session["revision"]:
                self._refresh_dialogue_checkpoint(session)
                self._save_dialogue_locked(session)

    def advance_dialogue_beat(
        self, session_id: str, *, force_evaluate: bool = True, use_llm: bool | None = None,
        sync_models: bool = True,
    ) -> dict[str, Any]:
        eff_llm = self._effective_use_llm(use_llm)
        if sync_models:
            self._evaluate_dialogue_snapshot(session_id, force_evaluate=force_evaluate, use_llm=eff_llm)
        else:
            key = ("host_dialogue", session_id)
            with self._lock:
                if self._closed:
                    return copy.deepcopy(self._dialogue_session_locked(session_id))
                previous = self._pending_dialogue.get(session_id)
                self._pending_dialogue[session_id] = (force_evaluate or bool(previous and previous[0]), eff_llm)
                if key not in self._inflight:
                    self._inflight.add(key)
                    self._model_jobs.submit(self._drain_dialogue_jobs, key, session_id)
        return self.get_session(session_id)

    def _drain_dialogue_jobs(self, key: tuple[str, ...], session_id: str) -> None:
        while True:
            with self._lock:
                job = self._pending_dialogue.pop(session_id, None)
                if self._closed or job is None:
                    self._inflight.discard(key)
                    return
            try:
                self._evaluate_dialogue_snapshot(session_id, force_evaluate=job[0], use_llm=job[1])
            except Exception:
                logger.debug("对话剧本推演失败：%s", session_id, exc_info=True)

    def is_dialogue_evaluating(self, session_id: str) -> bool:
        with self._lock:
            return ("host_dialogue", session_id) in self._inflight or session_id in self._pending_dialogue

    def report_active_chat(self, history_path: str, *, ready: bool = False) -> None:
        clean = canonical_history_path(history_path)
        with self._lock, dialogue_write_lock(self._data_root):
            self._active_chat_history_path = clean
            self._store.reload_if_changed(self._sessions)
            for pending_sid, pending_path in list(self._pending_observer_openings.items()):
                if pending_path != clean:
                    self._pending_observer_openings.pop(pending_sid, None)
            now = time.monotonic()
            for sid, sess in list(self._sessions.items()):
                if sess.get("mode") != "dialogue":
                    continue
                linked = sess.get("linked_history_path")
                if sess.get("observer_autoplay") and linked != clean:
                    updated = copy.deepcopy(sess)
                    updated["observer_autoplay"] = False
                    updated["observer_status_note"] = "检测到已切换对话，自动观剧已停止"
                    self._observer_autoplay_state.pop(sid, None)
                    self._save_dialogue_locked(updated)
                elif (ready and clean and linked == clean and sess.get("status") == "active"
                      and sess.get("experience_mode") == "observer" and not sess.get("paused")
                      and active_dialogue_beat(sess)[1]):
                    if "observer_autoplay_preferred" in sess:
                        preferred = bool(sess["observer_autoplay_preferred"])
                    else:
                        preferred = (sess.get("dialogue_preset") or {}).get("observer_autoplay") is not False
                    tracker = self._observer_autoplay_state.get(sid) or {}
                    pending = tracker.get("pending_turn")
                    last_dispatched = float(tracker.get("last_dispatched_at") or 0.0)
                    recently_dispatched = pending is not None and now - last_dispatched <= OBSERVER_PENDING_TIMEOUT_SEC
                    if preferred:
                        self._pending_observer_openings.pop(sid, None)
                        if not sess.get("observer_autoplay") or sess.get("observer_status_note"):
                            self._save_dialogue_locked({**sess, "observer_autoplay": True, "observer_status_note": ""})
                        if not recently_dispatched and (
                            not sess.get("observer_autoplay") or not tracker or pending is not None
                        ):
                            tracker.update(
                                pending_turn=None,
                                last_dispatched_at=0.0,
                                last_turn_at=now - _clamp_observer_interval(sess.get("observer_interval_sec")),
                                auto_turns=0,
                                max_auto_turns=_normalize_observer_max_turns(None),
                                submit_failures=0,
                            )
                            self._observer_autoplay_state[sid] = tracker
                    elif int(sess.get("turn_count") or 0) == 0 and not recently_dispatched:
                        self._pending_observer_openings[sid] = clean

    @staticmethod
    def _apply_dialogue_background(
        session: dict[str, Any], background: Mapping[str, Any], catalog: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        scene_context = session.setdefault("scene_context", {})
        scene_context.update(background)
        _, node = active_dialogue_beat(session)
        for key in BACKGROUND_SCENE_KEYS:
            scene_context.pop(key, None)
            if node is not None:
                node.pop(key, None)
        if not node or not catalog:
            return {}
        scenes = catalog["scenes"]
        scene = resolve_catalog_scene(
            scenes, scene_index=background["opening_scene_index"], scene_name=node.get("scene_name", ""),
        ) or scenes[0]
        fields = background_scene_fields(catalog["name"], scene)
        node.update(fields)
        scene_context.update(fields)
        return fields

    def set_dialogue_background(
        self, session_id: str, *, background_mode: str = "custom", opening_background: str = "",
        opening_scene_index: int = 0, allow_scene_transitions: bool = True,
    ) -> dict[str, Any]:
        mode = str(background_mode or "custom").strip().lower()
        if mode not in {"custom", "shinsekai"}:
            raise ValueError("无效的对话剧本背景模式。")
        background = normalize_dialogue_background({
            "background_mode": mode, "opening_background": opening_background,
            "opening_scene_index": opening_scene_index, "allow_scene_transitions": allow_scene_transitions,
        })
        catalog = self._dialogue_background_catalog(background, required=True)
        if catalog:
            background["opening_background"] = catalog["name"]
        with self._lock, dialogue_write_lock(self._data_root):
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
            fields = self._apply_dialogue_background(session, background, catalog)
            preset = session.setdefault("dialogue_preset", {})
            preset.update(background)
            draft = preset.get("custom_draft")
            if isinstance(draft, dict):
                for key in BACKGROUND_SCENE_KEYS:
                    draft.pop(key, None)
                draft.update(fields)
            self._refresh_dialogue_checkpoint(session)
            self._save_dialogue_locked(session)
            return copy.deepcopy(session)

    def set_dialogue_experience_mode(self, session_id: str, experience_mode: str) -> dict[str, Any]:
        target = str(experience_mode or "co_play").strip().lower()
        if target not in VALID_EXPERIENCE_MODES:
            raise ValueError("无效的对话剧本体验模式。")
        with self._lock, dialogue_write_lock(self._data_root):
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
            session["experience_mode"] = target
            if isinstance(session.get("dialogue_preset"), dict):
                session["dialogue_preset"]["experience_mode"] = target
            if target == "observer":
                session["player_actor_id"] = ""
                session.get("ledgers", {}).pop("player", None)
                session["observer_interval_sec"] = _clamp_observer_interval(session.get("observer_interval_sec"))
            else:
                session["player_actor_id"] = "player"
                ledgers = session.setdefault("ledgers", {})
                if "player" not in ledgers:
                    ledgers["player"] = make_character_ledger("player", "玩家", is_player=True)
                session["observer_autoplay"] = False
                session["observer_autoplay_preferred"] = False
                if isinstance(session.get("dialogue_preset"), dict):
                    session["dialogue_preset"]["observer_autoplay"] = False
                session["observer_status_note"] = ""
                self._observer_autoplay_state.pop(session_id, None)
                self._pending_observer_openings.pop(session_id, None)
            self._save_dialogue_locked(session)
            return copy.deepcopy(session)

    def set_dialogue_observer_autoplay(
        self,
        session_id: str,
        *,
        enabled: bool,
        interval_sec: float | None = None,
        history_path: str = "",
        max_auto_turns: int | None = OBSERVER_DEFAULT_MAX_AUTO_TURNS,
        persist_preference: bool = False,
    ) -> dict[str, Any]:
        with self._lock, dialogue_write_lock(self._data_root):
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
            if interval_sec is not None:
                clamped = _clamp_observer_interval(interval_sec)
                session["observer_interval_sec"] = clamped
                if isinstance(session.get("dialogue_preset"), dict):
                    session["dialogue_preset"]["observer_interval_sec"] = clamped
            if enabled:
                if session.get("experience_mode") != "observer":
                    raise ValueError("请先将此剧本切换为观剧模式。")
                if not session.get("linked_history_path"):
                    raise ValueError("请先将此剧本绑定到对话。")
                if session.get("paused"):
                    raise ValueError("剧本注入已暂停，请先恢复。")
                if not active_dialogue_beat(session)[1]:
                    raise ValueError("当前篇章已落幕，请先拨动新丝线。")
                session["observer_autoplay"] = True
                session["observer_status_note"] = ""
                clean = canonical_history_path(history_path) or session["linked_history_path"]
                self._active_chat_history_path = clean
                self._observer_autoplay_state[session_id] = {
                    "pending_turn": None,
                    "last_dispatched_at": 0.0,
                    "last_turn_at": time.monotonic(),
                    "auto_turns": 0,
                    "max_auto_turns": _normalize_observer_max_turns(max_auto_turns),
                    "submit_failures": 0,
                }
            else:
                session["observer_autoplay"] = False
                session["observer_status_note"] = ""
                self._observer_autoplay_state.pop(session_id, None)
                self._pending_observer_openings.pop(session_id, None)
            if persist_preference:
                session["observer_autoplay_preferred"] = bool(enabled)
                if isinstance(session.get("dialogue_preset"), dict):
                    session["dialogue_preset"]["observer_autoplay"] = bool(enabled)
            self._save_dialogue_locked(session)
            return copy.deepcopy(session)

    def mark_observer_cue_dispatched(
        self,
        session_id: str,
        turn_count: int,
        history_path: str = "",
        *,
        count_as_auto: bool = False,
        now: float | None = None,
    ) -> None:
        ts = time.monotonic() if now is None else float(now)
        clean = canonical_history_path(history_path)
        with self._lock:
            self._pending_observer_openings.pop(session_id, None)
            if clean:
                self._active_chat_history_path = clean
            tracker = self._observer_autoplay_state.setdefault(
                session_id,
                {
                    "pending_turn": None,
                    "last_dispatched_at": 0.0,
                    "last_turn_at": ts,
                    "auto_turns": 0,
                    "max_auto_turns": OBSERVER_DEFAULT_MAX_AUTO_TURNS,
                    "submit_failures": 0,
                },
            )
            tracker["pending_turn"] = int(turn_count)
            tracker["last_dispatched_at"] = ts
            tracker["submit_failures"] = 0
            if count_as_auto:
                tracker["auto_turns"] = int(tracker.get("auto_turns") or 0) + 1

    def _stop_observer_autoplay_locked(self, session_id: str, note: str) -> None:
        try:
            session = copy.deepcopy(self._dialogue_session_locked(session_id))
        except ValueError:
            self._observer_autoplay_state.pop(session_id, None)
            return
        session["observer_autoplay"] = False
        session["observer_status_note"] = note
        self._observer_autoplay_state.pop(session_id, None)
        self._save_dialogue_locked(session)

    def tick_observer_autoplay(self, host_chat_adapter: Any, *, now: float | None = None) -> list[str]:
        if host_chat_adapter is None or not host_chat_adapter.available():
            return []
        ts = time.monotonic() if now is None else float(now)
        dispatched: list[str] = []
        candidates: list[tuple[str, str, int, str, bool]] = []

        with self._lock:
            if self._closed:
                return []
            self._store.reload_if_changed(self._sessions)
            for sid, sess in list(self._sessions.items()):
                if (sess.get("status") != "active" or sess.get("mode") != "dialogue"
                        or sess.get("experience_mode") != "observer"):
                    self._pending_observer_openings.pop(sid, None)
                    continue
                if sess.get("paused") or not sess.get("linked_history_path"):
                    self._pending_observer_openings.pop(sid, None)
                    if sess.get("observer_autoplay"):
                        with dialogue_write_lock(self._data_root):
                            self._stop_observer_autoplay_locked(sid, "")
                    continue
                if not active_dialogue_beat(sess)[1]:
                    self._pending_observer_openings.pop(sid, None)
                    if sess.get("observer_autoplay"):
                        with dialogue_write_lock(self._data_root):
                            self._stop_observer_autoplay_locked(sid, "当前篇章已落幕，自动观剧已停止")
                    continue
                if (self._active_chat_history_path
                        and self._active_chat_history_path != sess.get("linked_history_path")):
                    self._pending_observer_openings.pop(sid, None)
                    if sess.get("observer_autoplay"):
                        with dialogue_write_lock(self._data_root):
                            self._stop_observer_autoplay_locked(sid, "检测到已切换对话，自动观剧已停止")
                    continue
                if ("host_dialogue", sid) in self._inflight or sid in self._pending_dialogue:
                    continue
                turn_count = int(sess.get("turn_count") or 0)
                if not sess.get("observer_autoplay"):
                    pending_path = self._pending_observer_openings.get(sid)
                    if (pending_path and turn_count == 0
                            and pending_path == sess.get("linked_history_path")
                            and (not self._active_chat_history_path or self._active_chat_history_path == pending_path)):
                        tracker = self._observer_autoplay_state.get(sid)
                        if (tracker is not None and tracker.get("pending_turn") is not None
                                and ts - float(tracker.get("last_dispatched_at") or 0.0) <= OBSERVER_PENDING_TIMEOUT_SEC):
                            continue
                        candidates.append((
                            sid,
                            dialogue_stage_cue(sess, is_opening=True),
                            turn_count,
                            sess.get("linked_history_path", ""),
                            True,
                        ))
                    elif pending_path is not None:
                        self._pending_observer_openings.pop(sid, None)
                    continue
                tracker = self._observer_autoplay_state.get(sid)
                if tracker is None:
                    tracker = {
                        "pending_turn": None,
                        "last_dispatched_at": 0.0,
                        "last_turn_at": ts,
                        "auto_turns": 0,
                        "max_auto_turns": OBSERVER_DEFAULT_MAX_AUTO_TURNS,
                        "submit_failures": 0,
                    }
                    self._observer_autoplay_state[sid] = tracker
                pending = tracker.get("pending_turn")
                if pending is not None:
                    if turn_count > int(pending):
                        tracker["pending_turn"] = None
                        tracker["submit_failures"] = 0
                    elif ts - float(tracker.get("last_dispatched_at") or 0.0) > OBSERVER_PENDING_TIMEOUT_SEC:
                        with dialogue_write_lock(self._data_root):
                            self._stop_observer_autoplay_locked(sid, "等待回复超时，自动观剧已暂停")
                        continue
                    else:
                        continue
                raw_max = tracker.get("max_auto_turns")
                max_turns = OBSERVER_DEFAULT_MAX_AUTO_TURNS if raw_max is None else int(raw_max)
                if max_turns > 0 and int(tracker.get("auto_turns") or 0) >= max_turns:
                    with dialogue_write_lock(self._data_root):
                        self._stop_observer_autoplay_locked(sid, f"已连续自动续演 {max_turns} 轮，可随时继续观剧")
                    continue
                interval = _clamp_observer_interval(sess.get("observer_interval_sec"))
                if ts - float(tracker.get("last_turn_at") or 0.0) < interval:
                    continue
                candidates.append((sid, dialogue_stage_cue(sess), turn_count, sess.get("linked_history_path", ""), False))

        for sid, cue, turn_count, linked_path, is_one_shot_opening in candidates:
            ok = False
            try:
                ok = bool(host_chat_adapter.submit_text(cue))
            except Exception:
                logger.debug("自动观剧推送续演信号失败：%s", sid, exc_info=True)
                ok = False
            if ok:
                self.mark_observer_cue_dispatched(
                    sid,
                    turn_count,
                    linked_path,
                    count_as_auto=not is_one_shot_opening,
                    now=ts,
                )
                dispatched.append(sid)
            elif is_one_shot_opening:
                with self._lock:
                    self._pending_observer_openings.pop(sid, None)
            else:
                with self._lock:
                    tracker = self._observer_autoplay_state.get(sid)
                    if tracker is None or not self._sessions.get(sid, {}).get("observer_autoplay"):
                        continue
                    tracker["submit_failures"] = int(tracker.get("submit_failures") or 0) + 1
                    tracker["last_turn_at"] = ts
                    if tracker["submit_failures"] < OBSERVER_MAX_SUBMIT_FAILURES:
                        continue
                    note = "当前对话连续多次未响应，自动观剧已暂停"
                    if self._sessions.get(sid, {}).get("observer_autoplay_preferred"):
                        note += "（重新打开对话后将自动继续）"
                    with dialogue_write_lock(self._data_root):
                        self._stop_observer_autoplay_locked(sid, note)
        return dispatched

    def attach_world(
        self,
        realm_space_id: str = "",
        *,
        auto_pluck: bool = True,
        theme_compass: str = "auto",
        prompt_hint: str = "",
        title: str = "",
        experience_mode: str = "co_play",
        world_mode: str = "town",
        player_actor_id: str = "",
        owned_actor_ids: Sequence[str] | None = None,
        max_nodes: int = 0,
        preset_ledgers: Mapping[str, Mapping[str, Any]] | None = None,
        reveal_inner_thoughts: bool = False,
        mode: str = "attached",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """将命运丝线挂载到一个现有常驻小镇或新建独立舞台上。"""
        if not self._world.available():
            raise RuntimeError("文字世界引擎尚未就绪，无法挂载命运丝线。")

        ws = self._world.query_world_state(str(realm_space_id or "").strip())
        realm_sid = str(ws.get("realm_space_id") or ws.get("active_space_id") or "").strip()
        if not realm_sid:
            raise ValueError("未能解析有效的小镇域 ID。")
        realm_sp = next(
            (sp for sp in (ws.get("spaces") or ()) if sp.get("space_id") == realm_sid),
            ws.get("space") or {},
        )
        realm_name = str(realm_sp.get("name") or realm_sid)
        eff_player_id = str(player_actor_id or "")
        if not eff_player_id and experience_mode != "observer":
            eff_player_id = next(
                (str(a.get("actor_id") or "") for a in (ws.get("actors") or ()) if a.get("is_player")),
                "",
            )

        with self._lock:
            for existing in self._sessions.values():
                if existing.get("status") == "active" and existing.get("realm_space_id") == realm_sid:
                    raise ValueError(f"小镇「{realm_name}」已挂载命运丝线，请直接在现有会话中拨动丝线。")

            cursor = self._tail_event_cursor(realm_sid)
            start_min = float(ws.get("minutes") or 0.0)
            session = make_fate_session(
                mode=mode,
                title=title or realm_name,
                experience_mode=experience_mode,
                world_mode=world_mode,
                realm_space_id=realm_sid,
                realm_name=realm_name,
                player_actor_id=eff_player_id,
                owned_actor_ids=owned_actor_ids,
                reveal_inner_thoughts=reveal_inner_thoughts,
                event_cursor=cursor,
                started_at_min=start_min,
                ledgers=preset_ledgers,
            )
            sync_cast_ledgers(session, ws, self._persona, custom_persona_dir=self._custom_persona_dir())
            sid = session["session_id"]
            self._sessions[sid] = session
            self._register_overlay_cb(sid, realm_sid)
            if not auto_pluck:
                self._push_overlay_locked(session, ws)
                self._store.save(session)
                return copy.deepcopy(session)
            sess_snap = copy.deepcopy(session)

        eff_llm = self._effective_use_llm(use_llm)
        cast_rels = self._query_cast_world_relations(realm_sid, sess_snap.get("ledgers") or {})
        try:
            thread = weave_opening_thread(
                sess_snap,
                ws,
                self._persona,
                theme_compass=theme_compass,
                prompt_hint=prompt_hint,
                custom_title=title,
                max_nodes=max_nodes,
                cast_relations=cast_rels,
                llm_fn=self._llm_fn,
                use_llm=eff_llm,
            )
        except Exception:
            with self._lock:
                self._sessions.pop(sid, None)
            self._world.unregister_overlay_handler(realm_sid)
            raise

        with self._lock:
            live = self._sessions.get(sid)
            if live is None:
                raise RuntimeError(f"命运会话初始化中断：{sid}")
            self._commit_thread_locked(live, thread, ws)
            return copy.deepcopy(live)

    def start_standalone_stage(self, raw_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """新建由「命运丝线」驱动的故事舞台（室内多层分散落位，零地图中心散落线索）。"""
        if not self._world.available():
            raise RuntimeError("文字世界引擎尚未就绪。")
        cfg = dict(raw_config or {})
        world_mode = str(cfg.get("world_mode") or "town").strip().lower()
        if world_mode not in ("town", "compound", "room"):
            world_mode = "town"
        exp_mode = str(cfg.get("experience_mode") or "co_play").strip().lower()
        if exp_mode not in VALID_EXPERIENCE_MODES:
            exp_mode = "co_play"

        theme = str(cfg.get("theme_compass") or cfg.get("genre") or "auto").strip().lower()
        story_title = clip_text(cfg.get("title"), 48, "")
        w_name = clip_text(cfg.get("world_name") or story_title, 32, "微风多层小镇" if world_mode == "town" else "山岚公馆")
        raw_wp = clip_text(cfg.get("world_prompt"), 140, "")
        raw_out = clip_text(cfg.get("outline") or cfg.get("prompt_hint"), 160, "")
        prompt_parts = [p for p in (story_title, raw_wp, raw_out) if p]
        prompt = clip_text("；".join(dict.fromkeys(prompt_parts)), 260, "带有二楼窗景房间、地下室与庭院的宁静多空间小镇")
        inferred_hour, inferred_weather = _infer_stage_atmosphere(story_title, raw_wp, raw_out)
        eff_hour = float(cfg["initial_hour"]) if cfg.get("initial_hour") is not None else inferred_hour
        max_nodes = normalize_max_nodes(cfg.get("max_nodes"), 0)
        use_llm = self._effective_use_llm(cfg.get("use_llm") if "use_llm" in cfg else None)

        raw_imported = cfg.get("imported_characters") if "imported_characters" in cfg else cfg.get("characters")
        imported_list = [str(c).strip() for c in (raw_imported or ()) if str(c).strip()]
        if "native_npc_count" in cfg and cfg.get("native_npc_count") is not None:
            native_count = max(0, min(4, int(cfg["native_npc_count"])))
        else:
            native_count = 2 if not imported_list else 1
        if not imported_list and native_count <= 0:
            native_count = 2

        res = self._world.create_world(
            mode=world_mode,
            name=w_name,
            prompt_text=prompt,
            building_count=max(3, min(8, int(cfg.get("building_count") or 5))),
            initial_hour=eff_hour,
            resident_count=len(imported_list) + native_count,
            opaque_minds=False,
            isolated_persona=True,
            switch_active=bool(cfg.get("switch_active", False)),
            use_llm=use_llm,
        )
        realm_id = str(res["space_id"])

        owned_actor_ids: list[str] = []
        player_actor_id = ""
        try:
            if inferred_weather:
                try:
                    self._world.set_world_environment(realm_id, weather_mode=inferred_weather)
                except Exception:
                    logger.debug("预设舞台初始天气失败：%s", realm_id, exc_info=True)

            ws_init = self._world.query_world_state(realm_id)
            spaces_by_id = {
                str(sp["space_id"]): sp
                for sp in (ws_init.get("spaces") or ())
                if isinstance(sp, Mapping) and sp.get("space_id")
            }
            interior_objs = [
                sp for sp in (ws_init.get("spaces") or ())
                if isinstance(sp, Mapping) and sp.get("space_id") != realm_id and sp.get("space_kind") != "town"
            ]

            # 为原生常住居民分配彼此独立的主建筑作为住处/守望据点（优先各栋 1F 主室内）
            by_bldg: dict[str, dict[str, Any]] = {}
            extra_interiors: list[dict[str, Any]] = []
            sorted_for_native = sorted(
                interior_objs,
                key=lambda s: (0 if int(s.get("floor_index") or 0) == 0 else 1, str(s.get("space_id") or "")),
            )
            for sp in sorted_for_native:
                bg = str(sp.get("building_group_id") or sp.get("space_id") or "")
                if bg not in by_bldg:
                    by_bldg[bg] = dict(sp)
                else:
                    extra_interiors.append(dict(sp))
            native_homes = list(by_bldg.values()) + extra_interiors
            if not native_homes:
                native_homes = [dict(spaces_by_id.get(realm_id) or {"space_id": realm_id, "name": w_name})]

            # 为外来暂留角色优先分配旅馆/咖啡/客房等非镇中心空间
            visitor_spawns = sorted(
                [str(sp["space_id"]) for sp in interior_objs],
                key=lambda sid: (
                    0 if any(k in str((spaces_by_id.get(sid) or {}).get("name") or "") for k in ("旅馆", "客房", "宿舍", "咖啡", "茶")) else 1,
                    0 if int((spaces_by_id.get(sid) or {}).get("floor_index") or 0) != 0 else 1,
                    sid,
                ),
            ) or [realm_id]

            preset_ledgers: dict[str, dict[str, Any]] = {}
            used_names: set[str] = set()
            imported_pairs: list[tuple[str, str]] = []
            native_pairs: list[tuple[str, str]] = []

            is_mystery_or_crisis = theme in ("mystery_ripple", "crisis_test") or bool(
                re.search(r"(?:杀人|凶|尸体|命案|谋杀|嫌疑|失踪|诡|谜|探查|暴雨|停电|风暴|火灾|危机|逃生|被困|案)", f"{story_title} {raw_wp} {raw_out}")
            )
            for idx, cname in enumerate(imported_list):
                if cname in used_names:
                    continue
                used_names.add(cname)
                target_sid = visitor_spawns[idx % len(visitor_spawns)]
                target_sp = spaces_by_id.get(target_sid) or {}
                home_name = str(target_sp.get("name") or "")
                prof = self._persona.read_character_profile(cname, custom_persona_dir=self._custom_persona_dir())
                up = self._world.upsert_actor(
                    name=cname,
                    host_character=cname,
                    actor_id=f"{realm_id}__imp_{idx}"[:48],
                    space_id=target_sid,
                    is_native_npc=False,
                    ephemeral_realm_id=realm_id,
                    persona_brief=clip_text(prof.get("soul_text"), 180, ""),
                    seed_familiar_realm=False,
                    autonomy_enabled=True,
                )
                aid = str(up["actor_id"])
                owned_actor_ids.append(aid)
                imported_pairs.append((aid, cname))
                preset_ledgers[aid] = make_character_ledger(
                    aid,
                    cname,
                    host_character=cname,
                    is_native_npc=False,
                    persona_brief=clip_text(prof.get("soul_text"), 180, ""),
                    persona_profile=prof,
                    story_role=(
                        {
                            "role_category": "visitor",
                            "local_role": "外来旅人",
                            "home_space_id": target_sid,
                            "home_space_name": home_name,
                            "relationship_to_incident": "刚因旅途或骤雨暂留本镇的外来客人，与镇上原住民尚不相识。",
                        }
                        if is_mystery_or_crisis
                        else {
                            "role_category": "protagonist",
                            "local_role": "核心当事角色",
                            "home_space_id": target_sid,
                            "home_space_name": home_name,
                            "relationship_to_incident": clip_text(raw_out or story_title, 96, "本篇故事当事主角"),
                        }
                    ),
                )

            for n_idx in range(native_count):
                arch = NATIVE_RESIDENT_ARCHETYPES[n_idx % len(NATIVE_RESIDENT_ARCHETYPES)]
                chosen_name = next((nm for nm in arch["names"] if nm not in used_names), f"居民{n_idx + 1}")
                used_names.add(chosen_name)
                home_sp = native_homes[n_idx % len(native_homes)]
                target_sid = str(home_sp["space_id"])
                home_name = str(home_sp.get("name") or "")
                local_brief = f"你是长年居住在「{home_name or w_name}」一带的本镇常住居民，熟悉小镇街坊与各栋建筑。{arch['soul_blurb']}"
                up = self._world.upsert_actor(
                    name=chosen_name,
                    host_character="",
                    actor_id=f"{realm_id}__nat_{n_idx}"[:48],
                    space_id=target_sid,
                    is_native_npc=True,
                    native_realm_id=realm_id,
                    ephemeral_realm_id=realm_id,
                    home_space_id=target_sid,
                    persona_brief=local_brief,
                    seed_familiar_realm=True,
                    autonomy_enabled=True,
                )
                aid = str(up["actor_id"])
                owned_actor_ids.append(aid)
                native_pairs.append((aid, chosen_name))
                self._world.synthesize_actor_sprite(
                    actor_id=aid,
                    name=chosen_name,
                    prompt_text=arch["visual_prompt"],
                    use_llm=False,
                )
                preset_ledgers[aid] = make_character_ledger(
                    aid,
                    chosen_name,
                    is_native_npc=True,
                    persona_brief=local_brief,
                    persona_profile={
                        "traits_summary": arch["traits_summary"],
                        "style_tags": arch["style_tags"],
                        "soul_text": local_brief,
                    },
                    story_role={
                        "role_category": "local_resident",
                        "local_role": "",
                        "home_space_id": target_sid,
                        "home_space_name": home_name,
                        "relationship_to_incident": "",
                    },
                )

            # 区分「本镇熟人圈」与「外来同行旅伴」，原生与外来之间保持初见隔离
            for idx_a in range(len(native_pairs)):
                for idx_b in range(idx_a + 1, len(native_pairs)):
                    nm_a, nm_b = native_pairs[idx_a][1], native_pairs[idx_b][1]
                    for subj, tgt in ((nm_a, nm_b), (nm_b, nm_a)):
                        try:
                            self._world.set_actor_relation(
                                realm_id,
                                subj,
                                tgt,
                                {
                                    "known": True,
                                    "rel_stage": "acquaintance",
                                    "attitude_stance": "neutral",
                                    "trust": 0.52,
                                    "warmth": 0.45,
                                    "relation_tag": "同镇居民",
                                    "shared_memories": "同住在这座小镇多年，平时常在街坊间照面。",
                                },
                            )
                        except Exception:
                            pass
            relation_source = str(cfg.get("relation_source") or "auto").strip()
            seeded_rels: dict[str, dict[str, dict[str, Any]]] = {}
            imported_names = [nm for _, nm in imported_pairs]
            if (
                imported_names
                and relation_source != "story_fresh"
                and hasattr(self._world, "seed_initial_relations")
            ):
                try:
                    seeded_rels = (
                        self._world.seed_initial_relations(
                            realm_id,
                            imported_names,
                            source_space_id=relation_source,
                            include_user=(exp_mode != "observer"),
                        )
                        or {}
                    )
                except Exception:
                    logger.debug("从文字世界继承初始情感指针失败：%s", realm_id, exc_info=True)

            for idx_a in range(len(imported_pairs)):
                for idx_b in range(idx_a + 1, len(imported_pairs)):
                    nm_a, nm_b = imported_pairs[idx_a][1], imported_pairs[idx_b][1]
                    for subj, tgt in ((nm_a, nm_b), (nm_b, nm_a)):
                        if f"char:{tgt}" in (seeded_rels.get(subj) or {}):
                            continue
                        try:
                            self._world.set_actor_relation(
                                realm_id,
                                subj,
                                tgt,
                                {
                                    "known": True,
                                    "rel_stage": "friend",
                                    "attitude_stance": "warm",
                                    "trust": 0.72,
                                    "warmth": 0.68,
                                    "relation_tag": "同行旅伴",
                                    "shared_memories": "一同暂留在这座小镇。",
                                },
                            )
                        except Exception:
                            pass

            if exp_mode != "observer":
                pl = self._world.ensure_player(realm_id, name=clip_text(cfg.get("player_name"), 20, "旅人"))
                player_actor_id = str(pl["actor_id"])
            elif owned_actor_ids:
                self._world.set_background_roster(realm_id, owned_actor_ids)

            return self.attach_world(
                realm_id,
                auto_pluck=bool(cfg.get("auto_pluck", True)),
                theme_compass=theme,
                prompt_hint=clip_text(cfg.get("prompt_hint") or cfg.get("outline"), 180, ""),
                title=story_title or w_name,
                experience_mode=exp_mode,
                world_mode=world_mode,
                player_actor_id=player_actor_id,
                owned_actor_ids=owned_actor_ids,
                max_nodes=max_nodes,
                preset_ledgers=preset_ledgers,
                reveal_inner_thoughts=bool(cfg.get("reveal_inner_thoughts", False)),
                mode="standalone",
                use_llm=use_llm,
            )
        except Exception:
            try:
                self._world.delete_world(realm_id, owned_actor_ids=owned_actor_ids)
            except Exception:
                pass
            raise

    def _refresh_actor_brief_locked(
        self,
        session: Mapping[str, Any],
        thread: Mapping[str, Any] | None,
        node: Mapping[str, Any] | None,
        actor_id: str,
    ) -> None:
        """基于角色个人非全知账本与身份权重合成简洁接地简报（绝不超过 360 字）。"""
        realm_id = str(session.get("realm_space_id") or "")
        ledger = (session.get("ledgers") or {}).get(actor_id)
        if not realm_id or not isinstance(ledger, Mapping) or ledger.get("is_player"):
            return
        sr = ledger.get("story_role") if isinstance(ledger.get("story_role"), Mapping) else {}
        is_native = bool(ledger.get("is_native_npc", False))
        local_role = str(sr.get("local_role") or "")
        home_name = str(sr.get("home_space_name") or "本镇")
        stake = str(sr.get("relationship_to_incident") or "")

        if thread is None or thread.get("status") == "resolved":
            ep_txt = clip_text(((thread or {}).get("chapter_chronicle") or {}).get("epilogue"), 120, "刚才的插曲暂告一段落。")
            id_line = f"本镇常住{local_role or '居民'}（常驻{home_name}）" if is_native else "暂留本镇的旅人"
            brief = f"身份：{id_line}。近况：{ep_txt}"
            try:
                self._world.set_private_brief(
                    actor_id,
                    brief[:350],
                    cover_goal="平静回归日常",
                    space_id=realm_id,
                    night_active=False,
                )
            except Exception:
                pass
            return

        public_fact = clip_text(thread.get("public_incident_fact") or thread.get("premise") or (node or {}).get("situation"), 95, "")
        choice = ((node or {}).get("participant_choices") or {}).get(actor_id) if isinstance(node, Mapping) else None
        deployed_eids = {str(x) for x in ((node or {}).get("deployed_entity_ids") or ()) if str(x)} if isinstance(node, Mapping) else set()
        raw_clues = [
            ev
            for ev in (ledger.get("experienced_events") or ())
            if ev.get("kind") in ("entity_inspected", "entity_read") and ev.get("visibility") in ("direct", "witnessed")
        ]
        filtered_clues = [
            clip_text(ev.get("summary"), 70, "")
            for ev in raw_clues
            if str(ev.get("entity_id") or "") in deployed_eids
            or not any(tok in str(ev.get("summary") or "") for tok in ("小镇布告栏", "木制指路牌", "街口路牌"))
        ][-2:]
        role_cat = str(sr.get("role_category") or "")

        parts: list[str] = []
        if is_native:
            parts.append(f"身份：本镇常住{local_role or '住户'}（常驻{home_name}，熟悉镇民，非外来侦探）。")
        elif role_cat == "visitor" or "外来" in local_role:
            parts.append("身份：暂留本镇的外来旅人（非本镇村民，与居民初见）。")
        else:
            parts.append(f"身份：{local_role or '故事主角'}。")
        if public_fact:
            parts.append(f"背景：{public_fact}")
        if stake and not ("暂留" in stake and role_cat == "protagonist"):
            parts.append(f"处境：{clip_text(stake, 64)}")
        if isinstance(choice, Mapping):
            dec_txt = clip_text(choice.get("inner_reason") or choice.get("choice_summary"), 90)
            if dec_txt:
                parts.append(f"当前决定：{dec_txt}")
            seed_txt = clip_text(choice.get("speech_seed"), 72, "")
            if seed_txt and str(choice.get("execution_status") or "pending") == "pending":
                intent = choice.get("world_intent")
                intent = intent if isinstance(intent, Mapping) else {}
                target_id = str(intent.get("target") or "") if intent.get("kind") in ("social", "seek_player") else ""
                target_name = str(((session.get("ledgers") or {}).get(target_id) or {}).get("name") or "")
                target_label = f"（对「{target_name}」）" if target_name else ""
                parts.append(f"见面想表达{target_label}：{seed_txt}")
        if filtered_clues:
            parts.append(f"亲见实证：{'；'.join(filtered_clues)}")
        parts.append("严禁编造不存在的声响或无意义寒暄，紧扣当前决定推进真实互动。")

        brief_text = "\n".join(parts)[:460]
        cover_goal = clip_text(
            (choice or {}).get("choice_summary") or (node or {}).get("title") or thread.get("title"),
            32,
        )
        has_urgent_night_task = False
        if isinstance(choice, Mapping) and str(choice.get("execution_status") or "pending") == "pending":
            wi = choice.get("world_intent") if isinstance(choice.get("world_intent"), Mapping) else {}
            stance = str(choice.get("stance") or "observe").strip()
            wi_kind = str(wi.get("kind") or "observe").strip()
            if stance in ("investigate", "confront", "protect", "pursue_dream", "confide", "mediate") and wi_kind not in ("", "observe"):
                has_urgent_night_task = True
        try:
            self._world.set_private_brief(
                actor_id,
                brief_text,
                cover_goal=cover_goal,
                space_id=realm_id,
                night_active=has_urgent_night_task,
            )
        except Exception:
            logger.debug("更新角色接地简报失败：%s", actor_id, exc_info=True)

    def _deploy_node_setup_locked(
        self,
        session: dict[str, Any],
        thread: Mapping[str, Any],
        node: dict[str, Any],
        world_state: Mapping[str, Any],
    ) -> None:
        setup = node.get("world_setup") if isinstance(node.get("world_setup"), Mapping) else {}
        realm_id = session["realm_space_id"]
        wmode = str(setup.get("weather_mode") or "").strip().lower()
        if wmode:
            try:
                self._world.set_world_environment(realm_id, weather_mode=wmode)
            except Exception:
                logger.debug("调整丝线环境天气失败", exc_info=True)

        spawns: list[Mapping[str, Any]] = []
        if isinstance(setup.get("spawn_props"), Sequence) and not isinstance(setup.get("spawn_props"), (str, bytes)):
            spawns.extend(p for p in setup["spawn_props"] if isinstance(p, Mapping) and p.get("name"))
        if not spawns and isinstance(setup.get("spawn_prop"), Mapping) and setup["spawn_prop"].get("name"):
            spawns.append(setup["spawn_prop"])

        for spawn in spawns:
            try:
                placed = self._world.synthesize_entity(
                    space_id=str(spawn.get("space_id") or node.get("target_space_id") or realm_id),
                    zone_id=str(spawn.get("zone_id") or node.get("target_zone_id") or ""),
                    custom_name=str(spawn["name"]),
                    prompt_text=str(spawn.get("visual_prompt") or spawn["name"]),
                    brief=str(spawn.get("brief") or ""),
                    detail=str(spawn.get("detail") or ""),
                    collectible=bool(spawn.get("collectible", False)),
                    traits_hint=dict(spawn.get("traits_hint") or {}),
                    placement_strategy="surface" if (spawn.get("traits_hint") or {}).get("inscribable") else "spread",
                    use_llm=False,
                )
                eid = str(placed.get("entity_id") or "")
                if eid:
                    node.setdefault("deployed_entity_ids", []).append(eid)
                    node.setdefault("entry_triggers", []).append({"type": "entity_event", "entity_id": eid})
            except Exception:
                logger.debug("部署丝线节点物件失败", exc_info=True)

        cue = setup.get("ambient_cue")
        if isinstance(cue, Mapping) and cue.get("text"):
            try:
                self._world.emit_ambient_cue(
                    str(cue.get("space_id") or node.get("target_space_id") or realm_id),
                    str(cue["text"]),
                    zone_id=str(cue.get("zone_id") or node.get("target_zone_id") or ""),
                    cue_kind=str(cue.get("cue_kind") or "sound"),
                )
            except Exception:
                logger.debug("发出丝线环境提示失败", exc_info=True)

        for aid, ledger in (session.get("ledgers") or {}).items():
            if not isinstance(ledger, Mapping) or ledger.get("is_player"):
                continue
            self._refresh_actor_brief_locked(session, thread, node, aid)
        self._sync_dialogue_focus_locked(session, world_state)

    def _commit_thread_locked(
        self,
        session: dict[str, Any],
        thread: Mapping[str, Any],
        world_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        active_threads = [t for t in (session.get("threads") or ()) if t.get("status") == "active"]
        if len(active_threads) >= 3:
            raise ValueError("当前同时活跃的命运丝线已达上限（3 条），请先推演并收束其中一条。")
        thread_copy = copy.deepcopy(dict(thread))

        # 持久化原生居民在此丝线中承载的本地身份与当事利害，并写回人设简述供对话层感知
        actors_map = {
            str(a.get("actor_id") or ""): a
            for a in (world_state.get("actors") or ())
            if isinstance(a, Mapping) and a.get("actor_id")
        }
        res_roles = thread_copy.get("resident_roles") or {}
        if isinstance(res_roles, Mapping):
            ledgers = session.get("ledgers") or {}
            for aid, r_info in res_roles.items():
                ledger = ledgers.get(aid)
                if not isinstance(ledger, dict) or not isinstance(r_info, Mapping):
                    continue
                sr = ledger.setdefault("story_role", {
                    "role_category": "local_resident" if ledger.get("is_native_npc") else "visitor",
                    "local_role": "",
                    "home_space_id": "",
                    "home_space_name": "",
                    "relationship_to_incident": "",
                })
                if r_info.get("local_role"):
                    sr["local_role"] = clip_text(r_info["local_role"], 36, sr.get("local_role") or "")
                if r_info.get("relationship_to_incident"):
                    sr["relationship_to_incident"] = clip_text(
                        r_info["relationship_to_incident"], 120, sr.get("relationship_to_incident") or "",
                    )
                if ledger.get("is_native_npc") and aid in actors_map:
                    cur_sp = str(actors_map[aid].get("space_id") or sr.get("home_space_id") or session["realm_space_id"])
                    home_nm = sr.get("home_space_name") or "本镇"
                    role_nm = sr.get("local_role") or "镇民"
                    new_brief = (
                        f"你是本镇「{home_nm}」的{role_nm}（长居本镇的老住户，熟识邻里）。"
                        f"{clip_text(ledger.get('persona_brief'), 140, '')}"
                    )
                    try:
                        self._world.upsert_actor(
                            name=ledger.get("name") or aid,
                            host_character="",
                            actor_id=aid,
                            space_id=cur_sp,
                            is_native_npc=True,
                            home_space_id=str(sr.get("home_space_id") or cur_sp),
                            persona_brief=new_brief,
                        )
                    except Exception:
                        pass

        if not thread_copy.get("lead_actor_ids"):
            leads = sorted(resolve_thread_lead_ids(session, thread_copy, actors_map))
            thread_copy["lead_actor_ids"] = leads
        if thread_copy.get("nodes"):
            first_node = thread_copy["nodes"][0]
            if isinstance(first_node, dict) and not first_node.get("catalyst_actor_ids"):
                first_node["catalyst_actor_ids"] = list(thread_copy.get("lead_actor_ids") or ())

        session.setdefault("threads", []).append(thread_copy)
        for node in thread_copy.get("nodes") or ():
            self._deploy_node_setup_locked(session, thread_copy, node, world_state)
        session["updated_at"] = round(time.time(), 3)
        self._push_overlay_locked(session, world_state)
        self._store.save(session)
        return copy.deepcopy(thread_copy)

    def pluck_thread(
        self,
        session_id: str,
        *,
        theme_compass: str = "auto",
        prompt_hint: str = "",
        focus_actor_ids: Sequence[str] | None = None,
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """在指定命运会话里轻拨一条新的命运丝线（编排期绝不持锁）。"""
        sid = str(session_id or "").strip()
        if (self.get_session(sid) or {}).get("mode") == "dialogue":
            return self.pluck_dialogue_thread(
                sid, theme_compass=theme_compass, prompt_hint=prompt_hint, use_llm=use_llm,
            )
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                raise ValueError(f"命运会话不存在：{session_id}")
            active_threads = [t for t in (sess.get("threads") or ()) if t.get("status") == "active"]
            if len(active_threads) >= 3:
                raise ValueError("当前同时活跃的命运丝线已达上限（3 条），请先推演并收束其中一条。")
            realm_id = sess["realm_space_id"]
            ws = self._world.query_world_state(realm_id)
            sync_cast_ledgers(sess, ws, self._persona, custom_persona_dir=self._custom_persona_dir())
            sess_snap = copy.deepcopy(sess)

        eff_llm = self._effective_use_llm(use_llm)
        cast_rels = self._query_cast_world_relations(realm_id, sess_snap.get("ledgers") or {})
        thread = weave_opening_thread(
            sess_snap,
            ws,
            self._persona,
            theme_compass=theme_compass,
            prompt_hint=prompt_hint,
            focus_actor_ids=focus_actor_ids,
            cast_relations=cast_rels,
            llm_fn=self._llm_fn,
            use_llm=eff_llm,
        )

        with self._lock:
            live = self._sessions.get(sid)
            if live is None:
                raise ValueError(f"命运会话不存在：{session_id}")
            committed = self._commit_thread_locked(live, thread, ws)
            return {"session": copy.deepcopy(live), "thread": committed}

    def record_player_stance(
        self,
        session_id: str,
        stance: str,
        note: str = "",
        *,
        thread_id: str = "",
    ) -> dict[str, Any]:
        """记录玩家自己在当前节点的可选表态（2.A 隐形融入外的轻量显式记录）。"""
        clean_stance = str(stance or "").strip().lower()
        if clean_stance not in VALID_STANCES:
            raise ValueError(f"无效的表态类型：{stance}")
        with self._lock:
            sess = self._sessions.get(str(session_id or "").strip())
            if sess is None:
                raise ValueError(f"命运会话不存在：{session_id}")
            threads = sess.get("threads") or ()
            thread = (
                next((t for t in threads if t.get("thread_id") == thread_id and t.get("status") == "active"), None)
                if thread_id
                else next((t for t in threads if t.get("status") == "active"), None)
            )
            if thread is None:
                raise ValueError("当前没有正在进行的命运丝线。")
            nid = str(thread.get("active_node_id") or "")
            node = next((n for n in (thread.get("nodes") or ()) if n.get("node_id") == nid), None)
            if node is None:
                raise ValueError("当前丝线暂无活跃节点。")
            sim_min = float(sess.get("last_sim_min") or 0.0)
            clean_note = clip_text(note, 140, f"选择了「{STANCE_LABELS.get(clean_stance, clean_stance)}」")
            node["player_stance"] = {
                "stance": clean_stance,
                "stance_label": STANCE_LABELS.get(clean_stance, clean_stance),
                "note": clean_note,
                "at_min": sim_min,
            }
            for aid, ledger in (sess.get("ledgers") or {}).items():
                if ledger.get("is_player"):
                    apply_choice_to_ledger(
                        ledger,
                        {
                            "choice_id": f"player_{nid}",
                            "actor_id": aid,
                            "actor_name": ledger.get("name") or "你",
                            "thread_id": thread["thread_id"],
                            "node_id": nid,
                            "node_title": node.get("title") or "",
                            "stance": clean_stance,
                            "stance_label": STANCE_LABELS.get(clean_stance, clean_stance),
                            "choice_summary": clean_note,
                            "inner_reason": "",
                            "world_intent": {"kind": "observe", "target": "", "action": "", "reason": clean_note, "note_text": ""},
                            "execution_status": "completed",
                            "decided_at_min": sim_min,
                            "completed_at_min": sim_min,
                        },
                    )
                    break
            sess["updated_at"] = round(time.time(), 3)
            self._push_overlay_locked(sess)
            self._store.save(sess)
            return copy.deepcopy(sess)

    def set_reveal_inner_thoughts(self, session_id: str, reveal: bool) -> dict[str, Any]:
        """切换 Decision 3.A 心路可见开关（默认隐藏，支持随时偷看或收起）。"""
        if (self.get_session(session_id) or {}).get("mode") == "dialogue":
            with self._lock, dialogue_write_lock(self._data_root):
                session = copy.deepcopy(self._dialogue_session_locked(session_id))
                session["reveal_inner_thoughts"] = bool(reveal)
                self._save_dialogue_locked(session)
                return session
        with self._lock:
            sess = self._sessions.get(str(session_id or "").strip())
            if sess is None:
                raise ValueError(f"命运会话不存在：{session_id}")
            sess["reveal_inner_thoughts"] = bool(reveal)
            sess["updated_at"] = round(time.time(), 3)
            self._push_overlay_locked(sess)
            self._store.save(sess)
            return copy.deepcopy(sess)

    def _apply_choice_to_world_locked(
        self,
        session: dict[str, Any],
        thread: dict[str, Any],
        node: dict[str, Any],
        actor_id: str,
        choice: dict[str, Any],
    ) -> None:
        realm_id = session["realm_space_id"]
        intent = choice.get("world_intent") or {}
        ikind = str(intent.get("kind") or "observe")
        focus_ids = (node.get("dialogue_focus") or {}).get("actor_ids", ())
        handled_by_focus = ikind == "social" and {actor_id, intent.get("target")} == set(focus_ids)
        if ikind != "observe" and intent.get("target") and not handled_by_focus:
            try:
                lead_ids = resolve_thread_lead_ids(session, thread, {})
                is_active_move = str(choice.get("stance") or "") in (
                    "confide", "confront", "investigate", "pursue_dream", "protect", "mediate",
                )
                res = self._world.propose_actor_commitment(
                    actor_id,
                    {
                        "commitment_id": f"fate:{choice['choice_id']}",
                        "kind": ikind,
                        "target": intent["target"],
                        "action": intent.get("action") or "",
                        "reason": intent.get("reason") or choice.get("choice_summary") or "",
                        "note_text": intent.get("note_text") or "",
                        "priority": "high" if (actor_id in focus_ids or actor_id in lead_ids or is_active_move) else "normal",
                    },
                    space_id=realm_id,
                    preempt=True,
                )
                if res.get("proposed"):
                    choice["commitment_id"] = str(res.get("commitment_id") or "")
            except Exception:
                logger.debug("注入角色自主选择意图失败：%s", actor_id, exc_info=True)

        # 温和写入基于角色个人账本与身份的简洁简报，不泄露他人秘密
        self._refresh_actor_brief_locked(session, thread, node, actor_id)

    def _clear_dialogue_focus_locked(self, realm: str) -> None:
        if realm in self._dialogue_realms:
            try:
                self._world.set_dialogue_focus(realm, [])
                self._dialogue_realms.discard(realm)
            except Exception:
                logger.debug("解除关键对话的保留状态失败：%s", realm, exc_info=True)

    def _sync_dialogue_focus_locked(
        self, session: dict[str, Any], world_state: Mapping[str, Any] | None = None,
        *, force: bool = False,
    ) -> bool:
        if self._closed:
            return False
        realm = session["realm_space_id"]
        ws = world_state or {}
        now = float(ws.get("minutes", session.get("last_sim_min") or 0.0))
        active_actors = {
            actor["actor_id"]: actor for actor in ws.get("actors", ())
            if actor.get("actor_status", "active") == "active"
        }
        scenes: list[dict[str, Any]] = []
        reserved: set[str] = set()
        changed = False
        for thread in session.get("threads", ()):
            if thread.get("status") != "active":
                continue
            node = next((n for n in thread.get("nodes", ()) if n.get("node_id") == thread["active_node_id"]
                         and n.get("status") == "active"), None)
            if node is None:
                continue
            focus = node.get("dialogue_focus") or {}
            local_topic = clip_text(
                f"围绕这两位当事人在《{node.get('title') or '本幕'}》里的谈话："
                f"{node.get('dilemma_frame') or node.get('situation') or '当面说清各自的打算'}",
                160,
            )
            if str(focus.get("topic") or "").startswith("在本幕当面谈清双方想说的事。核心处境："):
                focus["topic"] = local_topic
                changed = True
            if not focus or focus.get("interruption"):
                continue
            if not focus.get("evidence_version"):
                focus["evidence_version"] = 1
                if focus["exchanges"] and len(focus["exchanges"]) == focus["revision"]:
                    focus["review_saved"] = True
                changed = True
            pair = focus["actor_ids"]
            if force:
                focus["interruption"] = "已手动推进本幕。"
                changed = True
                continue
            if "actors" in ws and not set(pair) <= active_actors.keys():
                focus["interruption"] = "有当事人已离场或无法继续交谈，本幕的谈话暂告中断。"
                changed = True
                continue
            if "started_at_min" not in focus:
                if "actors" not in ws and not focus.get("exchanges"):
                    continue
                same_space = "actors" in ws and active_actors[pair[0]].get("space_id") == active_actors[pair[1]].get("space_id")
                leads = set(thread.get("core_actor_ids") or ()) or resolve_thread_lead_ids(session, thread, active_actors)
                if not focus.get("exchanges") and (
                    not set(pair) <= leads or not (same_space or focus.get("required_face_to_face"))
                ):
                    node["dialogue_focus"] = {}
                    changed = True
                    continue
                focus["started_at_min"] = now
                changed = True
            if len(focus["exchanges"]) >= 8:
                focus["interruption"] = "这段谈话告一段落，未谈妥的事项留待后续。"
                changed = True
                continue
            if now >= float(focus["started_at_min"]) + 30.0:
                focus["interruption"] = "本幕的等待时间已到，两位当事人尚未谈出明确结果。"
                changed = True
                continue
            if reserved.intersection(pair):
                continue
            scenes.append({
                "scene_id": node["node_id"],
                "actor_ids": pair,
                "topic": focus["topic"],
                "revision": focus["revision"],
                "review_pending": dialogue_needs_review(focus),
            })
            reserved.update(pair)
        if scenes:
            try:
                if self._world.set_dialogue_focus(realm, scenes).get("available"):
                    self._dialogue_realms.add(realm)
            except Exception:
                logger.debug("同步关键对话的参与角色失败：%s", realm, exc_info=True)
        else:
            self._clear_dialogue_focus_locked(realm)
        return changed

    def _record_actor_choice_locked(
        self,
        session: dict[str, Any],
        thread_id: str,
        node_id: str,
        actor_id: str,
        choice: dict[str, Any],
    ) -> bool:
        thread = next((t for t in (session.get("threads") or ()) if t.get("thread_id") == thread_id and t.get("status") == "active"), None)
        if thread is None:
            return False
        node = next((n for n in (thread.get("nodes") or ()) if n.get("node_id") == node_id and n.get("status") == "active"), None)
        if node is None or (node.get("dialogue_focus") or {}).get("interruption"):
            return False
        p_choices = node.setdefault("participant_choices", {})
        if actor_id in p_choices:
            return False
        p_choices[actor_id] = copy.deepcopy(choice)
        ledger = (session.get("ledgers") or {}).get(actor_id)
        if isinstance(ledger, dict):
            apply_choice_to_ledger(ledger, p_choices[actor_id])
        self._sync_dialogue_focus_locked(session)
        self._apply_choice_to_world_locked(session, thread, node, actor_id, p_choices[actor_id])
        thread["updated_at_min"] = float(session.get("last_sim_min") or 0.0)
        return True

    def _nudge_stalled_choices_locked(
        self,
        session: dict[str, Any],
        thread: dict[str, Any],
        node: dict[str, Any],
        world_state: Mapping[str, Any],
    ) -> bool:
        """为尚未落地执行的主导/角色行动持续重新导航与催促，避免双方错身或闲逛卡住。"""
        sim_min = float(world_state.get("minutes") if world_state.get("minutes") is not None else session.get("last_sim_min") or 0.0)
        realm_id = str(session.get("realm_space_id") or "")
        if not realm_id:
            return False
        actors_map = {
            str(a.get("actor_id") or ""): a
            for a in (world_state.get("actors") or ())
            if isinstance(a, Mapping) and a.get("actor_id")
        }
        lead_ids = resolve_thread_lead_ids(session, thread, actors_map)
        focus = node.get("dialogue_focus") or {}
        focus_ids = set(focus.get("actor_ids") or ()) if not focus.get("interruption") else set()
        changed = False
        for aid, choice in (node.get("participant_choices") or {}).items():
            if not isinstance(choice, dict) or choice.get("execution_status") != "pending":
                continue
            intent = choice.get("world_intent") or {}
            ikind = str(intent.get("kind") or "observe")
            itarget = str(intent.get("target") or "")
            if ikind == "observe" or not itarget:
                continue
            if ikind == "social" and focus_ids.intersection((aid, itarget)):
                continue
            act = actors_map.get(aid)
            if act is not None and act.get("actor_status", "active") != "active":
                continue
            last_at = float(choice.get("last_nudged_at_min") or choice.get("decided_at_min") or 0.0)
            nudges = int(choice.get("nudge_count") or 0)
            if sim_min < last_at + 1.5 or nudges >= 5:
                continue
            try:
                res = self._world.propose_actor_commitment(
                    aid,
                    {
                        "commitment_id": f"fate:{choice['choice_id']}:n{nudges + 1}",
                        "kind": ikind,
                        "target": itarget,
                        "action": intent.get("action") or "",
                        "reason": intent.get("reason") or choice.get("choice_summary") or "",
                        "note_text": intent.get("note_text") or "",
                        "priority": "high" if aid in focus_ids or aid in lead_ids else "normal",
                    },
                    space_id=realm_id,
                    preempt=True,
                )
                if res.get("proposed"):
                    choice["commitment_id"] = str(res.get("commitment_id") or "")
                    choice["last_nudged_at_min"] = round(sim_min, 2)
                    choice["nudge_count"] = nudges + 1
                    changed = True
                    self._refresh_actor_brief_locked(session, thread, node, aid)
            except Exception:
                logger.debug("催促角色未完成抉择失败：%s", aid, exc_info=True)
        return changed

    def _node_ready_to_weave_locked(
        self,
        session: Mapping[str, Any],
        thread: Mapping[str, Any],
        node: Mapping[str, Any],
        world_state: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        focus = node.get("dialogue_focus") or {}
        if focus:
            return bool(force or focus.get("interruption") or dialogue_needs_review(focus))
        p_choices = node.get("participant_choices") or {}
        if not p_choices:
            return False
        if force:
            return True

        sim_min = float(world_state.get("minutes") if world_state.get("minutes") is not None else session.get("last_sim_min") or 0.0)
        actors_by_id = {
            str(a.get("actor_id") or ""): a
            for a in (world_state.get("actors") or ())
            if isinstance(a, Mapping) and a.get("actor_id")
        }
        lead_ids = resolve_thread_lead_ids(session, thread, actors_by_id)

        actionable = [
            (aid, c) for aid, c in p_choices.items()
            if isinstance(c, Mapping) and (c.get("world_intent") or {}).get("kind", "observe") != "observe"
        ]
        pending = [(aid, c) for aid, c in actionable if c.get("execution_status") == "pending"]
        completed = [(aid, c) for aid, c in actionable if c.get("execution_status") == "completed"]
        newest_decision = max(
            (float(c.get("decided_at_min") or 0.0) for c in p_choices.values() if isinstance(c, Mapping)),
            default=0.0,
        )

        # 1. 一旦主角/核心人物在世界里真实完成了对应行动或对话，命运立刻向前推进
        if any(aid in lead_ids for aid, _c in completed):
            return True
        # 若非主角完成行动，仅当当前没有主角仍在赶路/执行行动时才顺延推进，避免路人抢先跳拍
        if completed and not any(aid in lead_ids for aid, _c in pending):
            return True

        # 2. 玩家显式表态推动剧情
        if node.get("player_stance"):
            if not pending or sim_min >= newest_decision + 1.5:
                return True

        # 3. 若尚有角色正在执行未完成的行动（尚无人真实完成），绝不在短超时下强行脑补跳拍；仅保留 30 模拟分钟极端防卡死阀值
        if pending:
            return sim_min >= newest_decision + 30.0

        # 4. 本幕全员均为原地观察/表态时：主角或全员表态后短暂酝酿即可推进
        active_npc_ids = [
            aid for aid, a in actors_by_id.items()
            if not a.get("is_player") and a.get("actor_status", "active") == "active"
        ]
        all_decided = bool(active_npc_ids) and all(aid in p_choices for aid in active_npc_ids)
        lead_decided = bool(lead_ids) and any(aid in p_choices for aid in lead_ids)
        return (lead_decided or all_decided) and sim_min >= newest_decision + 2.0

    def _apply_weave_result_locked(
        self,
        session: dict[str, Any],
        thread_id: str,
        node_id: str,
        result: Mapping[str, Any],
        world_state: Mapping[str, Any],
    ) -> bool:
        thread = next((t for t in (session.get("threads") or ()) if t.get("thread_id") == thread_id and t.get("status") == "active"), None)
        if thread is None or thread.get("active_node_id") != node_id:
            return False
        node = next((n for n in (thread.get("nodes") or ()) if n.get("node_id") == node_id and n.get("status") == "active"), None)
        if node is None:
            return False

        sim_min = float(world_state.get("minutes") if world_state.get("minutes") is not None else session.get("last_sim_min") or 0.0)
        action = str(result.get("action") or "").strip().lower()
        focus = node.get("dialogue_focus") or {}
        if focus:
            if (result.get("dialogue_revision") != focus["revision"]
                    or bool(result.get("dialogue_review_saved")) != bool(focus.get("review_saved"))
                    or str(result.get("dialogue_interruption") or "") != str(focus.get("interruption") or "")):
                return False
            if action == "continue_dialogue" and not focus.get("interruption"):
                focus.pop("review_saved", None)
                focus["revision"] = len(focus["exchanges"])
                self._sync_dialogue_focus_locked(session, world_state)
                return True
            outcome = str(result.get("dialogue_outcome") or "")
            if outcome not in ("answered", "deferred", "interrupted"):
                return False
            if action not in ("resolve", "next_node") or (action == "next_node" and not isinstance(result.get("node"), Mapping)):
                return False
            note = clip_text(result.get("dialogue_evidence"), 160, "本幕谈话已结束。")
            focus["outcome"] = outcome
            for aid, peer in (focus["actor_ids"], focus["actor_ids"][::-1]):
                choice = (node.get("participant_choices") or {}).get(aid)
                if choice and choice.get("execution_status") == "pending":
                    intent = choice.get("world_intent") or {}
                    if intent.get("kind") == "social" and intent.get("target") == peer:
                        choice["execution_status"] = "completed" if outcome == "answered" else "interrupted"
                        choice["execution_note"] = note
                        if outcome == "answered":
                            choice["completed_at_min"] = round(sim_min, 2)
                        apply_choice_to_ledger(session["ledgers"][aid], choice)
        node["status"] = "resolved"
        node["resolved_at_min"] = round(sim_min, 2)
        node["outcome_summary"] = clip_text(result.get("outcome_summary"), 240, node.get("outcome_summary") or "")

        if action == "resolve":
            cc = dict(result.get("chapter_chronicle") or {})
            cc.setdefault("title", f"《{thread['title']}》· 章节纪事")
            cc.setdefault("epilogue", node["outcome_summary"])
            cc["resolved_at_min"] = round(sim_min, 2)
            cc["thread_id"] = thread_id
            thread["status"] = "resolved"
            thread["phase"] = "afterglow"
            thread["active_node_id"] = ""
            thread["chapter_chronicle"] = cc
            thread["updated_at_min"] = round(sim_min, 2)
            session.setdefault("chronicle_archive", []).append(copy.deepcopy(cc))
            if len(session["chronicle_archive"]) > 24:
                del session["chronicle_archive"][:-24]
            for aid in (cc.get("character_epilogues") or {}).keys():
                ledger = (session.get("ledgers") or {}).get(aid)
                if isinstance(ledger, dict):
                    tp = f"经历了「{thread['title']}」的章节收束：{clip_text(cc.get('epilogue'), 60)}"
                    bucket = ledger.setdefault("arc_state", {}).setdefault("turning_points", [])
                    if tp not in bucket:
                        bucket.append(tp)
                        del bucket[:-10]
            for aid, ledger in (session.get("ledgers") or {}).items():
                if isinstance(ledger, Mapping) and not ledger.get("is_player"):
                    self._refresh_actor_brief_locked(session, thread, None, aid)
            self._sync_dialogue_focus_locked(session, world_state)
            return True

        next_node = result.get("node")
        if not isinstance(next_node, Mapping):
            return False
        next_node_copy = copy.deepcopy(dict(next_node))
        actors_by_id = {
            str(a.get("actor_id") or ""): a
            for a in (world_state.get("actors") or ())
            if isinstance(a, Mapping) and a.get("actor_id")
        }
        catalysts: list[str] = list(next_node_copy.get("catalyst_actor_ids") or ())
        node_start = float(node.get("created_at_min") or 0.0)
        ledgers = session.get("ledgers") or {}
        for aid, rec in (node.get("participant_choices") or {}).items():
            if not isinstance(rec, Mapping):
                continue
            if rec.get("execution_status") == "completed":
                catalysts.append(str(aid))
                wi = rec.get("world_intent") or {}
                if wi.get("kind") == "social" and wi.get("target"):
                    catalysts.append(str(wi["target"]))
                lg = ledgers.get(aid) or {}
                for ev in (lg.get("experienced_events") or ())[-6:]:
                    if float(ev.get("sim_min") or 0.0) >= node_start - 0.1 and ev.get("kind") == "dialogue_exchange":
                        for rel_aid in ev.get("related_actor_ids") or ():
                            if str(rel_aid).strip():
                                catalysts.append(str(rel_aid).strip())
        for lid in sorted(resolve_thread_lead_ids(session, thread, world_state_actors := actors_by_id)):
            _ = world_state_actors
            catalysts.append(lid)
        next_node_copy["catalyst_actor_ids"] = list(dict.fromkeys(c for c in catalysts if c))

        thread.setdefault("nodes", []).append(next_node_copy)
        thread["active_node_id"] = str(next_node_copy["node_id"])
        thread["phase"] = str(result.get("phase") or next_node_copy.get("phase") or thread["phase"])
        if result.get("tension") is not None:
            thread["tension"] = round(max(0.0, min(1.0, float(result["tension"]))), 3)
        thread["updated_at_min"] = round(sim_min, 2)
        self._deploy_node_setup_locked(session, thread, next_node_copy, world_state)
        return True

    def _query_cast_world_relations(
        self,
        realm_id: str,
        ledgers: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        if not realm_id or not isinstance(ledgers, Mapping) or not hasattr(self._world, "get_actor_relations"):
            return {}
        cast_names = [
            str(lg.get("host_character") or lg.get("name") or "").strip()
            for lg in ledgers.values()
            if isinstance(lg, Mapping) and not lg.get("is_player") and str(lg.get("host_character") or lg.get("name") or "").strip()
        ]
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for aid, lg in ledgers.items():
            if not isinstance(lg, Mapping) or lg.get("is_player"):
                continue
            subj = str(lg.get("host_character") or lg.get("name") or "").strip()
            if not subj:
                continue
            try:
                rels = self._world.get_actor_relations(
                    realm_id,
                    subj,
                    known_actors=[n for n in cast_names if n != subj],
                )
            except Exception:
                rels = {}
            if isinstance(rels, Mapping) and rels:
                out[str(aid)] = {str(k): dict(v) for k, v in rels.items() if isinstance(v, Mapping)}
        return out

    def _schedule_actor_choice(
        self,
        session_id: str,
        thread: Mapping[str, Any],
        node: Mapping[str, Any],
        actor_id: str,
        world_state: Mapping[str, Any],
        *,
        use_llm: bool,
        sync_models: bool = False,
        world_relations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> bool:
        tid = str(thread["thread_id"])
        nid = str(node["node_id"])
        with self._lock:
            sess_snap = copy.deepcopy(self._sessions.get(session_id))
        if sess_snap is None:
            return False

        custom_dir = self._custom_persona_dir()
        if world_relations is None:
            realm_sid = str(sess_snap.get("realm_space_id") or "")
            ledger_now = (sess_snap.get("ledgers") or {}).get(actor_id)
            if realm_sid and isinstance(ledger_now, Mapping):
                world_relations = self._query_cast_world_relations(
                    realm_sid,
                    {actor_id: ledger_now},
                ).get(actor_id)
        if not use_llm or sync_models:
            choice = decide_actor_choice(
                sess_snap,
                thread,
                node,
                actor_id,
                world_state,
                self._persona,
                llm_fn=(self._llm_fn if use_llm else None),
                use_llm=use_llm,
                custom_persona_dir=custom_dir,
                world_relations=world_relations,
            )
            with self._lock:
                live_sess = self._sessions.get(session_id)
                if live_sess is None:
                    return False
                return self._record_actor_choice_locked(live_sess, tid, nid, actor_id, choice)

        t_copy = copy.deepcopy(dict(thread))
        n_copy = copy.deepcopy(dict(node))
        ws_copy = copy.deepcopy(dict(world_state))
        rel_copy = copy.deepcopy(dict(world_relations)) if isinstance(world_relations, Mapping) else None

        def _job() -> None:
            choice = decide_actor_choice(
                sess_snap,
                t_copy,
                n_copy,
                actor_id,
                ws_copy,
                self._persona,
                llm_fn=self._llm_fn,
                use_llm=True,
                custom_persona_dir=custom_dir,
                world_relations=rel_copy,
            )
            with self._lock:
                live_sess = self._sessions.get(session_id)
                if live_sess is None:
                    return
                if self._record_actor_choice_locked(live_sess, tid, nid, actor_id, choice):
                    live_sess["updated_at"] = round(time.time(), 3)
                    self._push_overlay_locked(live_sess)
                    self._store.save(live_sess)

        self._submit_async(("choice", session_id, tid, nid, actor_id), _job)
        return False

    def _schedule_next_weave(
        self,
        session_id: str,
        thread: Mapping[str, Any],
        node: Mapping[str, Any],
        world_state: Mapping[str, Any],
        *,
        use_llm: bool,
        sync_models: bool = False,
        cast_relations: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> bool:
        tid = str(thread["thread_id"])
        nid = str(node["node_id"])
        with self._lock:
            sess_snap = copy.deepcopy(self._sessions.get(session_id))
        if sess_snap is None:
            return False

        if cast_relations is None:
            cast_relations = self._query_cast_world_relations(
                str(sess_snap.get("realm_space_id") or ""),
                sess_snap.get("ledgers") or {},
            )

        if not use_llm or sync_models:
            result = weave_next_beat(
                sess_snap,
                thread,
                world_state,
                self._persona,
                cast_relations=cast_relations,
                llm_fn=(self._llm_fn if use_llm else None),
                use_llm=use_llm,
            )
            with self._lock:
                live_sess = self._sessions.get(session_id)
                if live_sess is None:
                    return False
                return self._apply_weave_result_locked(live_sess, tid, nid, result, world_state)

        t_copy = copy.deepcopy(dict(thread))
        ws_copy = copy.deepcopy(dict(world_state))
        rel_copy = copy.deepcopy(dict(cast_relations)) if isinstance(cast_relations, Mapping) else None

        def _job() -> None:
            result = weave_next_beat(
                sess_snap,
                t_copy,
                ws_copy,
                self._persona,
                cast_relations=rel_copy,
                llm_fn=self._llm_fn,
                use_llm=True,
            )
            with self._lock:
                live_sess = self._sessions.get(session_id)
                if live_sess is None:
                    return
                if self._apply_weave_result_locked(live_sess, tid, nid, result, ws_copy):
                    live_sess["updated_at"] = round(time.time(), 3)
                    self._push_overlay_locked(live_sess)
                    self._store.save(live_sess)

        self._submit_async(("weave", session_id, tid, nid), _job)
        return False

    def tick_session(
        self,
        session_id: str,
        *,
        force_evaluate: bool = False,
        use_llm: bool | None = None,
        sync_models: bool = False,
    ) -> dict[str, Any] | None:
        """消费世界事件、同步各角色账本、触发节点抉择并按需滚动编排下一拍（模型期绝不持锁）。"""
        if (self.get_session(session_id) or {}).get("mode") == "dialogue":
            return self.advance_dialogue_beat(
                session_id, force_evaluate=force_evaluate, use_llm=use_llm, sync_models=sync_models,
            )
        if not self._world.available():
            return self.get_session(session_id)
        sid = str(session_id or "").strip()
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if sess.get("status") != "active":
                return copy.deepcopy(sess)
            realm_id = sess["realm_space_id"]
            cursor = int(sess.get("event_cursor") or 0)

        try:
            ws = self._world.query_world_state(realm_id)
            events: list[dict[str, Any]] = []
            cur = cursor
            for _ in range(10):
                ev_res = self._world.poll_events(after_seq=cur, limit=500, realm_id=realm_id)
                batch = ev_res.get("events") or ()
                events.extend(batch)
                nxt = int(ev_res.get("next_seq") or cur)
                if not batch or nxt <= cur or len(batch) < 500:
                    break
                cur = nxt
        except Exception:
            logger.debug("nanami_story.fate_runtime 轮询世界状态失败：%s", session_id, exc_info=True)
            return self.get_session(session_id)

        eff_llm = self._effective_use_llm(use_llm)
        custom_dir = self._custom_persona_dir()

        # Phase A: 更新记事与筛选待抉择队列（收集快照后立即释锁，绝不持锁跑模型）
        choice_jobs: list[tuple[str, str, str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None or sess.get("status") != "active":
                return copy.deepcopy(sess) if sess else None

            changed = sync_cast_ledgers(sess, ws, self._persona, custom_persona_dir=custom_dir)
            ingest_res = ingest_world_events(sess, events, ws)
            changed |= bool(ingest_res.get("changed"))
            changed |= self._sync_dialogue_focus_locked(sess, ws, force=force_evaluate)
            refresh_aids = set(ingest_res.get("discovered_actor_ids") or ()) | set(ingest_res.get("completed_actor_ids") or ())
            for disc_aid in sorted(refresh_aids):
                disc_eids = {
                    str(ev.get("entity_id") or "")
                    for ev in (sess["ledgers"].get(disc_aid, {}).get("experienced_events") or ())[-6:]
                    if ev.get("kind") in ("entity_inspected", "entity_read") and ev.get("entity_id")
                }
                for active_t in sess.get("threads") or ():
                    if active_t.get("status") != "active":
                        continue
                    active_n = next(
                        (n for n in (active_t.get("nodes") or ()) if n.get("node_id") == active_t.get("active_node_id")),
                        None,
                    )
                    if active_n is not None and (
                        not disc_eids
                        or disc_aid in (active_n.get("participant_choices") or {})
                        or any(eid in disc_eids for eid in (active_n.get("deployed_entity_ids") or ()))
                    ):
                        self._refresh_actor_brief_locked(sess, active_t, active_n, disc_aid)
                        break

            for active_t in sess.get("threads") or ():
                if active_t.get("status") != "active":
                    continue
                active_n = next(
                    (n for n in (active_t.get("nodes") or ()) if n.get("node_id") == active_t.get("active_node_id") and n.get("status") == "active"),
                    None,
                )
                if active_n is not None and not force_evaluate and not (active_n.get("dialogue_focus") or {}).get("interruption"):
                    changed |= self._nudge_stalled_choices_locked(sess, active_t, active_n, ws)

            threads_by_id = {str(t["thread_id"]): t for t in (sess.get("threads") or ())}
            triggered_rows = list(ingest_res.get("triggered") or ())
            if force_evaluate:
                existing_pairs = {(t_i, a_i) for t_i, _n_i, a_i in triggered_rows}
                for thread in sess.get("threads") or ():
                    if thread.get("status") != "active":
                        continue
                    nid = str(thread.get("active_node_id") or "")
                    node = next((n for n in (thread.get("nodes") or ()) if n.get("node_id") == nid), None)
                    if node is None or (node.get("dialogue_focus") or {}).get("interruption"):
                        continue
                    for aid, ledger in (sess.get("ledgers") or {}).items():
                        if ledger.get("is_player") or aid in (node.get("participant_choices") or {}):
                            continue
                        if (thread["thread_id"], aid) not in existing_pairs:
                            triggered_rows.append((thread["thread_id"], nid, aid))

            for tid, nid, aid in triggered_rows:
                thread = threads_by_id.get(tid)
                if thread is None or thread.get("status") != "active":
                    continue
                node = next((n for n in (thread.get("nodes") or ()) if n.get("node_id") == nid and n.get("status") == "active"), None)
                if node is None or (node.get("dialogue_focus") or {}).get("interruption") or aid in (node.get("participant_choices") or {}):
                    continue
                if eff_llm and not sync_models:
                    self._schedule_actor_choice(sid, thread, node, aid, ws, use_llm=True, sync_models=False)
                else:
                    choice_jobs.append((tid, nid, aid, copy.deepcopy(sess), copy.deepcopy(thread), copy.deepcopy(node)))

        # Phase B: 锁外执行角色抉择并在重新加锁后提交
        cast_rels: dict[str, dict[str, dict[str, Any]]] | None = None
        if choice_jobs:
            cast_rels = self._query_cast_world_relations(realm_id, choice_jobs[0][3].get("ledgers") or {})
            computed_choices: list[tuple[str, str, str, dict[str, Any]]] = []
            for tid, nid, aid, sess_snap, t_copy, n_copy in choice_jobs:
                choice = decide_actor_choice(
                    sess_snap,
                    t_copy,
                    n_copy,
                    aid,
                    ws,
                    self._persona,
                    llm_fn=(self._llm_fn if eff_llm else None),
                    use_llm=eff_llm,
                    custom_persona_dir=custom_dir,
                    world_relations=(cast_rels or {}).get(aid),
                )
                computed_choices.append((tid, nid, aid, choice))
            with self._lock:
                live = self._sessions.get(sid)
                if live is not None and live.get("status") == "active":
                    for tid, nid, aid, choice in computed_choices:
                        if self._record_actor_choice_locked(live, tid, nid, aid, choice):
                            changed = True

        # Phase C: 检查哪些线程的当前节点已就绪，需要编排下一拍或收束
        weave_jobs: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        with self._lock:
            live = self._sessions.get(sid)
            if live is None or live.get("status") != "active":
                return copy.deepcopy(live) if live else None
            for thread in live.get("threads") or ():
                if thread.get("status") != "active":
                    continue
                nid = str(thread.get("active_node_id") or "")
                node = next((n for n in (thread.get("nodes") or ()) if n.get("node_id") == nid and n.get("status") == "active"), None)
                if node is None:
                    continue
                if self._node_ready_to_weave_locked(live, thread, node, ws, force=force_evaluate):
                    if eff_llm and not sync_models:
                        self._schedule_next_weave(sid, thread, node, ws, use_llm=True, sync_models=False)
                    else:
                        weave_jobs.append((str(thread["thread_id"]), nid, copy.deepcopy(live), copy.deepcopy(thread)))

        # Phase D: 锁外执行滚动编排并回填
        if weave_jobs:
            if cast_rels is None:
                cast_rels = self._query_cast_world_relations(realm_id, weave_jobs[0][2].get("ledgers") or {})
            woven_results: list[tuple[str, str, dict[str, Any]]] = []
            for tid, nid, sess_snap, t_copy in weave_jobs:
                w_res = weave_next_beat(
                    sess_snap,
                    t_copy,
                    ws,
                    self._persona,
                    cast_relations=cast_rels,
                    llm_fn=(self._llm_fn if eff_llm else None),
                    use_llm=eff_llm,
                )
                woven_results.append((tid, nid, w_res))
            with self._lock:
                live = self._sessions.get(sid)
                if live is not None and live.get("status") == "active":
                    for tid, nid, w_res in woven_results:
                        if self._apply_weave_result_locked(live, tid, nid, w_res, ws):
                            changed = True

        with self._lock:
            live = self._sessions.get(sid)
            if live is None:
                return None
            if changed:
                live["updated_at"] = round(time.time(), 3)
                self._push_overlay_locked(live, ws)
                self._store.save(live)
            return copy.deepcopy(live)

    def tick_all(self) -> None:
        with self._lock:
            self._store.reload_if_changed(self._sessions)
            active_ids = [sid for sid, sess in self._sessions.items()
                          if sess.get("status") == "active" and sess.get("mode") != "dialogue"]
        for sid in active_ids:
            try:
                self.tick_session(sid)
            except Exception:
                logger.debug("命运会话轮询失败：%s", sid, exc_info=True)

    def detach_session(self, session_id: str, *, delete_world: bool = False) -> dict[str, Any]:
        """解绑并清理命运会话；常驻挂载模式（attached）严禁删除原小镇。"""
        if (self.get_session(session_id) or {}).get("mode") == "dialogue":
            with self._lock, dialogue_write_lock(self._data_root):
                session = copy.deepcopy(self._dialogue_session_locked(session_id))
                self._observer_autoplay_state.pop(session_id, None)
                self._pending_observer_openings.pop(session_id, None)
                self._bind_dialogue_locked(session, "", "")
                self._store.delete(session_id)
                self._sessions.pop(session_id, None)
                return {"ok": True, "session_id": session_id, "preserved_world": True}
        with self._lock:
            sid = str(session_id or "").strip()
            sess = self._sessions.pop(sid, None)
            if sess is None:
                raise ValueError(f"命运会话不存在：{session_id}")
            realm_id = str(sess.get("realm_space_id") or "")
            mode = str(sess.get("mode") or "attached")
            self._clear_dialogue_focus_locked(realm_id)
            self._store.delete(sid)

        if self._world.available() and realm_id:
            try:
                if sess.get("experience_mode") == "observer":
                    self._world.set_background_roster(realm_id, [])
                self._world.unregister_overlay_handler(realm_id)
                self._world.set_overlay(realm_id, None)
                for aid, ledger in (sess.get("ledgers") or {}).items():
                    if not ledger.get("is_player"):
                        self._world.clear_private_brief(aid, space_id=realm_id)
            except Exception:
                logger.debug("清理命运会话世界面板失败：%s", realm_id, exc_info=True)

            # 硬不变量：常驻挂载模式（attached）绝不删档
            if mode == "standalone" and delete_world:
                try:
                    owned = list(sess.get("owned_actor_ids") or ()) or [
                        aid for aid, ledger in (sess.get("ledgers") or {}).items()
                        if not ledger.get("is_player")
                    ]
                    self._world.delete_world(realm_id, owned_actor_ids=owned)
                except Exception:
                    logger.debug("清理独立命运舞台失败：%s", realm_id, exc_info=True)

        return {"ok": True, "session_id": sid, "preserved_world": mode == "attached" or not delete_world}

    def _handle_overlay_action(self, session_id: str, req: Mapping[str, Any]) -> dict[str, Any]:
        action_id = str(req.get("action_id") or "").strip()
        payload = req.get("payload") if isinstance(req.get("payload"), Mapping) else {}
        try:
            if action_id == "refresh_fate":
                sess = self.tick_session(session_id)
                return {"message": "已同步最新小镇见闻与丝线动态。", "session": sess}

            if action_id == "toggle_inner_thoughts":
                cur = self.get_session(session_id)
                if cur is None:
                    return {"error": "命运会话不存在。"}
                nxt = not bool(cur.get("reveal_inner_thoughts", False))
                self.set_reveal_inner_thoughts(session_id, nxt)
                return {"message": "已展开角色内心独白与完整心路。" if nxt else "已收起未收束丝线的角色内心独白。"}

            if action_id == "pluck_fate_thread":
                res = self.pluck_thread(session_id)
                return {"message": f"已轻拨新丝线：《{res['thread']['title']}》"}

            if action_id == "record_player_stance":
                sel = list(payload.get("selected_ids") or ())
                if not sel:
                    return {"error": "请先点选一项你想留下的态度。"}
                self.record_player_stance(session_id, str(sel[0]))
                return {"message": f"已记录你的态度：{STANCE_LABELS.get(str(sel[0]), str(sel[0]))}"}

            if action_id == "advance_fate_beat":
                sess = self.tick_session(
                    session_id,
                    force_evaluate=True,
                    use_llm=self._effective_use_llm(),
                    sync_models=True,
                )
                return {"message": "已顺着当前选择推演命运的下一拍。", "session": sess}

            return {"error": f"不支持的命运面板动作：{action_id}"}
        except ValueError as exc:
            return {"error": str(exc)}
