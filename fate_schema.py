"""Fate Weaver v3（命运丝线）数据结构、规范化与心路脱敏视图。"""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

FATE_SCHEMA_VERSION = 3
MAX_ORGANIC_NODES = 20

VALID_FATE_MODES: tuple[str, ...] = ("attached", "standalone", "dialogue")
VALID_DIALOGUE_INTENTS = (
    "probe", "confide", "conceal", "confront", "deflect", "propose_shift", "silent_observe",
)
VALID_EXPERIENCE_MODES: tuple[str, ...] = ("co_play", "observer")
VALID_THEME_COMPASSES: tuple[str, ...] = (
    "daily_aspiration",
    "bond_turning",
    "mystery_ripple",
    "crisis_test",
)
THEME_COMPASS_LABELS: dict[str, str] = {
    "daily_aspiration": "日常微光 · 心愿萌芽",
    "bond_turning": "羁绊转折 · 心照不宣",
    "mystery_ripple": "谜团涟漪 · 寻迹探幽",
    "crisis_test": "风波考验 · 抉择时刻",
}

VALID_THREAD_PHASES: tuple[str, ...] = (
    "spark",
    "entangle",
    "crossroads",
    "ripple",
    "climax",
    "afterglow",
)
PHASE_LABELS: dict[str, str] = {
    "spark": "初现微光",
    "entangle": "渐入丝线",
    "crossroads": "抉择路口",
    "ripple": "因果涟漪",
    "climax": "心意碰撞",
    "afterglow": "篇章落笔",
}

VALID_STANCES: tuple[str, ...] = (
    "confront",
    "protect",
    "confide",
    "investigate",
    "conceal",
    "withdraw",
    "pursue_dream",
    "mediate",
)
STANCE_LABELS: dict[str, str] = {
    "confront": "当面求证",
    "protect": "默默护住",
    "confide": "找人坦诚",
    "investigate": "暗中探查",
    "conceal": "暂且隐瞒",
    "withdraw": "独自沉淀",
    "pursue_dream": "践行念想",
    "mediate": "居中调和",
}

VALID_INTENT_KINDS: tuple[str, ...] = ("visit", "entity", "social", "seek_player", "observe")

NATIVE_RESIDENT_ARCHETYPES: tuple[dict[str, Any], ...] = (
    {
        "archetype_id": "local_keeper",
        "names": ("林间", "星野勇", "朔风"),
        "soul_blurb": "本镇常住居民，熟悉镇上各栋建筑与老规矩，性格直爽敏感、遇事容易先护住自己的地盘和熟人。",
        "visual_prompt": "穿着深蓝色防风外套配米色围巾的本镇年轻住户，神情敏锐而警惕",
        "traits_summary": {
            "effortful_control": 0.48, "surgency": 0.86, "safety_need": 0.22,
            "relatedness_need": 0.46, "prosocial": 0.54, "norm_compliance": 0.48,
            "affiliation": 0.50, "avoidance": 0.16, "competence_need": 0.90,
        },
        "style_tags": ["熟悉镇上地形与旧事", "直爽敏感、反应迅速"],
    },
    {
        "archetype_id": "local_host",
        "names": ("苏晴", "白川暖", "悠真"),
        "soul_blurb": "在镇上长住的温柔店主，对街坊邻里的过往了如指掌，重情护短、担心风波伤及身边熟悉的人。",
        "visual_prompt": "穿着暖杏色毛衣、眉眼温柔却略带牵挂的本镇店主，系着帆布围裙",
        "traits_summary": {
            "effortful_control": 0.76, "surgency": 0.44, "safety_need": 0.44,
            "relatedness_need": 0.92, "prosocial": 0.92, "norm_compliance": 0.74,
            "affiliation": 0.86, "avoidance": 0.20, "competence_need": 0.62,
        },
        "style_tags": ["街坊熟络、重情护短", "顾念熟人安危"],
    },
    {
        "archetype_id": "local_steward",
        "names": ("沈墨", "雾岛静", "暮云"),
        "soul_blurb": "深居简出的本镇老住户，习惯记账和留心细枝末节，在摸清风向之前绝不轻易吐露自己知道的内情。",
        "visual_prompt": "穿浅灰风衣、戴细框眼镜的安静本镇老住户，神态冷静克制",
        "traits_summary": {
            "effortful_control": 0.88, "surgency": 0.16, "safety_need": 0.92,
            "relatedness_need": 0.30, "prosocial": 0.42, "norm_compliance": 0.84,
            "affiliation": 0.32, "avoidance": 0.88, "competence_need": 0.45,
        },
        "style_tags": ["城府沉稳、惜字如金", "对镇上风声极度警觉"],
    },
    {
        "archetype_id": "local_artisan",
        "names": ("南宫匠", "夏目音", "千寻"),
        "soul_blurb": "长年守着镇上工坊与旧物件的手艺人，念旧执拗、对镇里的老器物和人情往来有着自己的执着。",
        "visual_prompt": "系着工装围裙、袖口略微卷起的本镇手艺人，腰间别着常用工具包",
        "traits_summary": {
            "effortful_control": 0.72, "surgency": 0.68, "safety_need": 0.36,
            "relatedness_need": 0.66, "prosocial": 0.74, "norm_compliance": 0.60,
            "affiliation": 0.68, "avoidance": 0.28, "competence_need": 0.86,
        },
        "style_tags": ["熟悉老器物来历", "脾气执拗、守口风紧"],
    },
)


def clip_text(value: Any, max_len: int, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip()


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return round(max(lo, min(hi, num)), 3)


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def normalize_max_nodes(value: Any, default: int = 0, *, minimum: int = 3) -> int:
    """规范化丝线拍数上限：0/auto 表示不限固定拍数、随角色抉择持续演进（硬顶 MAX_ORGANIC_NODES）。"""
    if value is None:
        return default
    if isinstance(value, str):
        clean = value.strip().lower()
        if not clean or clean in ("0", "auto", "organic", "continuous", "unlimited"):
            return 0
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    if num <= 0:
        return 0
    return max(minimum, min(MAX_ORGANIC_NODES, num))


def _str_list(raw: Any, *, max_items: int = 12, max_len: int = 120) -> list[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        val = clip_text(item, max_len)
        if val and val not in seen:
            seen.add(val)
            out.append(val)
            if len(out) >= max_items:
                break
    return out


def make_character_ledger(
    actor_id: str,
    name: str,
    *,
    host_character: str = "",
    is_player: bool = False,
    is_native_npc: bool = False,
    persona_brief: str = "",
    persona_profile: Mapping[str, Any] | None = None,
    story_role: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """初始化单角色非全知三维个人记事账本（L1 亲历 / L2 抉择 / L3 生命弧光）。"""
    profile = persona_profile if isinstance(persona_profile, Mapping) else {}
    raw_role = story_role if isinstance(story_role, Mapping) else {}
    default_cat = "player" if is_player else ("local_resident" if is_native_npc else "visitor")
    norm_role = {
        "role_category": str(raw_role.get("role_category") or default_cat),
        "local_role": clip_text(raw_role.get("local_role"), 36, ""),
        "home_space_id": str(raw_role.get("home_space_id") or ""),
        "home_space_name": clip_text(raw_role.get("home_space_name"), 36, ""),
        "relationship_to_incident": clip_text(raw_role.get("relationship_to_incident"), 120, ""),
    }
    return {
        "actor_id": str(actor_id),
        "name": clip_text(name, 32, str(actor_id)),
        "host_character": clip_text(host_character, 32, ""),
        "is_player": bool(is_player),
        "is_native_npc": bool(is_native_npc),
        "persona_brief": clip_text(persona_brief or profile.get("soul_text"), 240, ""),
        "traits_summary": dict(profile.get("traits_summary") or {}),
        "style_tags": _str_list(profile.get("style_tags"), max_items=8, max_len=40),
        "story_role": norm_role,
        "experienced_events": [],
        "choice_records": [],
        "arc_state": {
            "aspirations": [],
            "promises": [],
            "secrets": [],
            "stance_toward": {},
            "turning_points": [],
        },
    }


def normalize_world_intent(raw: Any) -> dict[str, str]:
    src = raw if isinstance(raw, Mapping) else {}
    kind = str(src.get("kind") or "observe").strip().lower()
    if kind not in VALID_INTENT_KINDS:
        kind = "observe"
    target = clip_text(src.get("target"), 80, "")
    if kind != "observe" and not target:
        kind = "observe"
    action = clip_text(src.get("action"), 24, "").lower()
    reason = clip_text(src.get("reason"), 48, "")
    note_text = clip_text(src.get("note_text"), 120, "")
    return {
        "kind": kind,
        "target": target,
        "action": action,
        "reason": reason,
        "note_text": note_text,
    }


def normalize_arc_delta(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    stance_raw = src.get("stance_toward")
    stance_toward: dict[str, str] = {}
    if isinstance(stance_raw, Mapping):
        for k, v in stance_raw.items():
            ck = clip_text(k, 32, "")
            cv = clip_text(v, 80, "")
            if ck and cv:
                stance_toward[ck] = cv
    return {
        "aspirations": _str_list(src.get("aspirations"), max_items=4, max_len=80),
        "promises": _str_list(src.get("promises"), max_items=4, max_len=80),
        "secrets": _str_list(src.get("secrets"), max_items=4, max_len=80),
        "turning_points": _str_list(src.get("turning_points"), max_items=3, max_len=100),
        "stance_toward": stance_toward,
    }


def normalize_choice_record(
    raw: Any,
    *,
    actor_id: str,
    actor_name: str = "",
    thread_id: str,
    node_id: str,
    node_title: str = "",
    sim_min: float = 0.0,
) -> dict[str, Any]:
    """规范化节点抉择卡（ChoiceRecord）。"""
    src = raw if isinstance(raw, Mapping) else {}
    stance = str(src.get("stance") or "investigate").strip().lower()
    if stance not in VALID_STANCES:
        stance = "investigate"
    intent = normalize_world_intent(src.get("world_intent"))
    cid = clip_text(src.get("choice_id"), 48, "") or f"choice_{uuid.uuid4().hex[:10]}"
    exec_status = str(src.get("execution_status") or "").strip().lower()
    if exec_status not in ("pending", "completed", "interrupted", "observed"):
        exec_status = "observed" if intent["kind"] == "observe" else "pending"
    dialogue = src.get("dialogue_intent")
    if isinstance(dialogue, Mapping):
        kind = str(dialogue.get("kind") or "").strip().lower()
        dialogue = {
            "kind": kind if kind in VALID_DIALOGUE_INTENTS else "silent_observe",
            "target": clip_text(dialogue.get("target"), 80, ""),
            "hint": clip_text(dialogue.get("hint"), 120, ""),
        }
    return {
        "choice_id": cid,
        "actor_id": str(actor_id),
        "actor_name": clip_text(actor_name or src.get("actor_name"), 32, str(actor_id)),
        "thread_id": str(thread_id),
        "node_id": str(node_id),
        "node_title": clip_text(node_title or src.get("node_title"), 48, ""),
        "stance": stance,
        "stance_label": STANCE_LABELS.get(stance, stance),
        "choice_summary": clip_text(src.get("choice_summary"), 140, "静观其变，记下眼前的细节。"),
        "inner_reason": clip_text(src.get("inner_reason"), 240, ""),
        "inner_hidden": bool(src.get("inner_hidden", False)),
        "world_intent": intent,
        **({"dialogue_intent": dialogue} if isinstance(dialogue, Mapping) else {}),
        "speech_seed": clip_text(src.get("speech_seed"), 100, ""),
        "arc_delta": normalize_arc_delta(src.get("arc_delta")),
        "execution_status": exec_status,
        "execution_note": clip_text(src.get("execution_note"), 120, ""),
        "commitment_id": clip_text(src.get("commitment_id"), 64, ""),
        "decided_at_min": round(float(src.get("decided_at_min", sim_min) or 0.0), 2),
        "completed_at_min": (
            round(float(src["completed_at_min"]), 2)
            if src.get("completed_at_min") is not None
            else None
        ),
    }


def normalize_dialogue_focus(raw: Any, ledgers: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    ids = raw.get("actor_ids")
    topic = clip_text(raw.get("topic"), 160, "")
    if (not isinstance(ids, list) or len(ids) != 2 or not topic
            or not all(isinstance(aid, str) and isinstance(ledgers.get(aid), Mapping)
                       and not ledgers[aid].get("is_player") for aid in ids)
            or ids[0] == ids[1]):
        return {}
    focus = {"actor_ids": list(ids), "topic": topic, "revision": 0, "exchanges": [], "evidence_version": 1}
    if raw.get("required_face_to_face") is True:
        focus["required_face_to_face"] = True
    return focus


def dialogue_needs_review(focus: Mapping[str, Any]) -> bool:
    return bool(focus.get("review_saved") or len(focus["exchanges"]) > focus["revision"])


def make_story_node(
    *,
    node_id: str = "",
    parent_node_id: str = "",
    seq: int = 1,
    title: str = "",
    phase: str = "spark",
    situation: str = "",
    dilemma_frame: str = "",
    target_space_id: str = "",
    target_space_name: str = "",
    target_floor_label: str = "",
    target_zone_id: str = "",
    target_zone_name: str = "",
    world_setup: Mapping[str, Any] | None = None,
    deployed_entity_ids: Sequence[str] | None = None,
    entry_triggers: Sequence[Mapping[str, Any]] | None = None,
    catalyst_actor_ids: Sequence[str] | None = None,
    dialogue_focus: Mapping[str, Any] | None = None,
    created_at_min: float = 0.0,
    pressure_deadline_min: float = 0.0,
    turns_in_node: int = 0,
    min_turns_before_advance: int = 2,
    scene_name: str = "",
    atmosphere: str = "",
    revealed_facts: Sequence[str] | None = None,
    hidden_hooks: Sequence[str] | None = None,
    recent_dialogue_lines: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    clean_phase = str(phase or "spark").strip().lower()
    if clean_phase not in VALID_THREAD_PHASES:
        clean_phase = "spark"
    return {
        "node_id": node_id or f"node_{seq}_{uuid.uuid4().hex[:6]}",
        "parent_node_id": str(parent_node_id or ""),
        "seq": max(1, int(seq)),
        "title": clip_text(title, 48, f"第 {seq} 拍"),
        "phase": clean_phase,
        "situation": clip_text(situation, 320, "平静的小镇里浮起一丝微小的波澜。"),
        "dilemma_frame": clip_text(dilemma_frame, 240, "你可以按自己的性子决定如何回应眼前的局势。"),
        "target_space_id": str(target_space_id or ""),
        "target_space_name": clip_text(target_space_name, 48, ""),
        "target_floor_label": clip_text(target_floor_label, 12, ""),
        "target_zone_id": str(target_zone_id or ""),
        "target_zone_name": clip_text(target_zone_name, 48, ""),
        "world_setup": copy.deepcopy(dict(world_setup or {})),
        "deployed_entity_ids": [str(eid) for eid in (deployed_entity_ids or ()) if str(eid).strip()],
        "entry_triggers": [dict(t) for t in (entry_triggers or ()) if isinstance(t, Mapping)],
        "catalyst_actor_ids": list(dict.fromkeys(str(aid).strip() for aid in (catalyst_actor_ids or ()) if str(aid).strip())),
        "dialogue_focus": copy.deepcopy(dict(dialogue_focus or {})),
        "participant_choices": {},
        "player_stance": None,
        "outcome_summary": "",
        "status": "active",
        "created_at_min": round(float(created_at_min), 2),
        "resolved_at_min": None,
        "pressure_deadline_min": round(float(pressure_deadline_min), 2),
        "turns_in_node": max(0, int(turns_in_node)),
        "min_turns_before_advance": max(0, int(min_turns_before_advance)),
        "scene_name": clip_text(scene_name, 80, ""),
        "atmosphere": clip_text(atmosphere, 160, ""),
        "revealed_facts": _str_list(revealed_facts, max_items=24, max_len=240),
        "hidden_hooks": _str_list(hidden_hooks),
        "recent_dialogue_lines": [copy.deepcopy(dict(row)) for row in
                                  (recent_dialogue_lines or ()) if isinstance(row, Mapping)][-48:],
    }


def make_fate_thread(
    *,
    thread_id: str = "",
    title: str = "",
    premise: str = "",
    theme_compass: str = "daily_aspiration",
    tension: float = 0.25,
    phase: str = "spark",
    cast_stakes: Mapping[str, Mapping[str, Any]] | None = None,
    nodes: Sequence[Mapping[str, Any]] | None = None,
    active_node_id: str = "",
    max_nodes: int = 0,
    min_nodes: int = 3,
    lead_actor_ids: Sequence[str] | None = None,
    core_actor_ids: Sequence[str] = (),
    created_at_min: float = 0.0,
    public_incident_fact: str = "",
    resident_roles: Mapping[str, Mapping[str, Any]] | None = None,
    visitor_framing: str = "",
) -> dict[str, Any]:
    clean_theme = str(theme_compass or "daily_aspiration").strip().lower()
    if clean_theme not in VALID_THEME_COMPASSES:
        clean_theme = "daily_aspiration"
    clean_phase = str(phase or "spark").strip().lower()
    if clean_phase not in VALID_THREAD_PHASES:
        clean_phase = "spark"
    node_list = [copy.deepcopy(dict(n)) for n in (nodes or ()) if isinstance(n, Mapping)]
    eff_active = str(active_node_id or (node_list[-1]["node_id"] if node_list else ""))
    norm_res_roles: dict[str, dict[str, str]] = {}
    for aid, row in (resident_roles or {}).items():
        if not isinstance(row, Mapping):
            continue
        norm_res_roles[str(aid)] = {
            "local_role": clip_text(row.get("local_role"), 36, ""),
            "relationship_to_incident": clip_text(row.get("relationship_to_incident"), 120, ""),
        }
    return {
        "thread_id": thread_id or f"thread_{uuid.uuid4().hex[:8]}",
        "title": clip_text(title, 56, "悄然浮现的命运丝线"),
        "premise": clip_text(premise, 240, ""),
        "public_incident_fact": clip_text(public_incident_fact, 260, ""),
        "resident_roles": norm_res_roles,
        "visitor_framing": clip_text(visitor_framing, 160, ""),
        "theme_compass": clean_theme,
        "tension": _clamp_float(tension, 0.0, 1.0, 0.25),
        "phase": clean_phase,
        "status": "active",
        "cast_stakes": {
            str(k): copy.deepcopy(dict(v))
            for k, v in (cast_stakes or {}).items()
            if isinstance(v, Mapping)
        },
        "nodes": node_list,
        "active_node_id": eff_active,
        "max_nodes": normalize_max_nodes(max_nodes, 0, minimum=min_nodes),
        "lead_actor_ids": list(dict.fromkeys(str(aid).strip() for aid in (lead_actor_ids or ()) if str(aid).strip())),
        "core_actor_ids": list(dict.fromkeys(core_actor_ids)),
        "chapter_chronicle": None,
        "created_at_min": round(float(created_at_min), 2),
        "updated_at_min": round(float(created_at_min), 2),
    }


def make_fate_session(
    *,
    mode: str = "attached",
    realm_space_id: str = "",
    realm_name: str = "",
    title: str = "",
    experience_mode: str = "co_play",
    world_mode: str = "town",
    player_actor_id: str = "",
    owned_actor_ids: Sequence[str] | None = None,
    reveal_inner_thoughts: bool = False,
    event_cursor: int = 0,
    started_at_min: float = 0.0,
    ledgers: Mapping[str, Mapping[str, Any]] | None = None,
    paused: bool = False,
    linked_history_path: str = "",
    linked_conversation_id: str = "",
    turn_count: int = 0,
    revision: int = 0,
    director_whispers: Sequence[Mapping[str, Any]] | None = None,
    turn_checkpoints: Sequence[Mapping[str, Any]] | None = None,
    scene_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    now = round(time.time(), 3)
    clean_mode = str(mode or "attached").strip().lower()
    if clean_mode not in VALID_FATE_MODES:
        clean_mode = "attached"
    clean_exp = str(experience_mode or "co_play").strip().lower()
    if clean_exp not in VALID_EXPERIENCE_MODES:
        clean_exp = "co_play"
    return {
        "session_id": f"fate_{uuid.uuid4().hex[:12]}",
        "version": FATE_SCHEMA_VERSION,
        "mode": clean_mode,
        "title": clip_text(title or realm_name, 56, str(realm_space_id)),
        "experience_mode": clean_exp,
        "world_mode": clip_text(world_mode, 20, "town"),
        "realm_space_id": str(realm_space_id),
        "realm_name": clip_text(realm_name, 48, str(realm_space_id)),
        "player_actor_id": str(player_actor_id or ""),
        "owned_actor_ids": [str(a) for a in (owned_actor_ids or ()) if str(a).strip()],
        "status": "active",
        "reveal_inner_thoughts": bool(reveal_inner_thoughts),
        "event_cursor": int(event_cursor or 0),
        "started_at_min": round(float(started_at_min), 2),
        "last_sim_min": round(float(started_at_min), 2),
        "ledgers": {
            str(k): copy.deepcopy(dict(v))
            for k, v in (ledgers or {}).items()
            if isinstance(v, Mapping)
        },
        "threads": [],
        "chronicle_archive": [],
        "paused": bool(paused),
        "linked_history_path": str(linked_history_path or ""),
        "linked_conversation_id": str(linked_conversation_id or ""),
        "turn_count": max(0, int(turn_count)),
        "revision": max(0, int(revision)),
        "director_whispers": copy.deepcopy(list(director_whispers or ())),
        "turn_checkpoints": copy.deepcopy(list(turn_checkpoints or ())[-24:]),
        "scene_context": copy.deepcopy(dict(scene_context or {})),
        "created_at": now,
        "updated_at": now,
    }


def redact_fate_session_for_ui(session: Mapping[str, Any]) -> dict[str, Any]:
    """按 Decision 3.A 脱敏返回给前端：默认隐藏未收束丝线中的角色内心独白。"""
    out = copy.deepcopy({
        key: value for key, value in session.items()
        if key not in {"turn_checkpoints", "dialogue_checkpoint_base", "dialogue_history_cursor",
                       "dialogue_turn_signatures"}
    })
    reveal_all = bool(out.get("reveal_inner_thoughts", False))
    threads = out.get("threads") or []
    resolved_ids = {
        str(t.get("thread_id") or "")
        for t in threads
        if isinstance(t, Mapping) and t.get("status") == "resolved"
    }

    for thread in threads:
        if not isinstance(thread, dict):
            continue
        is_resolved = thread.get("status") == "resolved"
        for node in thread.get("nodes") or ():
            if not isinstance(node, dict):
                continue
            node.pop("dialogue_focus", None)
            p_choices = node.get("participant_choices") or {}
            if not isinstance(p_choices, dict):
                continue
            for rec in p_choices.values():
                if not isinstance(rec, dict):
                    continue
                if not reveal_all and not is_resolved and rec.get("inner_reason"):
                    rec["inner_reason"] = ""
                    rec["inner_hidden"] = True
                else:
                    rec["inner_hidden"] = False

    ledgers = out.get("ledgers") or {}
    if isinstance(ledgers, dict):
        for ledger in ledgers.values():
            if not isinstance(ledger, dict):
                continue
            for rec in ledger.get("choice_records") or ():
                if not isinstance(rec, dict):
                    continue
                in_resolved = str(rec.get("thread_id") or "") in resolved_ids
                if not reveal_all and not in_resolved and rec.get("inner_reason"):
                    rec["inner_reason"] = ""
                    rec["inner_hidden"] = True
                else:
                    rec["inner_hidden"] = False

    return out
