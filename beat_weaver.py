"""滚动式命运编排器：随角色真实选择实时织出下一节点、世界异常与章节纪事。"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .adapters.background_adapter import (
    BACKGROUND_SCENE_KEYS, background_scene_fields, match_catalog_scene, resolve_catalog_scene,
)
from .adapters.llm_adapter import ask_llm_json
from .character_chronicle import active_dialogue_beat
from .fate_schema import (
    MAX_ORGANIC_NODES,
    PHASE_LABELS,
    STANCE_LABELS,
    THEME_COMPASS_LABELS,
    VALID_THEME_COMPASSES,
    VALID_THREAD_PHASES,
    clip_text,
    make_fate_thread,
    make_story_node,
    normalize_dialogue_focus,
    normalize_max_nodes,
    redact_fate_session_for_ui,
)


def _ask_dialogue_model(system: str, context: Any, llm_fn) -> Mapping[str, Any] | None:
    try:
        raw = (llm_fn or ask_llm_json)(system, json.dumps(context, ensure_ascii=False))
    except Exception:
        return None
    return raw if isinstance(raw, Mapping) else None


def _available_backgrounds(scenes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"index": scene["index"], "label": scene["label"]} for scene in scenes]


def preview_dialogue_opening(
    *, title: str = "", theme_compass: str = "auto", outline: str = "",
    characters: Sequence[str] = (), recent_chat_turns: Sequence[Any] = (),
    custom_draft: Mapping[str, Any] | None = None, experience_mode: str = "co_play",
    use_llm: bool = True, llm_fn=None,
    background_catalog: Mapping[str, Any] | None = None,
    opening_scene_index: int = 0, allow_scene_transitions: bool = True,
) -> dict[str, Any]:
    """Prepare editable scene framing with one private stake per character."""
    title = clip_text(title, 56, "未命名对话剧本")
    is_observer = str(experience_mode or "").strip().lower() == "observer"
    recent = [
        clip_text(row.get("text") or row.get("content") if isinstance(row, Mapping) else row, 240)
        for row in list(recent_chat_turns)[-10:]
        if not isinstance(row, Mapping) or row.get("role") in {"user", "assistant", "rendered"}
    ]
    theme = theme_compass if theme_compass in VALID_THEME_COMPASSES else "bond_turning"
    anchor = next((row for row in reversed(recent) if row), "")
    default_sit = "在场众人各自怀揣心事，气氛悄然绷紧，故事正要展开。" if is_observer else "你们有了片刻独处的时间，正等着看对方想说什么。"
    situation = clip_text(outline, 240, default_sit)
    if anchor:
        situation = f"谈话刚触及「{clip_text(anchor, 75)}」。{situation}"
    draft = {
        "title": title, "theme_compass": theme,
        "premise": clip_text(
            outline, 240,
            "一场汇聚在场角色的交谈悄然展开，各自的心事即将浮出水面。" if is_observer else "一场对话让彼此有机会说出一直搁在心里的事。",
        ),
        "public_incident_fact": "在场的人正在交谈，话题仍有继续的余地。",
        "scene_name": "谈话现场", "atmosphere": "让情绪随着彼此的反应慢慢浮现",
        "node1": {
            "situation": situation,
            "dilemma_frame": "在场角色是选择主动试探彼此，还是暂时保留自己的想法？" if is_observer else "是顺着话题问下去，还是先给彼此一点空间？",
        },
        "character_roles": {name: {"local_role": "谈话的参与者", "relationship_to_incident": ""}
                            for name in characters},
    }
    scenes = (background_catalog or {}).get("scenes") or ()
    pinned_scene = resolve_catalog_scene(scenes, scene_index=opening_scene_index)
    model = None
    if use_llm and custom_draft is None:
        system_prompt = (
            "为主对话准备开场。仅返回 JSON {premise, public_incident_fact, scene_name, atmosphere,"
            "node1:{situation,dilemma_frame}, character_roles:{角色名:{local_role,relationship_to_incident}}}。"
            "从用户剧本切入。已有聊天只是素材，禁止把未说的私事写进公开局势。"
            "character_roles 的关系和秘密只供对应角色知道。写处境和两难，避免可照念的台词。"
        )
        if is_observer:
            system_prompt += "当前为纯观剧模式，玩家不在场。开局梗概、公开事实、处境与两难只能围绕在场角色之间展开，禁止出现「你」或玩家参与。"
        context = {"title": title, "theme_compass": theme, "outline": outline, "characters": list(characters),
                   "experience_mode": "observer" if is_observer else "co_play", "recent_chat_turns": recent}
        if scenes:
            context["available_backgrounds"] = _available_backgrounds(scenes)
            system_prompt += "严格从 available_backgrounds 选择开场背景，输出 background_scene_index 及其对应的准确 scene_name。"
            if pinned_scene is not None:
                context["pinned_scene"] = {"index": pinned_scene["index"], "label": pinned_scene["label"]}
                system_prompt += "指定了 pinned_scene 时必须使用该场景。"
            if not allow_scene_transitions:
                system_prompt += "本篇章保持在开场场景。"
        model = _ask_dialogue_model(system_prompt, context, llm_fn)
    edits = custom_draft if isinstance(custom_draft, Mapping) else model or {}
    for key, limit in (("premise", 240), ("public_incident_fact", 260), ("scene_name", 80), ("atmosphere", 160)):
        if key in edits:
            draft[key] = clip_text(edits[key], limit)
    node_edits = edits.get("node1")
    if isinstance(node_edits, Mapping):
        for key, limit in (("situation", 320), ("dilemma_frame", 240)):
            if key in node_edits:
                draft["node1"][key] = clip_text(node_edits[key], limit)
    roles = edits.get("character_roles")
    if isinstance(roles, Mapping):
        for name in characters:
            role = roles.get(name)
            if isinstance(role, Mapping):
                for key, limit in (("local_role", 36), ("relationship_to_incident", 120)):
                    if key in role:
                        draft["character_roles"][name][key] = clip_text(role[key], limit)
    if background_catalog and scenes:
        chosen = (pinned_scene or resolve_catalog_scene(
            scenes, scene_index=edits.get("background_scene_index", 0), scene_name=edits.get("scene_name", ""),
        ) or match_catalog_scene(scenes, f"{outline}\n{title}"))
        if chosen is not None:
            draft.update(background_scene_fields(background_catalog["name"], chosen))
    return draft


def weave_dialogue_opening(
    session: Mapping[str, Any], *, title: str = "", theme_compass: str = "auto",
    outline: str = "", characters: Sequence[str] = (), max_nodes: int = 0,
    recent_chat_turns: Sequence[Any] = (), custom_draft: Mapping[str, Any] | None = None,
    experience_mode: str = "", use_llm: bool = True, llm_fn=None,
    background_catalog: Mapping[str, Any] | None = None,
    opening_scene_index: int = 0, allow_scene_transitions: bool = True,
) -> dict[str, Any]:
    cast = {
        row["host_character"]: aid for aid, row in session.get("ledgers", {}).items()
        if row.get("host_character")
    }
    characters = list(characters or cast)
    exp_mode = experience_mode or str(session.get("experience_mode") or "co_play")
    draft = preview_dialogue_opening(
        title=title, theme_compass=theme_compass, outline=outline, characters=characters,
        recent_chat_turns=recent_chat_turns, custom_draft=custom_draft,
        experience_mode=exp_mode, use_llm=use_llm, llm_fn=llm_fn,
        background_catalog=background_catalog, opening_scene_index=opening_scene_index,
        allow_scene_transitions=allow_scene_transitions,
    )
    is_observer = str(exp_mode).strip().lower() == "observer"
    default_sit = "在场众人各自怀揣心事，故事正悄然展开。" if is_observer else "你们有了片刻安静交谈的时间。"
    default_dil = "是直接试探彼此的想法，还是先按捺不语？" if is_observer else "是继续聊下去，还是先听听对方想说什么？"
    node = make_story_node(
        title=draft["scene_name"] or "开场", scene_name=draft["scene_name"], atmosphere=draft["atmosphere"],
        situation=draft["node1"]["situation"] or default_sit,
        dilemma_frame=draft["node1"]["dilemma_frame"] or default_dil,
    )
    if "background_scene_index" in draft:
        node.update({key: draft[key] for key in (*BACKGROUND_SCENE_KEYS, "scene_name")})
    roles = {cast[name]: role for name, role in draft["character_roles"].items() if name in cast}
    return make_fate_thread(
        title=draft["title"], premise=draft["premise"], theme_compass=draft["theme_compass"],
        public_incident_fact=draft["public_incident_fact"], resident_roles=roles,
        lead_actor_ids=list(roles), core_actor_ids=list(roles), nodes=[node], max_nodes=max_nodes, min_nodes=1,
    )


_EXPLICIT_SCENE_SHIFT = re.compile(
    r"(?:换个场景|跳到(?:第二天|次日|下一幕|晚上|明天)|转到下一幕|推进(?:到)?下一幕|"
    r"结束(?:这段|这个)场景|我们(?:换个地方|去别处)|let.s (?:move on|change scenes)|skip to|end (?:the|this) scene)",
    re.IGNORECASE,
)


def evaluate_and_weave_dialogue_beat(
    session: Mapping[str, Any], *, force_evaluate: bool = False, strong_shift: bool = False,
    use_llm: bool = True, llm_fn=None,
    background_catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    thread, node = active_dialogue_beat(session)
    if not node:
        return {"action": "continue_scene"}
    # Shared scene framing can rely only on lines heard by the entire chapter cast.
    cast = {aid for aid, row in session["ledgers"].items() if not row.get("is_player")}
    all_lines = node.get("recent_dialogue_lines", [])
    lines = {row["line_id"]: row for row in all_lines if cast.issubset(row["audience"])}
    is_observer = session.get("experience_mode") == "observer"
    player_id = str(session.get("player_actor_id") or "")
    if not is_observer and player_id:
        last_player = next((row for row in reversed(all_lines)
                            if row["actor_id"] == player_id), {})
        strong_shift = strong_shift or bool(
            _EXPLICIT_SCENE_SHIFT.search(last_player.get("speech", ""))
        )
    turns = int(node.get("turns_in_node", 0))
    if not (force_evaluate or strong_shift) and turns < int(node.get("min_turns_before_advance", 2)):
        return {"action": "continue_scene"}

    seq = int(node["seq"])
    limit = thread.get("max_nodes") or MAX_ORGANIC_NODES
    scene_context = session.get("scene_context") or {}
    builtin_background = scene_context.get("background_mode") == "shinsekai"
    scenes = ((background_catalog or {}).get("scenes") or ()) if builtin_background else ()
    allow_shift = bool(scene_context.get("allow_scene_transitions", True))
    raw = None
    if use_llm and lines:
        eval_prompt = (
            "根据真实对话推演。只输出 JSON {action,line_ids,revealed_facts,player_stance,"
            "next_node:{scene_name,atmosphere,situation,dilemma_frame}}。"
            "action 用 continue_scene/next_node/resolve；后两种必须引用真实角色发言的 line_ids。"
            "每条 revealed_facts 用 {line_ids,quote}，quote 必须是原话，事实依发言人的说法记录。"
            "player_stance 可选 {line_ids,quote}，只能引用玩家原话。"
            "编排大纲可提示未来方向，里面的秘密与计划不属于已知事实。"
            "下一幕只写当下可面对的处境和选择，不能宣称发生了台词中没有说出的事。"
        )
        if is_observer:
            eval_prompt += "当前为纯观剧模式，玩家不在场，不要输出 player_stance，仅根据在场角色之间的真实发言推进下一幕或收束篇章。"
        context = {"title": thread["title"], "public_incident_fact": thread.get("public_incident_fact"),
                   "theme_compass": thread["theme_compass"], "chapter_outline": session.get("dialogue_preset", {}).get("outline"),
                   "experience_mode": "observer" if is_observer else "co_play",
                   "situation": node["situation"], "dilemma_frame": node["dilemma_frame"],
                   "chapter_limit": limit, "current_beat": seq, "strong_shift": strong_shift,
                   "transcript": [{k: row[k] for k in ("line_id", "character_name", "speech")} for row in lines.values()]}
        if scenes and allow_shift:
            context.update(available_backgrounds=_available_backgrounds(scenes),
                           current_scene_index=node.get("background_scene_index", 0))
            eval_prompt += "如需转场，只能用 next_node.background_scene_index 指定 available_backgrounds 中的编号，场景名由该编号决定。"
        elif builtin_background:
            context["current_scene_name"] = node.get("scene_name", "")
            eval_prompt += "下一幕沿用当前内置背景场景。"
        raw = _ask_dialogue_model(eval_prompt, context, llm_fn)
    if raw is None and not thread.get("max_nodes"):
        limit = 3
    default_action = "resolve" if seq >= limit else "next_node"

    def verified_ids(value: Any) -> list[str]:
        if not isinstance(value, list) or not value or any(not isinstance(ref, str) or ref not in lines for ref in value):
            return []
        return list(dict.fromkeys(value))

    def grounded_quote(value: Any, *, player_only: bool = False) -> tuple[str, list[str]]:
        if not isinstance(value, Mapping):
            return "", []
        refs = verified_ids(value.get("line_ids"))
        quote = value.get("quote")
        if isinstance(quote, str) and quote.strip():
            for ref in refs:
                line = lines[ref]
                if (not player_only or (player_id and line["actor_id"] == player_id)) and quote in line["speech"]:
                    return f"{line['character_name']}：「{line['speech']}」", [ref]
        return "", []

    refs = verified_ids(raw.get("line_ids")) if raw else []
    action = raw.get("action") if raw else default_action
    if not lines and not (force_evaluate or strong_shift):
        # The pacing still applies when only part of the cast is present. Advance
        # with neutral scene framing and omit claims about private exchanges.
        action = default_action if all_lines else "continue_scene"
    elif raw and (action not in ("continue_scene", "next_node", "resolve")
                  or (action != "continue_scene" and not any(
                      lines[ref]["actor_id"] != player_id for ref in refs))):
        action = "continue_scene"
    if force_evaluate and action == "continue_scene":
        action, refs, raw = default_action, [], None
    if action != "continue_scene" and seq >= limit:
        action = "resolve"
    if raw is None and action != "continue_scene":
        refs = list(lines)[-2:]
    evidence = [f"{lines[ref]['character_name']}：「{clip_text(lines[ref]['speech'], 160)}」" for ref in refs]
    facts = raw.get("revealed_facts") if raw else None
    fact_rows = [grounded_quote(row) for row in facts[:12]] if isinstance(facts, list) else []
    stance, _ = ("", []) if is_observer else (grounded_quote(raw.get("player_stance"), player_only=True) if raw else ("", []))
    result = {
        "action": action, "line_ids": refs, "revealed_facts": [quote for quote, _ in fact_rows if quote],
        "player_stance_note": stance, "outcome_summary": " / ".join(evidence),
    }
    if action == "next_node":
        framing = raw.get("next_node") if raw else None
        framing = framing if isinstance(framing, Mapping) else {}
        scene_fields = {}
        if builtin_background and (scenes or "background_scene_index" in node):
            chosen = resolve_catalog_scene(scenes, scene_index=framing.get("background_scene_index", 0)) if allow_shift else None
            if chosen is not None and background_catalog is not None:
                scene_fields = background_scene_fields(background_catalog["name"], chosen)
            else:
                scene_fields = {key: node[key] for key in (*BACKGROUND_SCENE_KEYS, "scene_name") if key in node}
        scene_name = scene_fields.get("scene_name", framing.get("scene_name") or node.get("scene_name", ""))
        result["next_node"] = make_story_node(
            seq=seq + 1, parent_node_id=node["node_id"],
            phase="entangle" if seq == 1 else "crossroads" if seq < 3 else "ripple",
            title=scene_name or "谈话继续",
            scene_name=scene_name,
            atmosphere=framing.get("atmosphere") or node.get("atmosphere", ""),
            situation=framing.get("situation") or "对话留出了新的空隙。眼下可以沿着仍值得谈的地方继续，也可以把注意力放到彼此接下来想做的事上。",
            dilemma_frame=framing.get("dilemma_frame") or "继续追问，还是提出一个双方都愿意尝试的下一步？",
            min_turns_before_advance=node.get("min_turns_before_advance", 2),
        )
        result["next_node"].update(scene_fields)
    elif action == "resolve":
        # Retrospective prose is assembled from the transcript, never from the
        # planner's guesses about an unspoken confession or promise.
        char_eps = {}
        for aid in sorted(cast):
            last = next((row for prior in reversed(thread["nodes"])
                         for row in reversed(prior["recent_dialogue_lines"]) if row["actor_id"] == aid), None)
            if last:
                char_eps[aid] = f"在这段谈话中说过：「{clip_text(last['speech'], 160)}」"
        key_turns = [prior["outcome_summary"] for prior in thread["nodes"] if prior["outcome_summary"]] + evidence
        result["chapter_chronicle"] = {
            "title": thread["title"], "epilogue": " / ".join((evidence or key_turns)[-2:]) or "这段谈话在此告一段落。",
            "key_turns": key_turns[-12:], "character_epilogues": char_eps,
        }
    return result


def _spaces_index(world_state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(sp.get("space_id") or ""): sp
        for sp in (world_state.get("spaces") or ())
        if isinstance(sp, Mapping) and sp.get("space_id")
    }


def _describe_space(sp: Mapping[str, Any] | None, zone: Mapping[str, Any] | None = None) -> dict[str, str]:
    if not isinstance(sp, Mapping):
        return {
            "space_id": "",
            "space_name": "小镇广场",
            "floor_label": "",
            "zone_id": "",
            "zone_name": "",
            "space_desc": "小镇",
        }
    sid = str(sp.get("space_id") or "")
    sname = str(sp.get("name") or sid)
    fl = str(sp.get("floor_label") or "").strip()
    zid = str((zone or {}).get("zone_id") or "")
    zname = str((zone or {}).get("name") or "").strip()
    base = f"{sname}({fl})" if fl and fl not in sname else sname
    desc = f"{base}·{zname}" if zname else base
    return {
        "space_id": sid,
        "space_name": sname,
        "floor_label": fl,
        "zone_id": zid,
        "zone_name": zname,
        "space_desc": desc,
    }


_PLACE_HINT_TOKENS: tuple[str, ...] = (
    "钟楼", "旅馆", "老宅", "图书馆", "咖啡", "阁楼", "地下", "窖", "密室",
    "教堂", "工坊", "茶室", "宿舍", "书房", "厨房", "仓库", "医院", "诊所",
    "酒吧", "剧场", "剧院", "木屋", "客栈", "食堂", "灶间",
)

_MYSTERY_RE = re.compile(r"(?:杀人|凶手|尸体|命案|谋杀|嫌疑|推理|失踪|密室|诡异|秘密|谜团|调查|血迹|受害|遇害|死亡|毒杀|侦探|凶案|案)")
_CRIME_RE = re.compile(r"(?:杀人|凶手|尸体|命案|谋杀|嫌疑|毒杀|凶案|受害|遇害|尸)")
_CRISIS_RE = re.compile(r"(?:暴雨|停电|风暴|火灾|危机|逃生|被困|洪水|地震|暴雪|警报|灾难|塌方)")
_BOND_RE = re.compile(r"(?:重逢|表白|误会|友情|羁绊|约定|告别|暗恋|争吵|和解|愧疚|心结)")
_ASPIRATION_RE = re.compile(r"(?:心愿|梦想|开店|演出|祭典|聚会|比赛|筹备|搭建)")


def _two_char_overlap(a: str, b: str) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    for idx in range(len(a) - 1):
        sub = a[idx : idx + 2]
        if sub in b:
            return True
    return False


def _pick_stage_anchor(
    world_state: Mapping[str, Any],
    *,
    avoid_space_id: str = "",
    avoid_space_ids: Sequence[str] = (),
    prefer_secondary_floor: bool = True,
    hint_text: str = "",
) -> dict[str, str]:
    spaces = [
        sp for sp in (world_state.get("spaces") or ())
        if isinstance(sp, Mapping) and sp.get("space_id")
    ]
    if not spaces:
        return _describe_space(None)

    realm_id = str(world_state.get("realm_space_id") or "")
    interior_children = [
        sp for sp in spaces
        if sp.get("space_id") != realm_id and sp.get("space_kind") != "town"
    ]
    non_town = [sp for sp in spaces if sp.get("space_kind") != "town"]
    pool = interior_children or non_town or spaces

    clean_hint = str(hint_text or "").strip()
    zone_hint_scores: dict[str, tuple[int, Mapping[str, Any] | None]] = {}
    if clean_hint:
        hint_tokens = [tok for tok in _PLACE_HINT_TOKENS if tok in clean_hint]
        scored_spaces: dict[str, int] = {}
        for sp in pool:
            sid = str(sp.get("space_id") or "")
            sname = str(sp.get("name") or "")
            base_name = re.sub(r"\s*(?:1F|2F|B1)$", "", sname).strip()
            score = 0
            for tok in hint_tokens:
                if tok in sname:
                    score += 6
            if base_name and _two_char_overlap(base_name, clean_hint):
                score += 2
            best_z: Mapping[str, Any] | None = None
            best_zs = 0
            for z in sp.get("zones") or ():
                if not isinstance(z, Mapping):
                    continue
                zname = str(z.get("name") or "")
                zs = sum(4 for tok in hint_tokens if tok in zname)
                if not zs and len(zname) >= 2 and _two_char_overlap(zname, clean_hint):
                    zs = 2
                if zs > best_zs:
                    best_zs = zs
                    best_z = z
            score += best_zs
            scored_spaces[sid] = score
            if best_z is not None:
                zone_hint_scores[sid] = (best_zs, best_z)
        max_match = max(scored_spaces.values(), default=0)
        if max_match > 0:
            matched_pool = [sp for sp in pool if scored_spaces.get(str(sp.get("space_id") or ""), 0) == max_match]
            if matched_pool:
                pool = matched_pool

    blocked = {str(x) for x in avoid_space_ids if str(x)}
    if avoid_space_id:
        blocked.add(avoid_space_id)
    if blocked and len(pool) > 1:
        fresh = [sp for sp in pool if sp.get("space_id") not in blocked]
        if fresh:
            pool = fresh
        elif avoid_space_id:
            alt = [sp for sp in pool if sp.get("space_id") != avoid_space_id]
            if alt:
                pool = alt

    def _rank(sp: Mapping[str, Any]) -> tuple[int, int, int]:
        fi = int(sp.get("floor_index") or 0)
        zones = [z for z in (sp.get("zones") or ()) if isinstance(z, Mapping)]
        enc_count = sum(1 for z in zones if z.get("enclosed"))
        if prefer_secondary_floor:
            floor_score = 3 if fi != 0 else (2 if enc_count > 0 else 1)
        else:
            floor_score = 2 if sp.get("space_kind") != "town" else 1
        return (floor_score, enc_count, len(zones))

    chosen_sp = max(pool, key=_rank)
    chosen_sid = str(chosen_sp.get("space_id") or "")
    zones = [z for z in (chosen_sp.get("zones") or ()) if isinstance(z, Mapping)]
    hinted_zone = (zone_hint_scores.get(chosen_sid) or (0, None))[1]
    enclosed_zones = [z for z in zones if z.get("enclosed")]
    chosen_zone = hinted_zone or (enclosed_zones[0] if enclosed_zones else (zones[0] if zones else None))
    return _describe_space(chosen_sp, chosen_zone)


def _pick_theme_compass(
    requested: str,
    session: Mapping[str, Any],
    focus_actor_ids: Sequence[str] | None,
    *,
    title: str = "",
    prompt_hint: str = "",
) -> str:
    clean = str(requested or "auto").strip().lower()
    if clean in VALID_THEME_COMPASSES:
        return clean

    combined = f"{title} {prompt_hint}".strip()
    if combined:
        if _MYSTERY_RE.search(combined):
            return "mystery_ripple"
        if _CRISIS_RE.search(combined):
            return "crisis_test"
        if _BOND_RE.search(combined):
            return "bond_turning"
        if _ASPIRATION_RE.search(combined):
            return "daily_aspiration"

    ledgers = session.get("ledgers") or {}
    targets = [
        ledgers[aid] for aid in (focus_actor_ids or ledgers.keys())
        if aid in ledgers and not ledgers[aid].get("is_player")
    ]
    used = Counter(str(t.get("theme_compass") or "") for t in (session.get("threads") or ()))

    # 结合在场角色人格均值与历史频率轮换，兼顾贴合与新鲜感
    avg_related = sum(float((t.get("traits_summary") or {}).get("relatedness_need", 0.6)) for t in targets) / max(1, len(targets))
    avg_comp = sum(float((t.get("traits_summary") or {}).get("competence_need", 0.6)) for t in targets) / max(1, len(targets))
    avg_safety = sum(float((t.get("traits_summary") or {}).get("safety_need", 0.5)) for t in targets) / max(1, len(targets))

    scores = {
        "daily_aspiration": 0.6 + avg_comp * 0.4 - used["daily_aspiration"] * 0.35,
        "bond_turning": 0.58 + avg_related * 0.42 - used["bond_turning"] * 0.35,
        "mystery_ripple": 0.56 + avg_comp * 0.35 - used["mystery_ripple"] * 0.35,
        "crisis_test": 0.45 + avg_safety * 0.30 + avg_related * 0.20 - used["crisis_test"] * 0.35,
    }
    return max(VALID_THEME_COMPASSES, key=lambda k: scores[k])


def _ensure_interior_sid(
    raw_sid: Any,
    spaces: Mapping[str, Mapping[str, Any]],
    world_state: Mapping[str, Any],
    fallback_sid: str,
) -> str:
    realm_id = str(world_state.get("realm_space_id") or "")
    interior_sids = {
        sid for sid, sp in spaces.items()
        if sid != realm_id and sp.get("space_kind") != "town"
    }
    sid = str(raw_sid or "").strip()
    if sid in spaces and (not interior_sids or sid in interior_sids):
        return sid
    if fallback_sid in spaces and (not interior_sids or fallback_sid in interior_sids):
        return fallback_sid
    return next(iter(interior_sids or spaces), "")


def _validate_world_setup(
    raw_setup: Any,
    world_state: Mapping[str, Any],
    fallback_anchor: Mapping[str, str],
) -> dict[str, Any]:
    src = raw_setup if isinstance(raw_setup, Mapping) else {}
    spaces = _spaces_index(world_state)

    def _safe_space_and_zone(raw_sid: Any, raw_zid: Any) -> tuple[str, str]:
        sid = _ensure_interior_sid(raw_sid, spaces, world_state, str(fallback_anchor.get("space_id") or ""))
        sp = spaces.get(sid) or {}
        zones = [z for z in (sp.get("zones") or ()) if isinstance(z, Mapping)]
        zid = str(raw_zid or "").strip()
        if zid and not any(str(z.get("zone_id") or "") == zid for z in zones):
            zid = ""
        if not zid and sid == fallback_anchor.get("space_id"):
            zid = str(fallback_anchor.get("zone_id") or "")
        return sid, zid

    out: dict[str, Any] = {}
    raw_props: list[Mapping[str, Any]] = []
    if isinstance(src.get("spawn_props"), Sequence) and not isinstance(src.get("spawn_props"), (str, bytes)):
        raw_props.extend(p for p in src["spawn_props"] if isinstance(p, Mapping))
    if isinstance(src.get("spawn_prop"), Mapping):
        raw_props.append(src["spawn_prop"])

    clean_props: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for spawn in raw_props:
        if not spawn.get("name"):
            continue
        psid, pzid = _safe_space_and_zone(spawn.get("space_id"), spawn.get("zone_id"))
        if not psid:
            continue
        pname = clip_text(spawn.get("name"), 14, "现场遗留物")
        if pname in seen_names:
            continue
        seen_names.add(pname)
        clean_props.append({
            "space_id": psid,
            "zone_id": pzid,
            "name": pname,
            "visual_prompt": clip_text(spawn.get("visual_prompt"), 140, pname),
            "brief": clip_text(spawn.get("brief"), 80, "放在室内的关键物件"),
            "detail": clip_text(spawn.get("detail"), 220, "近看能发现上面留着清晰的细节痕迹。"),
            "collectible": bool(spawn.get("collectible", False)),
            "traits_hint": dict(spawn.get("traits_hint") or {"inscribable": True}),
        })
        if len(clean_props) >= 3:
            break
    if clean_props:
        out["spawn_props"] = clean_props
        out["spawn_prop"] = clean_props[0]

    cue = src.get("ambient_cue")
    if isinstance(cue, Mapping) and cue.get("text"):
        csid, czid = _safe_space_and_zone(cue.get("space_id"), cue.get("zone_id"))
        if csid:
            out["ambient_cue"] = {
                "space_id": csid,
                "zone_id": czid,
                "text": clip_text(cue.get("text"), 96),
                "cue_kind": clip_text(cue.get("cue_kind"), 20, "sound"),
            }

    wmode = str(src.get("weather_mode") or "").strip().lower()
    if wmode in ("clear", "cloudy", "rain", "fog", "snow"):
        out["weather_mode"] = wmode
    return out


def _infer_local_role_title(home_name: str, idx: int) -> str:
    clean = str(home_name or "")
    if "钟楼" in clean:
        return "守钟人与机械维护"
    if "旅馆" in clean or "客栈" in clean or "汤宿" in clean:
        return "旅馆经营与前台记账"
    if "老宅" in clean or "公馆" in clean:
        return "老宅看守"
    if "图书" in clean or "书房" in clean:
        return "馆藏整理员"
    if "咖啡" in clean or "茶" in clean:
        return "店面主人"
    if "工坊" in clean:
        return "驻镇手艺人"
    fallback = ("镇上值夜人", "熟客店东", "账房老住户", "作坊匠人")
    return fallback[idx % len(fallback)]


def _build_resident_roles_offline(
    session: Mapping[str, Any],
    anchor: Mapping[str, str],
    theme: str,
    *,
    is_crime: bool = False,
) -> tuple[dict[str, dict[str, str]], str]:
    ledgers = session.get("ledgers") or {}
    desc = anchor.get("space_desc") or "现场"
    crime_stakes = (
        f"昨夜曾上过{desc}检查门窗却发现锁痕不对，说不清当时避雨的是谁",
        f"当晚看见熟悉的人急匆匆离开{desc}附近，担心无辜熟人遭牵连",
        f"手中留着记录当晚借还钥匙与进出的账册，其中几行字迹在看清人心前不愿轻易交出",
        f"发现{desc}遗落的证物中似有来自自家熟悉角落的工具或银扣，唯恐引火烧身",
    )
    general_stakes = (
        f"熟悉{desc}一带老规矩，一眼看出眼下的物件与往日不同",
        "对街坊旧情看得很重，第一反应是先顾全熟人的体面与安危",
        "一向低调谨慎，知道不少镇上陈年旧事，却不打算随口全盘托出",
        "认出了关键旧物背后的老手艺或出处，觉得这事不会只是巧合",
    )
    pool = crime_stakes if is_crime else general_stakes
    roles: dict[str, dict[str, str]] = {}
    native_idx = 0
    for aid, lg in ledgers.items():
        if lg.get("is_player") or not lg.get("is_native_npc"):
            continue
        sr = lg.get("story_role") if isinstance(lg.get("story_role"), Mapping) else {}
        home_name = str(sr.get("home_space_name") or "")
        roles[aid] = {
            "local_role": str(sr.get("local_role") or "") or _infer_local_role_title(home_name, native_idx),
            "relationship_to_incident": pool[native_idx % len(pool)],
        }
        native_idx += 1

    visitor_framing = (
        f"因骤雨或远行暂留本镇的外来旅人，与本镇居民互不相识，在{desc}发生这桩事件后各自凭本心与观察做出选择。"
        if theme in ("mystery_ripple", "crisis_test")
        else "因旅途暂留本镇的客人，原本只是旁观小镇日常，却在机缘之下被卷进眼前的故事丝线。"
    )
    return roles, visitor_framing


def _opening_lead_actor_ids(
    session: Mapping[str, Any],
    world_state: Mapping[str, Any],
    focus_actor_ids: Sequence[str] | None = None,
    *,
    title: str = "",
    prompt_hint: str = "",
) -> list[str]:
    ledgers = session.get("ledgers") or {}
    actors = {
        str(a.get("actor_id") or ""): a
        for a in (world_state.get("actors") or ())
        if isinstance(a, Mapping) and a.get("actor_id")
    }
    blob = f"{title} {prompt_hint}".strip()
    leads: list[str] = []
    seen: set[str] = set()

    def _add(aid: str) -> None:
        if not aid or aid in seen:
            return
        lg = ledgers.get(aid)
        if not isinstance(lg, Mapping) or lg.get("is_player"):
            return
        act = actors.get(aid)
        if act is not None and act.get("actor_status", "active") != "active":
            return
        seen.add(aid)
        leads.append(aid)

    for aid in (focus_actor_ids or ()):
        _add(str(aid))
    for aid, lg in ledgers.items():
        if isinstance(lg, Mapping) and not lg.get("is_native_npc"):
            _add(str(aid))
    for aid, lg in ledgers.items():
        if isinstance(lg, Mapping):
            nm = str(lg.get("name") or "").strip()
            if nm and nm in blob:
                _add(str(aid))
    return leads


def _weave_opening_offline(
    session: Mapping[str, Any],
    world_state: Mapping[str, Any],
    theme: str,
    anchor: Mapping[str, str],
    cast_stakes: Mapping[str, Mapping[str, Any]],
    prompt_hint: str,
    sim_min: float,
    *,
    custom_title: str = "",
    max_nodes: int = 0,
    lead_actor_ids: Sequence[str] = (),
) -> dict[str, Any]:
    desc = anchor["space_desc"]
    sid = anchor["space_id"]
    zid = anchor["zone_id"]
    is_crime = bool(_CRIME_RE.search(f"{custom_title} {prompt_hint}"))
    hint_note = f"（顺着「{clip_text(prompt_hint, 48)}」展开）" if prompt_hint else ""
    actors = [a for a in (world_state.get("actors") or ()) if not a.get("is_player") and a.get("actor_status", "active") == "active"]
    lead_name = str(actors[0].get("name") or "她") if actors else "居民"

    extra_props: list[dict[str, Any]] = []
    if theme == "daily_aspiration":
        title = clip_text(custom_title or prompt_hint, 36) or f"{anchor['space_name']}的未完心愿单"
        public_fact = f"一本夹着旧草图与愿望清单的牛皮小册静静放在{desc}木桌上，里面写着想为小镇添一处温暖聚会的筹备草图。"
        premise = f"{public_fact}{hint_note}"
        node_title = "桌角微亮的念想"
        situation = f"在{desc}的桌案上摆着这本泛黄的牛皮心愿手札，第一页正等待有人真正翻开并决定要不要付诸行动。"
        dilemma = "是把这份小小的念想当成玩笑掠过，还是顶着日常懒散真正迈出第一步去实现或邀人同行？"
        spawn_prop = {
            "space_id": sid, "zone_id": zid, "name": "牛皮心愿手札",
            "visual_prompt": "一本绑着米色细绳的牛皮旧手札，夹角露出一点淡蓝色书签",
            "brief": "桌边放着一本系着细绳的牛皮旧手札",
            "detail": "翻开第一页写着几行清秀的备忘：‘总想找个下雪或落雨的晚上，把二楼角落点上暖灯让大家聚一聚。’",
            "collectible": False, "traits_hint": {"inscribable": True},
        }
        cue_text = f"{desc}的木桌上新放了一本带着淡蓝书签的牛皮手札，摊开在灯光底下。"
        weather_mode = ""

    elif theme == "bond_turning":
        title = clip_text(custom_title or prompt_hint, 36) or f"寄给「{lead_name}」的无署名明信片"
        public_fact = f"一张火漆压角的浅米色手写明信片留在{desc}，上面写着熟人间迟迟未能说出口的心意。"
        premise = f"{public_fact}{hint_note}"
        node_title = "未署名的那句话"
        situation = f"{desc}的桌面放着这张火漆压角的浅米色明信片，字迹克制而诚恳，正落在熟人之间最微妙的心结上。"
        dilemma = "看到这种牵连旁人心意的东西，是直接拿去找在意的人摊开聊聊，还是悄悄替对方守住这段秘密？"
        spawn_prop = {
            "space_id": sid, "zone_id": zid, "name": "压角明信片",
            "visual_prompt": "一张边角略微折起的浅米色手写明信片，背面贴着淡琥珀色火漆印",
            "brief": "平面上摆着一张浅米色硬卡明信片",
            "detail": "背面写着：‘谢谢你之前什么都没问却一直留灯等着。有些话在吵闹的大厅里说不出口，如果今晚还在，我想再试一次。’",
            "collectible": True, "traits_hint": {"inscribable": True},
        }
        cue_text = f"{desc}桌边留着一张贴有琥珀色火漆印的手写明信片。"
        weather_mode = ""

    elif theme == "crisis_test":
        title = clip_text(custom_title or prompt_hint, 36) or f"{anchor['space_name']}停电雨夜的风波"
        public_fact = f"骤急的风雨撞松了{desc}的窗闩，一份被雨水打湿的「沾雨紧急便笺」被送到室内，指出某件重要托付急需当面照应。"
        premise = f"{public_fact}{hint_note}"
        node_title = "雨夜的紧急托付"
        situation = f"暴雨敲打着窗棂，{desc}门内地上落着那份沾湿边角的「沾雨紧急便笺」，上面明确标注着需要马上确认的物件与安全位置。"
        dilemma = "外头风雨渐急、房间里最安稳；是缩回安全区观望，还是提着勇气推门过去照应同伴？"
        spawn_prop = {
            "space_id": sid, "zone_id": zid, "name": "沾雨紧急便笺",
            "visual_prompt": "一张边缘被雨水打湿微皱的折纸便笺，折痕里夹着半截细麻绳",
            "brief": "门边地面落着一张沾湿边角的折叠便笺",
            "detail": "纸面匆匆写着：‘风太大把窗闩刮松了，有一件重要托付还留在楼里，看清纸背标的位置尽快核实。’",
            "collectible": True, "traits_hint": {"inscribable": True},
        }
        cue_text = f"暴雨中{desc}的木窗被风掀开一角，门边地上已经多了一张沾湿的紧急便笺。"
        weather_mode = "rain"

    elif is_crime:
        title = clip_text(custom_title, 40) or clip_text(prompt_hint, 32) or f"{anchor['space_name']}雨夜疑案"
        public_fact = (
            f"一场暴雨过后，有人在{desc}发现了一具身份不明的遇害者遗体，尸体旁遗落着「停摆的黄铜怀表」与「染泥现场勘查签」，"
            "几名镇上居民各自带着微妙的隐情，真相尚未水落石出。"
        )
        premise = f"{public_fact}{hint_note}"
        node_title = "现场停摆的怀表与泥印"
        situation = (
            f"{desc}内确实发生了一起命案：遇害者身旁静静躺着一枚指针停在 22:15 的「停摆的黄铜怀表」，"
            "墙角还压着记录现场第一批脚印与线索的「染泥现场勘查签」。"
        )
        dilemma = "这已不是传闻而是实打实的命案现场：本地住户有人想遮掩自己夜里的行踪、有人想护住熟人，而外来旅人也可亲自查看怀表与勘查签追问真相。"
        spawn_prop = {
            "space_id": sid, "zone_id": zid, "name": "停摆的黄铜怀表",
            "visual_prompt": "一枚表盖摔开、表盘沾着雨水与暗痕的黄铜怀表，指针停在十点十五分",
            "brief": "现场地面血迹旁遗落着一枚摔碎表蒙的黄铜怀表",
            "detail": "指针停在 22:15；表盖内侧刻着细小缩写‘W.K’，表链夹缝缠着一根浸泡过雨水的深色粗麻线。",
            "collectible": True, "traits_hint": {"inscribable": True},
        }
        extra_props = [{
            "space_id": sid, "zone_id": zid, "name": "染泥现场勘查签",
            "visual_prompt": "一张沾了红泥印的米色记录卡片，旁边压着半枚遗失的银色袖扣",
            "brief": "墙边压着记录初步现场发现的硬卡勘查签",
            "detail": "记录着三件明确事实：①门口留有一深一浅两枚雨靴泥印；②22:00前曾听见激烈争执；③遇害者右袖口少了一枚银扣。",
            "collectible": False, "traits_hint": {"inscribable": True},
        }]
        cue_text = f"冷雨敲窗，{desc}已经拉开命案警戒，地面遗留的铜怀表与染泥勘查签正清晰摆在眼前。"
        weather_mode = "rain"

    else:  # mystery_ripple (non-crime)
        title = clip_text(custom_title or prompt_hint, 36) or f"{anchor['space_name']}的深夜来物"
        public_fact = f"原本上锁存放的{desc}内竟多出一只「素面小木盒」，盒盖底下压着一张标有具体时间刻度的手写字条。"
        premise = f"{public_fact}{hint_note}"
        node_title = "桌角的上锁木盒"
        situation = f"在{desc}的墙角安稳放着一只本不该出现在此的「素面小木盒」，木盒表面留着手温，底下露着半张写有时间刻度的字条。"
        dilemma = "物体就在眼前摆着：是亲自过去仔细翻看木盒与字条、找熟识的人核对昨晚谁来过这里，还是暂且按下不说？"
        spawn_prop = {
            "space_id": sid, "zone_id": zid, "name": "素面小木盒",
            "visual_prompt": "一只巴掌大小的深胡桃木素面小方盒，盒盖缝隙透着半截细铜丝",
            "brief": "墙边地上放着一只陌生的深色小木盒",
            "detail": "木盒表面还残留着微弱手温，盒盖底下压着写有‘22:15 后门碰头、旧物别带回’的半张字条。",
            "collectible": False, "traits_hint": {"inscribable": True},
        }
        cue_text = f"{desc}墙角不知何时多出了一只压着字条的深色胡桃木盒。"
        weather_mode = "fog"

    all_props = [spawn_prop, *extra_props]
    world_setup = {
        "spawn_prop": spawn_prop,
        "spawn_props": all_props,
        "ambient_cue": {"space_id": sid, "zone_id": zid, "text": cue_text, "cue_kind": "sound"},
    }
    if weather_mode:
        world_setup["weather_mode"] = weather_mode

    resident_roles, visitor_framing = _build_resident_roles_offline(session, anchor, theme, is_crime=is_crime)

    opening_node = make_story_node(
        seq=1,
        title=node_title,
        phase="spark",
        situation=situation,
        dilemma_frame=dilemma,
        target_space_id=sid,
        target_space_name=anchor["space_name"],
        target_floor_label=anchor["floor_label"],
        target_zone_id=zid,
        target_zone_name=anchor["zone_name"],
        world_setup=world_setup,
        entry_triggers=[
            {"type": "ambient_cue", "space_id": sid},
            {"type": "actor_in_space", "space_id": sid, "zone_id": zid},
            {"type": "sim_time_reached", "at_min": sim_min + 1.0},
        ],
        catalyst_actor_ids=lead_actor_ids,
        created_at_min=sim_min,
        pressure_deadline_min=sim_min + 1.0,
    )
    return make_fate_thread(
        title=clip_text(custom_title, 56) or title,
        premise=premise,
        public_incident_fact=public_fact,
        resident_roles=resident_roles,
        visitor_framing=visitor_framing,
        theme_compass=theme,
        tension=0.28,
        phase="spark",
        cast_stakes=cast_stakes,
        nodes=[opening_node],
        max_nodes=max_nodes,
        lead_actor_ids=lead_actor_ids,
        created_at_min=sim_min,
    )


_REL_STAGE_CN: dict[str, str] = {
    "stranger": "尚不认识",
    "acquainted": "刚认识",
    "acquaintance": "相识",
    "friend": "同伴",
    "close": "挚友",
    "rival": "对手",
    "guarded": "戒备",
    "hostile": "敌对",
}

_REL_STANCE_CN: dict[str, str] = {
    "neutral": "平常",
    "open": "温和开放",
    "warm": "亲近热情",
    "shy": "害羞拘谨",
    "guarded": "疏离戒备",
    "cold": "冷淡",
}


_POSTURE_CN: dict[str, str] = {
    "standing": "站立",
    "sitting": "坐着",
    "resting": "睡着中",
}


def _format_actor_locations_block(
    world_state: Mapping[str, Any],
    ledgers: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    spaces = _spaces_index(world_state)
    lines: list[str] = []
    for act in world_state.get("actors") or ():
        if not isinstance(act, Mapping) or act.get("is_player") or act.get("actor_status", "active") != "active":
            continue
        aid = str(act.get("actor_id") or "")
        nm = str((ledgers or {}).get(aid, {}).get("name") or act.get("name") or aid)
        sid = str(act.get("space_id") or "")
        sp = spaces.get(sid) or {}
        sname = str(sp.get("name") or sid or "小镇")
        fl = str(sp.get("floor_label") or "").strip()
        loc = f"{sname}({fl})" if fl and fl not in sname else sname
        zname = str(act.get("zone_name") or "").strip()
        if zname:
            loc += f"·{zname}"
        posture = _POSTURE_CN.get(str(act.get("posture") or "standing"), "站立")
        lines.append(f"- {nm}({aid})：此刻位于 {loc}（{posture}）")
    return "\n".join(lines[:14])


def _format_cast_relations_block(
    session: Mapping[str, Any],
    cast_relations: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
) -> str:
    if not isinstance(cast_relations, Mapping) or not cast_relations:
        return ""
    ledgers = session.get("ledgers") or {}
    lines: list[str] = []
    for aid, rel_map in cast_relations.items():
        if not isinstance(rel_map, Mapping):
            continue
        lg = ledgers.get(aid) or {}
        if lg.get("is_player"):
            continue
        subj = str(lg.get("name") or aid)
        for tkey, row in rel_map.items():
            if not isinstance(row, Mapping):
                continue
            known = bool(row.get("known", False))
            manual = bool(row.get("manual_override", False))
            if not known and not manual:
                continue
            tname = str(row.get("target_name") or ("用户" if tkey == "user" else tkey.split(":", 1)[-1])).strip()
            if not tname:
                continue
            st = _REL_STAGE_CN.get(str(row.get("rel_stage") or "stranger").lower(), str(row.get("rel_stage") or "相识"))
            sc = _REL_STANCE_CN.get(str(row.get("attitude_stance") or "neutral").lower(), str(row.get("attitude_stance") or "平常"))
            val = float(row.get("affective_valence") or 0.0)
            trust = float(row.get("trust") if row.get("trust") is not None else 0.55)
            extras = [f"好感{val:+.2f}", f"信任{trust:.2f}"]
            if row.get("call_name"):
                extras.append(f"称呼「{row['call_name']}」")
            if row.get("relation_tag"):
                extras.append(f"标签:{row['relation_tag']}")
            if row.get("shared_memories"):
                extras.append(f"印象:{clip_text(row['shared_memories'], 48)}")
            lines.append(f"- {subj} → {tname}：{st}/{sc}（{'，'.join(extras)}）")
            if len(lines) >= 12:
                return "\n".join(lines)
    return "\n".join(lines)


def weave_opening_thread(
    session: Mapping[str, Any],
    world_state: Mapping[str, Any],
    persona_adapter: Any = None,
    *,
    theme_compass: str = "auto",
    prompt_hint: str = "",
    custom_title: str = "",
    max_nodes: int = 0,
    focus_actor_ids: Sequence[str] | None = None,
    cast_relations: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    llm_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """顺着现有小镇地形与在场人格，轻拨一条不预设结局、随关键抉择持续推进的新命运丝线。"""
    eff_theme = _pick_theme_compass(
        theme_compass,
        session,
        focus_actor_ids,
        title=custom_title,
        prompt_hint=prompt_hint,
    )
    anchor = _pick_stage_anchor(
        world_state,
        prefer_secondary_floor=True,
        hint_text=f"{custom_title} {prompt_hint}".strip(),
    )
    is_crime = bool(_CRIME_RE.search(f"{custom_title} {prompt_hint}"))
    sim_min = float(world_state.get("minutes") if world_state.get("minutes") is not None else session.get("last_sim_min") or 0.0)
    eff_max_nodes = normalize_max_nodes(max_nodes, 0)
    lead_actor_ids = _opening_lead_actor_ids(
        session,
        world_state,
        focus_actor_ids,
        title=custom_title,
        prompt_hint=prompt_hint,
    )

    ledgers = session.get("ledgers") or {}
    cast_stakes: dict[str, dict[str, Any]] = {}
    native_lines: list[str] = []
    visitor_lines: list[str] = []
    for aid, ledger in ledgers.items():
        if ledger.get("is_player"):
            continue
        tags = ledger.get("style_tags") or []
        arc = ledger.get("arc_state") or {}
        asps = arc.get("aspirations") or []
        sr = ledger.get("story_role") if isinstance(ledger.get("story_role"), Mapping) else {}
        home_name = str(sr.get("home_space_name") or "")
        stake = (
            f"正惦记着：{asps[-1]}"
            if asps
            else f"性格倾向：{'、'.join(tags[:2]) or '真实而有自己主见'}"
        )
        cast_stakes[aid] = {"name": ledger.get("name") or aid, "stake": stake}
        if ledger.get("is_native_npc"):
            native_lines.append(f"{ledger.get('name') or aid}({aid}, 常驻:{home_name or '本镇'}): {stake}")
        else:
            visitor_lines.append(f"{ledger.get('name') or aid}({aid}, 外来旅人): {stake}")

    offline_roles, offline_visitor_framing = _build_resident_roles_offline(session, anchor, eff_theme, is_crime=is_crime)

    if use_llm:
        caller = llm_fn or (
            lambda sys_p, usr_p: ask_llm_json(sys_p, usr_p, timeout_sec=26.0, max_tokens=2200)
        )
        spaces_desc = [
            f"{sp['space_id']}: {sp.get('name')}({sp.get('floor_label') or '1F'}) 隔间={','.join(z.get('name','') + '[' + z.get('zone_id','') + ']' for z in (sp.get('zones') or ()) if isinstance(z, Mapping)) or '整层'}"
            for sp in (world_state.get("spaces") or ())[:12]
        ]
        prop_rule = (
            "2. 若属于人际羁绊/心愿/表白类故事，聚焦【在场主角与同行角色】眼下具体的人际契机与第一幕抉择（world_setup.spawn_props 可为 [] 或 1 件贴切小物）；"
            "若属于悬疑/危机事件，必须在 world_setup.spawn_props 投放 1~2 件具体可查证物证，严禁写成‘好像有奇怪声音’之类虚浮暗示。\n"
        )
        system_prompt = (
            "你是「命运织机」，负责在活生生的小镇里把故事第一时间推入实处，绝不让角色干等着故事自己发生。\n"
            "硬性原则：\n"
            "1. 紧紧围绕玩家给出的标题/设定与在场角色（尤其是导入角色），把第一幕直接落到当事主角当下面临的具体决定上（例如打算找谁商量、去见谁、或查证何处），绝不预设固定结局；并且严禁把【当前各自在不同位置的角色】硬写成已经站在同一间屋里面对面说完开场白。\n"
            + prop_rule
            + "3. 本镇原生居民是‘生活在本地的事件参与者/知情街坊’，请为每位原生居民分配驻地职业(local_role)与她/他和此事件的当事利害(relationship_to_incident)，严禁把所有原生居民写成莫名其妙跑来破案的外来侦探。\n"
            "仅输出一个合法 JSON 对象，字段：\n"
            "title, premise, public_incident_fact (全镇公知的客观既成事实<=200字), "
            "node_title, situation (第一拍客观处境<=180字), dilemma_frame (开放十字路口<=120字), "
            "target_space_id, target_zone_id, core_actor_ids: [全条丝线的核心当事NPC ID，省略配角与协助者], "
            "dialogue_focus（可选）: {actor_ids: [先开口的核心NPC ID, 对方核心NPC ID], topic: 本幕实际需要谈清的事, required_face_to_face: bool}。"
            "两人均须列在 core_actor_ids。双方分处不同空间时，仅在本幕必须当面摊牌时填 required_face_to_face=true。"
            "配角商议、准备、传话、打气或顺路闲聊均省略 dialogue_focus。"
            "resident_roles: {actor_id: {local_role, relationship_to_incident}}, "
            "visitor_framing (外来旅人的介入处境<=100字), "
            "world_setup: {spawn_props: [{space_id, zone_id, name, visual_prompt, brief, detail, collectible: bool}], "
            "ambient_cue: {space_id, zone_id, text}, weather_mode: ''|clear|cloudy|rain|fog|snow}。"
        )
        rel_block = _format_cast_relations_block(session, cast_relations)
        loc_block = _format_actor_locations_block(world_state, ledgers)
        user_prompt = (
            f"丝线基调：{THEME_COMPASS_LABELS.get(eff_theme, eff_theme)}\n"
            f"故事标题：{custom_title or '自动生发'}\n"
            f"玩家设定与事件契机：{prompt_hint or '无（围绕当前小镇与角色自由起兴）'}\n"
            f"首选核心落点：{anchor['space_id']} ({anchor['space_desc']}) zone_id={anchor['zone_id']}\n"
            f"全部可用空间：{'; '.join(spaces_desc)}\n"
            + (f"在场角色当前真实位置：\n{loc_block}\n" if loc_block else "")
            + f"本镇原生居民（事件本地参与者）：{'; '.join(native_lines) or '无'}\n"
            f"导入的外来角色（当事主角）：{'; '.join(visitor_lines) or '无'}\n"
            + (f"在场角色初始情感态度指针（请尊重并融入世界观张力）：\n{rel_block}\n" if rel_block else "")
        )
        try:
            raw = caller(system_prompt, user_prompt)
        except Exception:
            raw = None
        if isinstance(raw, Mapping) and raw.get("situation"):
            proposed_core = raw.get("core_actor_ids")
            core_ids = [
                aid for aid in (proposed_core if isinstance(proposed_core, list) else ())
                if isinstance(aid, str) and isinstance(ledgers.get(aid), Mapping) and not ledgers[aid].get("is_player")
            ]
            spaces = _spaces_index(world_state)
            tsid = _ensure_interior_sid(raw.get("target_space_id"), spaces, world_state, anchor["space_id"])
            tsp = spaces.get(tsid) or {}
            tzid = str(raw.get("target_zone_id") or "")
            tzone = next((z for z in (tsp.get("zones") or ()) if isinstance(z, Mapping) and z.get("zone_id") == tzid), None)
            if tzone is None and tsid == anchor["space_id"] and anchor["zone_id"]:
                tzone = next((z for z in (tsp.get("zones") or ()) if isinstance(z, Mapping) and z.get("zone_id") == anchor["zone_id"]), None)
            tdesc = _describe_space(tsp, tzone)
            setup = _validate_world_setup(raw.get("world_setup"), world_state, tdesc)
            merged_roles = {**offline_roles}
            raw_roles = raw.get("resident_roles")
            if isinstance(raw_roles, Mapping):
                for r_aid, r_val in raw_roles.items():
                    if str(r_aid) in merged_roles and isinstance(r_val, Mapping):
                        merged_roles[str(r_aid)] = {
                            "local_role": clip_text(r_val.get("local_role"), 36, merged_roles[str(r_aid)]["local_role"]),
                            "relationship_to_incident": clip_text(
                                r_val.get("relationship_to_incident"),
                                120,
                                merged_roles[str(r_aid)]["relationship_to_incident"],
                            ),
                        }
            public_fact = clip_text(raw.get("public_incident_fact") or raw.get("premise") or raw.get("situation"), 260)
            v_frame = clip_text(raw.get("visitor_framing"), 160, offline_visitor_framing)
            node = make_story_node(
                seq=1,
                title=clip_text(raw.get("node_title"), 48, "命运的微小契机"),
                phase="spark",
                situation=clip_text(raw.get("situation"), 300),
                dilemma_frame=clip_text(raw.get("dilemma_frame"), 220, "各自会怎么回应这一幕？"),
                target_space_id=tdesc["space_id"],
                target_space_name=tdesc["space_name"],
                target_floor_label=tdesc["floor_label"],
                target_zone_id=tdesc["zone_id"],
                target_zone_name=tdesc["zone_name"],
                world_setup=setup,
                entry_triggers=[
                    {"type": "ambient_cue", "space_id": tdesc["space_id"]},
                    {"type": "actor_in_space", "space_id": tdesc["space_id"], "zone_id": tdesc["zone_id"]},
                    {"type": "sim_time_reached", "at_min": sim_min + 1.0},
                ],
                catalyst_actor_ids=lead_actor_ids,
                dialogue_focus=normalize_dialogue_focus(raw.get("dialogue_focus"), ledgers),
                created_at_min=sim_min,
                pressure_deadline_min=sim_min + 1.0,
            )
            return make_fate_thread(
                title=clip_text(custom_title or raw.get("title"), 56, "悄然萌生的丝线"),
                premise=clip_text(raw.get("premise"), 220, public_fact),
                public_incident_fact=public_fact,
                resident_roles=merged_roles,
                visitor_framing=v_frame,
                theme_compass=eff_theme,
                tension=0.28,
                phase="spark",
                cast_stakes=cast_stakes,
                nodes=[node],
                max_nodes=eff_max_nodes,
                lead_actor_ids=lead_actor_ids,
                core_actor_ids=core_ids,
                created_at_min=sim_min,
            )

    return _weave_opening_offline(
        session,
        world_state,
        eff_theme,
        anchor,
        cast_stakes,
        prompt_hint,
        sim_min,
        custom_title=custom_title,
        max_nodes=eff_max_nodes,
        lead_actor_ids=lead_actor_ids,
    )


def _focused_social_choice(aid: str, choice: Mapping[str, Any], node: Mapping[str, Any]) -> bool:
    intent = choice.get("world_intent") or {}
    return (intent.get("kind") == "social"
            and {aid, intent.get("target")} == set((node.get("dialogue_focus") or {}).get("actor_ids", ())))


def _choices_digest(
    session: Mapping[str, Any],
    node: Mapping[str, Any],
) -> tuple[list[str], Counter[str]]:
    lines: list[str] = []
    stances: Counter[str] = Counter()
    p_choices = node.get("participant_choices") or {}
    for aid, rec in p_choices.items():
        if not isinstance(rec, Mapping):
            continue
        st = str(rec.get("stance") or "observe")
        stances[st] += 1
        exec_st = str(rec.get("execution_status") or "observed")
        exec_label = {"completed": "已在世界里付诸行动", "pending": "正准备动身", "observed": "原地体会与观望", "interrupted": "中途生出变数"}.get(exec_st, exec_st)
        if exec_st == "pending" and (node.get("dialogue_focus") or {}).get("exchanges") and _focused_social_choice(aid, rec, node):
            exec_label = "已开口，结论以原话为准"
        lines.append(
            f"- {rec.get('actor_name') or aid} [{STANCE_LABELS.get(st, st)} / {exec_label}]：{rec.get('choice_summary')}（内心：{rec.get('inner_reason') or '未吐露'}）"
        )
    if node.get("player_stance"):
        ps = node["player_stance"]
        stances[str(ps.get("stance") or "observe")] += 1
        lines.append(f"- 玩家表态 [{STANCE_LABELS.get(ps.get('stance'), ps.get('stance'))}]：{ps.get('note') or '以自身方式参与其中'}")
    return lines, stances


def _weave_next_offline(
    session: Mapping[str, Any],
    thread: Mapping[str, Any],
    current_node: Mapping[str, Any],
    world_state: Mapping[str, Any],
    sim_min: float,
) -> dict[str, Any]:
    seq = int(current_node.get("seq") or 1)
    raw_max = int(thread.get("max_nodes") or 0)
    max_nodes = raw_max if raw_max > 0 else 4
    lines, stances = _choices_digest(session, current_node)
    dominant = stances.most_common(1)[0][0] if stances else "investigate"
    used_sids = [str(n.get("target_space_id") or "") for n in (thread.get("nodes") or ()) if isinstance(n, Mapping)]
    anchor = _pick_stage_anchor(
        world_state,
        avoid_space_id=str(current_node.get("target_space_id") or ""),
        avoid_space_ids=used_sids,
        prefer_secondary_floor=True,
    )
    sid, zid, desc = anchor["space_id"], anchor["zone_id"], anchor["space_desc"]
    prev_summary = "；".join(
        f"{rec.get('actor_name')}选择「{STANCE_LABELS.get(rec.get('stance'), rec.get('stance'))}」"
        for rec in (current_node.get("participant_choices") or {}).values()
        if isinstance(rec, Mapping)
    ) or "众人各自静静记下了这件事"
    outcome_summary = clip_text(f"上一拍落定：{prev_summary}。", 180)

    theme = str(thread.get("theme_compass") or "daily_aspiration")
    public_fact = str(thread.get("public_incident_fact") or thread.get("premise") or "")

    # 判断是否进入自然收束（afterglow）
    should_resolve = seq >= max_nodes or (
        seq >= 3
        and theme in ("daily_aspiration", "bond_turning")
        and dominant in ("confide", "mediate", "pursue_dream", "protect")
    )
    if should_resolve:
        epilogue_by_stance = {
            "investigate": "顺着众人大胆追问与细心查证留下的痕迹，最初的疑云终于一点点拼出了完整轮廓，大家也更看清了彼此在关键时刻的锋芒。",
            "confront": "正因为有人鼓起勇气把话说破、没有让误会继续发酵，这段风波在坦诚交锋后反而落回了清澈的地面。",
            "protect": "在不安与犹豫之中依然选择站出来护住同伴的举动，让这栋楼里的灯光比平时更加温暖，也让一段原本脆弱的信任真正生根。",
            "confide": "一次次把不敢说的顾虑交到信任的人手里后，原本压在心底的重量渐渐化作了彼此心照不宣的陪伴。",
            "mediate": "在各执一词的拉扯之间，有人愿意耐心地听见每一方的难处，才让这段插曲温柔地落在了恰到好处的平衡点上。",
            "pursue_dream": "从桌角那点微弱的念想出发，有人真的逆着懒散和顾忌动手往前跨了一步——小镇因此多了一段属于他们自己的明亮记忆。",
            "conceal": "有些人选择把秘密与考量暂时收进抽屉深处，表面平静重回小镇，但每推开一次二楼的门，心中留下的默契与余韵仍悄然存在。",
            "withdraw": "每个人都在安稳与喧闹之间找回了自己的呼吸边界——有时候不硬撑着迎上去，同样是忠于内心的回答。",
        }
        ledgers = session.get("ledgers") or {}
        char_eps: dict[str, dict[str, Any]] = {}
        for aid, ledger in ledgers.items():
            if ledger.get("is_player"):
                continue
            own_choices = [c for c in (ledger.get("choice_records") or ()) if c.get("thread_id") == thread.get("thread_id")]
            if not own_choices:
                continue
            stances_txt = " → ".join(STANCE_LABELS.get(c["stance"], c["stance"]) for c in own_choices)
            char_eps[aid] = {
                "name": ledger.get("name") or aid,
                "stances": stances_txt,
                "arc_note": own_choices[-1].get("inner_reason") or own_choices[-1].get("choice_summary") or "",
            }
        key_turns = [
            f"第{n['seq']}拍《{n['title']}》：{n.get('outcome_summary') or n.get('situation')}"
            for n in (thread.get("nodes") or ())
            if isinstance(n, Mapping)
        ]
        return {
            "action": "resolve",
            "outcome_summary": outcome_summary,
            "chapter_chronicle": {
                "title": f"《{thread.get('title')}》· 章节纪事",
                "epilogue": epilogue_by_stance.get(dominant, epilogue_by_stance["confide"]),
                "key_turns": key_turns,
                "character_epilogues": char_eps,
            },
        }

    # 按前序主导抉择分叉编排下一拍
    next_seq = seq + 1
    next_phase = VALID_THREAD_PHASES[min(len(VALID_THREAD_PHASES) - 2, next_seq)]

    if dominant in ("investigate", "confront"):
        n_title = "顺藤触及新的记录"
        n_sit = f"因为上一拍里有人没有袖手旁观、选择追查与直面，{desc}柜角里压着的另一半线索被翻了出来——上面是一页记录当晚进出时刻的「撕下的旧页」。"
        n_dil = "线索指向的人或关键时刻就在眼前了：继续深挖底牌，还是趁势找当事者温和摊牌？"
        n_prop = {
            "space_id": sid, "zone_id": zid, "name": "撕下的旧页",
            "visual_prompt": "半张泛黄方格纸，边缘有锯齿状撕痕，写着几行略微涂改的时间笔记",
            "brief": "墙边柜角压着半张锯齿撕痕的旧页",
            "detail": "上面记着当晚十点前后的出入时刻与一句潦草附注：‘当时锁孔外有人，先别把钥匙交出去。’",
            "collectible": True, "traits_hint": {"inscribable": True},
        }
        n_cue = f"{desc}柜角压着半张记有当晚时刻与附注的旧页。"
        next_tension = min(0.88, float(thread.get("tension") or 0.3) + 0.22)

    elif dominant in ("protect", "confide", "mediate"):
        if theme in ("mystery_ripple", "crisis_test"):
            n_title = "互保之下浮出的证言"
            n_sit = f"上一拍里有人选择彼此照应或私下商议后，有人在{desc}悄悄留下了「折角证言便签」，试图划清当晚谁真正在场。"
            n_dil = "有人愿意互相作证、也有人有所隐瞒：信下这份口风替熟人圆场，还是当场去核对字条里的不在场证明？"
            n_prop = {
                "space_id": sid, "zone_id": zid, "name": "折角证言便签",
                "visual_prompt": "一张折起一角的硬质记录签，钢笔字迹急促，写着两人的互相证言",
                "brief": "桌角压着一张钢笔写就的证言便签",
                "detail": "上面写着：‘昨晚 22:00 到 22:30 之间我和熟人一直待在屋里，绝没有靠近案发处，别误抓自己人。’",
                "collectible": True, "traits_hint": {"inscribable": True},
            }
            n_cue = f"{desc}的桌面新留下一张写着互相作证时段的折角便签。"
            next_tension = min(0.82, float(thread.get("tension") or 0.3) + 0.16)
        else:
            n_title = "敞开心扉后的连锁回响"
            n_sit = f"上一拍里选择伸手接住彼此的心意后，事情开始往意想不到的方向松动——有人主动在{desc}留下了约人相见的小礼盒与字条。"
            n_dil = "信任已经架起了一半，现在是鼓起最后一点勇气把真话全盘托出，还是用更体贴的方式护住这段刚发芽的默契？"
            n_prop = {
                "space_id": sid, "zone_id": zid, "name": "系带小礼盒",
                "visual_prompt": "一个系着暖黄色缎带的小方盒，旁边压着一张简短字条",
                "brief": "桌上摆着一只系着暖色缎带的小方盒",
                "detail": "字条上写着：‘谢谢你愿意走过来听我说。如果你也方便，我在刚才提过的地方等你一会。’",
                "collectible": False, "traits_hint": {"inscribable": True},
            }
            n_cue = f"{desc}桌上静静摆着一只系着暖黄缎带的小方盒与短笺。"
            next_tension = min(0.75, float(thread.get("tension") or 0.3) + 0.14)

    elif dominant == "pursue_dream":
        if theme in ("mystery_ripple", "crisis_test"):
            n_title = "登记簿上的缺页记录"
            n_sit = f"围绕「{thread.get('title') or '这起事件'}」的追查延伸到了{desc}：台面上多了一本被人匆忙合上的「值守进出登记簿」，当晚时段的记录明显缺了一角。"
            n_dil = "是谁在当晚撕走了进出记录？要拿着这本登记簿去当面质问相关住户，还是先暗中比对上面的笔迹与泥印？"
            n_prop = {
                "space_id": sid, "zone_id": zid, "name": "值守进出登记簿",
                "visual_prompt": "一本深蓝色封皮的值守登记本，中间几页有明显撕痕并沾着雨水",
                "brief": "柜台桌角摆着一本缺了内页的深蓝登记簿",
                "detail": "残留的墨痕显示昨晚 22:00 前后有两名住户曾先后离开住处走向案发处，最后一栏签名被刮糊。",
                "collectible": True, "traits_hint": {"inscribable": True},
            }
            n_cue = f"{desc}桌上摊开的深蓝值守登记簿上，缺失的一页正清晰指向昨晚的关键时段。"
            next_tension = min(0.84, float(thread.get("tension") or 0.3) + 0.18)
        else:
            n_title = "动手之后的意外插曲"
            n_sit = f"心愿已经不再只是纸上的字了！在{desc}准备动手落实想法时，却发现缺少一点关键小物件，而另一边似乎刚好有居民路过。"
            n_dil = "要独自跑遍楼上楼下找齐材料自己扛下来，还是放下羞怯找身边的同伴搭把手？"
            n_prop = {
                "space_id": sid, "zone_id": zid, "name": "缺角的布置清单",
                "visual_prompt": "一张用彩色铅笔勾出星号的小清单，其中两项被红笔圈了起来",
                "brief": "台面摊开着一张勾了红圈的彩色清单",
                "detail": "上面画着温馨的小茶点区与暖光灯布局，只差最后一份小点心或暖饮材料就能完工。",
                "collectible": False, "traits_hint": {"inscribable": True},
            }
            n_cue = f"{desc}里响起挪动椅凳与哼着调子忙碌的轻微响动。"
            next_tension = min(0.68, float(thread.get("tension") or 0.3) + 0.12)

    else:  # conceal / withdraw
        n_title = "静默之下暗暗发酵的余温"
        n_sit = f"大家各自把真实心思藏起来之后，表面重回安静，但{desc}门外却落下一枚「半枚银色袖扣」与试探字条，当晚的疑云并未散去。"
        n_dil = "沉默没能让风波自己消失：继续守口如瓶独处，还是走出去面对这枚确凿的物件与大家的追问？"
        n_prop = {
            "space_id": sid, "zone_id": zid, "name": "折角试探字条",
            "visual_prompt": "一张折成三角的素白纸签，旁边压着半枚泛冷光的银色袖扣",
            "brief": "门口地面压着一枚折角字条与半枚银扣",
            "detail": "字条上写着：‘昨晚十点后门外遗落的半枚银扣就在这里，隐瞒行踪只会让自己显得更可疑。’",
            "collectible": True, "traits_hint": {"inscribable": True},
        }
        n_cue = f"{desc}门边地面的字条与半枚银扣正在等人们做出下一轮回应。"
        next_tension = min(0.92, float(thread.get("tension") or 0.3) + 0.25)

    next_node = make_story_node(
        parent_node_id=str(current_node.get("node_id") or ""),
        seq=next_seq,
        title=n_title,
        phase=next_phase,
        situation=n_sit,
        dilemma_frame=n_dil,
        target_space_id=sid,
        target_space_name=anchor["space_name"],
        target_floor_label=anchor["floor_label"],
        target_zone_id=zid,
        target_zone_name=anchor["zone_name"],
        world_setup={
            "spawn_prop": n_prop,
            "spawn_props": [n_prop],
            "ambient_cue": {"space_id": sid, "zone_id": zid, "text": n_cue, "cue_kind": "sound"},
        },
        entry_triggers=[
            {"type": "ambient_cue", "space_id": sid},
            {"type": "actor_in_space", "space_id": sid, "zone_id": zid},
            {"type": "sim_time_reached", "at_min": sim_min + 1.0},
        ],
        created_at_min=sim_min,
        pressure_deadline_min=sim_min + 1.0,
    )
    return {
        "action": "next_node",
        "outcome_summary": outcome_summary,
        "phase": next_phase,
        "tension": next_tension,
        "node": next_node,
    }


def _dialogue_lines(focus: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        (exchange[side], turn[line])
        for exchange in focus["exchanges"] for turn in exchange["transcript"]
        for side, line in (("speaker_id", "a"), ("partner_id", "b"))
        if isinstance(turn.get(line), str) and turn[line].strip()
    ]


def _dialogue_result(raw: Mapping[str, Any], focus: Mapping[str, Any]) -> tuple[str, str]:
    result = raw.get("dialogue_result")
    if not isinstance(result, Mapping):
        return "", ""
    outcome = result.get("outcome")
    if outcome not in ("answered", "deferred"):
        return "", ""
    lines = _dialogue_lines(focus)
    line_ids = result.get("line_ids")
    matched: list[tuple[str, str]] = []
    if line_ids is not None:
        if not isinstance(line_ids, list):
            return "", ""
        valid_ids = set()
        for ref in line_ids:
            if isinstance(ref, str) and re.fullmatch(r"[0-9]{1,4}", ref.strip()):
                ref = int(ref)
            if type(ref) is int and 1 <= ref <= len(lines):
                valid_ids.add(ref)
        matched = [lines[i - 1] for i in sorted(valid_ids)]
    else:
        evidence = result.get("evidence")
        if not isinstance(evidence, list):
            return "", ""
        pos = 0
        for row in evidence:
            if not isinstance(row, Mapping) or not row.get("quote"):
                return "", ""
            quote = (row.get("actor_id"), row["quote"])
            while pos < len(lines) and lines[pos] != quote:
                pos += 1
            if pos == len(lines):
                return "", ""
            matched.append(quote)
            pos += 1
    if not matched:
        return "", ""
    return outcome, " / ".join(f"「{quote}」" for _, quote in matched)


def _continue_after_dialogue(
    thread: Mapping[str, Any], node: Mapping[str, Any], focus: Mapping[str, Any],
    review: Mapping[str, Any], reason: str, sim_min: float,
) -> dict[str, Any]:
    lines = _dialogue_lines(focus)
    excerpts = lines if len(lines) <= 3 else [lines[0], *lines[-2:]]
    summary = clip_text(" / ".join(f"「{clip_text(line, 52)}」" for _, line in excerpts), 165)
    outcome = f"{reason}已说出口的话：{summary}" if summary else reason
    follow_up = make_story_node(
        parent_node_id=node["node_id"], seq=int(node.get("seq") or 1) + 1,
        title="谈话之后", phase="crossroads",
        situation=outcome,
        dilemma_frame="根据已经说清的内容采取下一步实际行动，仍待确认的事项留到合适时机。",
        target_space_id=node.get("target_space_id") or "",
        target_space_name=node.get("target_space_name") or "",
        target_floor_label=node.get("target_floor_label") or "",
        target_zone_id=node.get("target_zone_id") or "",
        target_zone_name=node.get("target_zone_name") or "",
        entry_triggers=[{"type": "sim_time_reached", "at_min": sim_min + 1.0}],
        created_at_min=sim_min, pressure_deadline_min=sim_min + 1.0,
    )
    return {
        "action": "next_node", **review, "outcome_summary": outcome,
        "dialogue_outcome": "interrupted", "dialogue_evidence": outcome,
        "phase": "crossroads", "tension": float(thread.get("tension") or 0.4), "node": follow_up,
    }


def weave_next_beat(
    session: Mapping[str, Any],
    thread: Mapping[str, Any],
    world_state: Mapping[str, Any],
    persona_adapter: Any = None,
    *,
    cast_relations: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    llm_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """根据角色在当前节点实际做出的选择与真实世界后果，动态编排下一拍或收束成章。"""
    active_nid = str(thread.get("active_node_id") or "")
    current_node = next(
        (n for n in (thread.get("nodes") or ()) if n.get("node_id") == active_nid),
        (thread.get("nodes") or [{}])[-1],
    )
    sim_min = float(world_state.get("minutes") if world_state.get("minutes") is not None else session.get("last_sim_min") or 0.0)
    seq = int(current_node.get("seq") or 1)
    raw_max = int(thread.get("max_nodes") or 0)
    eff_cap = raw_max if raw_max > 0 else MAX_ORGANIC_NODES
    focus = current_node.get("dialogue_focus") or {}
    focus_exchanges = focus.get("exchanges") or []
    dialogue_review = {
        "dialogue_revision": int(focus.get("revision") or 0),
        "dialogue_review_saved": bool(focus.get("review_saved")),
        "dialogue_interruption": str(focus.get("interruption") or ""),
    }
    closing_reason = dialogue_review["dialogue_interruption"]
    if not closing_reason and len(focus_exchanges) >= 8:
        closing_reason = "这段谈话告一段落，未谈妥的事项留待后续。"
    focus_fallback = (
        _continue_after_dialogue(thread, current_node, focus, dialogue_review, closing_reason, sim_min)
        if closing_reason else {"action": "continue_dialogue", **dialogue_review}
    )
    choice_lines, _ = _choices_digest(session, current_node)
    completed_lines = [
        f"- {rec.get('actor_name') or aid}：{rec.get('choice_summary')}（结果：{rec.get('execution_note') or '已实际发生'}）"
        for aid, rec in (current_node.get("participant_choices") or {}).items()
        if isinstance(rec, Mapping) and rec.get("execution_status") == "completed"
    ]

    if use_llm and (choice_lines or focus_exchanges or closing_reason):
        caller = llm_fn or (
            lambda sys_p, usr_p: ask_llm_json(sys_p, usr_p, timeout_sec=18.0, max_tokens=1600)
        )
        used_sids = [str(n.get("target_space_id") or "") for n in (thread.get("nodes") or ()) if isinstance(n, Mapping)]
        anchor = _pick_stage_anchor(
            world_state,
            avoid_space_id=str(current_node.get("target_space_id") or ""),
            avoid_space_ids=used_sids,
            prefer_secondary_floor=True,
        )
        spaces_desc = [
            f"{sp['space_id']}: {sp.get('name')}({sp.get('floor_label') or '1F'}) 隔间={','.join(z.get('name','') + '[' + z.get('zone_id','') + ']' for z in (sp.get('zones') or ()) if isinstance(z, Mapping)) or '整层'}"
            for sp in (world_state.get("spaces") or ())[:10]
        ]
        prev_nodes_txt = "\n".join(
            f"- 第{n['seq']}幕 {n['title']}: {n.get('outcome_summary') or n['situation']}"
            for n in (thread.get("nodes") or ())
        )
        t_created = float(thread.get("created_at_min") or 0.0)
        n_created = float(current_node.get("created_at_min") or t_created)
        node_discoveries: list[str] = []
        thread_discoveries: list[str] = []
        role_lines: list[str] = []
        for lg in (session.get("ledgers") or {}).values():
            if not isinstance(lg, Mapping) or lg.get("is_player"):
                continue
            sr = lg.get("story_role") if isinstance(lg.get("story_role"), Mapping) else {}
            if sr.get("local_role") or sr.get("relationship_to_incident"):
                role_lines.append(
                    f"- {lg.get('name')}（{sr.get('role_category')}/{sr.get('local_role') or '访客'}）：{sr.get('relationship_to_incident') or ''}"
                )
            for ev in lg.get("experienced_events") or ():
                ev_min = float(ev.get("sim_min") or 0.0)
                if ev_min < t_created - 0.1:
                    continue
                if ev.get("kind") in ("entity_inspected", "entity_read", "dialogue_exchange", "gift_transferred"):
                    row = f"- {lg.get('name')}: {ev.get('summary')}"
                    thread_discoveries.append(row)
                    if ev_min >= n_created - 0.1:
                        node_discoveries.append(row)
        recent_ev_lines = node_discoveries[-10:] or thread_discoveries[-8:]
        discoveries_txt = "\n".join(recent_ev_lines) or "（注意：世界里暂未发生新的真实对话或物证交互）"
        pending_lines = [
            f"- {rec.get('actor_name') or aid}：原本打算[{rec.get('stance_label') or rec.get('stance')}]“{rec.get('choice_summary')}”，但世界里【尚未完成】该行动"
            for aid, rec in (current_node.get("participant_choices") or {}).items()
            if isinstance(rec, Mapping) and rec.get("execution_status") == "pending"
            and not (focus_exchanges and _focused_social_choice(aid, rec, current_node))
        ]
        if focus_exchanges:
            completed_lines.append("- 两位当事人已经实际交谈，已达成的共识和仍待执行的安排以文末原话为准。")
        completed_txt = "\n".join(completed_lines) or "暂未记录已完成的行动或对话。"
        pending_txt = "\n".join(pending_lines) or "无"
        roles_txt = "\n".join(role_lines[:10]) or "（常住与外来角色）"
        loc_block = _format_actor_locations_block(world_state, session.get("ledgers") or {})
        is_personal_theme = str(thread.get("theme_compass") or "") in ("daily_aspiration", "bond_turning")
        pacing_hint = (
            f"当前设定了参考上限 {raw_max} 幕。"
            if raw_max > 0
            else "当前为【无固定拍数上限】的持续命运模式：不要机械拖延或强行凑拍数，但必须以【世界里已经真实说出口的原话与行动】为准向前推进，只有当核心愿望/表白/谜团在【真实对话原话】中确实达成且得到回应时才允许输出 action='resolve'。"
        )
        system_prompt = (
            "你是「命运织机」的连续命运编排器。你要严格根据【刚才真实发生的对话原话、行动结果与角色此刻真实位置】编排下一步，绝不越俎代庖替角色提前演完未发生的剧情。\n"
            f"{pacing_hint}\n"
            "铁律（防强行推进与防幻觉）：\n"
            "1. 紧扣【核心既成事实】、主角【已实际完成的行动/对话原话】与【角色当前真实位置】，绝不吃书或偏离主题。\n"
            "2. **严禁把角色只是“原本打算(pending)”的心思写成已经发生**：如果【真实对话原话】里角色根本没有说出“我喜欢你”、没有拆信或还没见到目标人物，outcome_summary 与下一幕 situation 绝不能虚构成“已经说出口/已经表白”，只能如实承接刚才真正聊了什么（例如仍在犹豫或试探），让角色接下来自己再去做决定。\n"
            "3. **严禁空间与姿态幻觉**：必须尊重【角色当前真实位置与姿态】，如果两名角色分别在不同场所或站在广场/走廊，下一幕 situation 绝不能凭空写成“两人正坐在某室内壁炉前”，禁止编造“楼上楼下有动静”等空洞桥段。\n"
            + ("4. 若本幕属于人际互动/倾诉/表白过渡，world_setup.spawn_props 可为 []，直接把舞台交还给当事角色的下一步对话与行动。\n" if is_personal_theme else "4. 每一幕都必须落地到具体地点、新证物或人物对峙。\n")
            + "仅输出 JSON：\n"
            "- 若 action='next_node'：{action, outcome_summary, tension(0~1), phase, dialogue_result, node: {title, phase, situation, dilemma_frame, target_space_id, target_zone_id, world_setup: {spawn_props: [...], ambient_cue}, dialogue_focus（可选）: {actor_ids: [先开口的核心NPC ID, 对方核心NPC ID], topic: 本幕实际需要谈清的事, required_face_to_face: bool}}}。"
            "配角商议、传话和准备幕省略 dialogue_focus。仅在两位核心当事人必须当面摊牌时设置 required_face_to_face=true。\n"
            "- 若 action='resolve'：{action, outcome_summary, dialogue_result, chapter_chronicle: {title, epilogue, key_turns: []}}。\n"
            "- dialogue_result: {outcome:'answered'|'deferred', line_ids:[原话编号...]}，当前幕无关键对话时可省略。\n"
            "- 若 action='continue_dialogue'：{action}。"
        )
        focus_block = ""
        if focus:
            system_prompt += (
                "\n先判定【关键对话话题】在本幕是否已经谈清，再编排下一步。只用下方【关键对话原话】判定。"
                "单方明确回答本幕问题、双方达成分工或同意先动身，均可给出 answered。明确拒谈或改期可给出 deferred。"
                "若下一步要去找人、取物或改换地点，推进到执行该安排的一幕并省略 dialogue_focus。尚待讨论的细节留到行动发生后。"
                "若只有邀约、试探或单方面说有话想说，输出 {\"action\":\"continue_dialogue\"}。"
                "line_ids 引用足以支持判断的原话，可以只引用明确作答的一人。"
                "不要把配角的谈话、心理活动或模型摘要当证据。"
            )
            lines = [f"{i}. {aid}：{line}" for i, (aid, line) in enumerate(_dialogue_lines(focus), 1)]
            focus_block = f"\n【关键对话话题】{focus['topic']}\n【关键对话原话】\n" + "\n".join(lines)
            if closing_reason:
                system_prompt += "\n本段对话已经结束。输出 next_node，让角色根据已说出的内容采取下一步行动，省略下一幕的 dialogue_focus。"
                focus_block += f"\n【结束原因】{closing_reason}"
        rel_block = _format_cast_relations_block(session, cast_relations)
        core_ids = ", ".join(thread.get("core_actor_ids") or ())
        seq_head = f"第{seq}/{raw_max}幕" if raw_max > 0 else f"第{seq}幕（持续演进）"
        user_prompt = (
            f"丝线：《{thread.get('title')}》({thread.get('theme_compass')}) {seq_head} 张力={thread.get('tension')}\n"
            f"核心既成事实：{thread.get('public_incident_fact') or thread.get('premise')}\n"
            f"角色身份与当事利害：\n{roles_txt}\n"
            + (f"丝线核心当事人 ID：{core_ids}\n" if core_ids else "")
            + (f"在场角色当前真实位置与姿态（必须尊重，严禁写反）：\n{loc_block}\n" if loc_block else "")
            + (f"在场角色当前情感关系指针：\n{rel_block}\n" if rel_block else "")
            + f"前情脉络：\n{prev_nodes_txt}\n"
            f"本幕已实际完成行动/对话的决定：\n{completed_txt}\n"
            f"本幕尚未在世界里落地的意图（未发生，严禁当成既成事实）：\n{pending_txt}\n"
            f"刚才在世界里真实发生的对话原话与查证：\n{discoveries_txt}\n"
            f"本幕完整抉择清单：\n" + "\n".join(choice_lines) + "\n"
            f"可选空间：{'; '.join(spaces_desc)}\n"
            f"可选下一幕落点参考：{anchor['space_id']} ({anchor['space_desc']}) zone_id={anchor['zone_id']}\n"
            + focus_block
        )
        try:
            raw = caller(system_prompt, user_prompt)
        except Exception:
            raw = None
        if isinstance(raw, Mapping):
            act = str(raw.get("action") or "").strip().lower()
            dialogue_fields = {}
            if focus:
                outcome, evidence_note = _dialogue_result(raw, focus)
                if closing_reason:
                    if act != "next_node":
                        return focus_fallback
                    if not outcome:
                        outcome, evidence_note = "interrupted", focus_fallback["dialogue_evidence"]
                elif not outcome or act not in ("resolve", "next_node"):
                    return focus_fallback
                dialogue_fields = {
                    **dialogue_review, "dialogue_outcome": outcome, "dialogue_evidence": evidence_note,
                }
            default_summary = focus_fallback["outcome_summary"] if closing_reason else "这一步的选择已悄然改变了场上的空气。"
            outcome_summary = clip_text(raw.get("outcome_summary"), 220, default_summary)
            if focus and not closing_reason and act == "next_node" and seq >= eff_cap:
                act = "resolve"
                raw = {"chapter_chronicle": {"epilogue": evidence_note, "key_turns": [evidence_note]}}
                outcome_summary = clip_text(evidence_note, 220)
            if act == "resolve" and isinstance(raw.get("chapter_chronicle"), Mapping):
                cc = raw["chapter_chronicle"]
                ledgers = session.get("ledgers") or {}
                char_eps: dict[str, dict[str, Any]] = {}
                for aid, ledger in ledgers.items():
                    if ledger.get("is_player"):
                        continue
                    own_c = [c for c in (ledger.get("choice_records") or ()) if c.get("thread_id") == thread.get("thread_id")]
                    if own_c:
                        char_eps[aid] = {
                            "name": ledger.get("name") or aid,
                            "stances": " → ".join(STANCE_LABELS.get(c["stance"], c["stance"]) for c in own_c),
                            "arc_note": own_c[-1].get("inner_reason") or own_c[-1].get("choice_summary") or "",
                        }
                return {
                    "action": "resolve",
                    **dialogue_fields,
                    "outcome_summary": outcome_summary,
                    "chapter_chronicle": {
                        "title": clip_text(cc.get("title"), 64, f"《{thread.get('title')}》· 章节纪事"),
                        "epilogue": clip_text(cc.get("epilogue"), 360, outcome_summary),
                        "key_turns": [clip_text(x, 120) for x in (cc.get("key_turns") or ()) if str(x).strip()][:8],
                        "character_epilogues": char_eps,
                    },
                }
            if act == "next_node" and isinstance(raw.get("node"), Mapping) and (seq < eff_cap or closing_reason):
                nd = raw["node"]
                spaces = _spaces_index(world_state)
                tsid = _ensure_interior_sid(nd.get("target_space_id"), spaces, world_state, anchor["space_id"])
                tsp = spaces.get(tsid) or {}
                tzid = str(nd.get("target_zone_id") or "")
                tzone = next((z for z in (tsp.get("zones") or ()) if isinstance(z, Mapping) and z.get("zone_id") == tzid), None)
                tdesc = _describe_space(tsp, tzone)
                setup = _validate_world_setup(nd.get("world_setup"), world_state, tdesc)
                next_phase = str(nd.get("phase") or raw.get("phase") or "crossroads").strip().lower()
                if next_phase not in VALID_THREAD_PHASES:
                    next_phase = "crossroads"
                default_tension = float(thread.get("tension") or 0.4) + 0.18
                try:
                    next_tension = float(raw.get("tension", default_tension))
                except (TypeError, ValueError):
                    next_tension = default_tension
                node = make_story_node(
                    parent_node_id=active_nid,
                    seq=seq + 1,
                    title=clip_text(nd.get("title"), 48, f"第 {seq + 1} 幕"),
                    phase=next_phase,
                    situation=clip_text(nd.get("situation"), 300, "顺着刚才的选择，局势滑入了新的一层。"),
                    dilemma_frame=clip_text(nd.get("dilemma_frame"), 220, "接下来你打算如何面对？"),
                    target_space_id=tdesc["space_id"],
                    target_space_name=tdesc["space_name"],
                    target_floor_label=tdesc["floor_label"],
                    target_zone_id=tdesc["zone_id"],
                    target_zone_name=tdesc["zone_name"],
                    world_setup=setup,
                    dialogue_focus=(normalize_dialogue_focus(nd.get("dialogue_focus"), session.get("ledgers") or {})
                                    if not closing_reason else None),
                    entry_triggers=[
                        {"type": "ambient_cue", "space_id": tdesc["space_id"]},
                        {"type": "actor_in_space", "space_id": tdesc["space_id"], "zone_id": tdesc["zone_id"]},
                        {"type": "sim_time_reached", "at_min": sim_min + 1.0},
                    ],
                    created_at_min=sim_min,
                    pressure_deadline_min=sim_min + 1.0,
                )
                return {
                    "action": "next_node",
                    **dialogue_fields,
                    "outcome_summary": outcome_summary,
                    "phase": next_phase,
                    "tension": max(0.1, min(1.0, next_tension)),
                    "node": node,
                }

    return focus_fallback if focus else _weave_next_offline(session, thread, current_node, world_state, sim_min)


def build_fate_overlay(
    session: Mapping[str, Any],
    world_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """为世界顶栏 #btn-external-overlay 构建无剧透「命运编年史 & 心路面板」。"""
    _ = world_state
    safe = redact_fate_session_for_ui(session)
    realm_name = str(safe.get("realm_name") or safe.get("realm_space_id") or "小镇")
    reveal = bool(safe.get("reveal_inner_thoughts", False))
    threads = [t for t in (safe.get("threads") or ()) if isinstance(t, Mapping)]
    active_thread = next((t for t in threads if t.get("status") == "active"), None)
    ledgers = safe.get("ledgers") or {}

    sections: list[dict[str, Any]] = []
    badge = "平静日常 · 等待拨动丝线"
    summary = f"附着于「{realm_name}」的命运织机正在悄然记录每位居民的足迹与心愿。"

    if active_thread is not None:
        phase = str(active_thread.get("phase") or "spark")
        tension_pct = int(round(float(active_thread.get("tension") or 0.0) * 100))
        badge = f"{PHASE_LABELS.get(phase, phase)} · 张力 {tension_pct}%"
        summary = f"《{active_thread.get('title')}》：{active_thread.get('premise')}"
        active_nid = str(active_thread.get("active_node_id") or "")
        node = next((n for n in (active_thread.get("nodes") or ()) if n.get("node_id") == active_nid), None)
        if node is not None:
            choice_items: list[str] = []
            for rec in (node.get("participant_choices") or {}).values():
                if not isinstance(rec, Mapping):
                    continue
                st_lbl = rec.get("stance_label") or rec.get("stance") or "观察"
                exec_st = str(rec.get("execution_status") or "")
                exec_tag = "✓" if exec_st == "completed" else ("→" if exec_st == "pending" else "")
                line = f"{exec_tag}{rec.get('actor_name')} [{st_lbl}]：{rec.get('choice_summary')}"
                if rec.get("inner_reason"):
                    line += f" ｜ 心路：{rec['inner_reason']}"
                elif rec.get("inner_hidden"):
                    line += " ｜ （心路已收起，点击下方按钮或待收束揭晓）"
                choice_items.append(line)
            if node.get("player_stance"):
                ps = node["player_stance"]
                choice_items.append(f"你 [{STANCE_LABELS.get(ps.get('stance'), ps.get('stance'))}]：{ps.get('note') or '已表态'}")
            fl = str(node.get("target_floor_label") or "").strip()
            sname = str(node.get("target_space_name") or "").strip()
            s_with_fl = f"{sname}({fl})" if fl and fl not in sname else sname
            loc_str = f"{s_with_fl}{('·' + node['target_zone_name']) if node.get('target_zone_name') else ''}"
            max_n = int(active_thread.get("max_nodes") or 0)
            step_lbl = f"第 {node.get('seq', 1)}/{max_n} 拍" if max_n > 0 else f"第 {node.get('seq', 1)} 幕"
            sections.append({
                "heading": f"{step_lbl} · {node.get('title')} ({loc_str or '小镇'})",
                "body": f"{node.get('situation')}\n十字路口：{node.get('dilemma_frame')}",
                "items": choice_items or ["命运丝线已落位，核心角色正在迈出下一步…"],
            })
    elif threads:
        last_t = threads[-1]
        cc = last_t.get("chapter_chronicle") if isinstance(last_t.get("chapter_chronicle"), Mapping) else {}
        badge = f"已收束 · {last_t.get('title')}"
        summary = str(cc.get("epilogue") or "最近一条命运丝线已安静落笔。")

    resident_items: list[str] = []
    for ledger in ledgers.values():
        if not isinstance(ledger, Mapping):
            continue
        name = str(ledger.get("name") or ledger.get("actor_id") or "")
        exps = list(ledger.get("experienced_events") or ())
        recent = exps[-1]["summary"] if exps else "暂无显著记事"
        arc = ledger.get("arc_state") or {}
        asp = (arc.get("aspirations") or ())[-1] if arc.get("aspirations") else ""
        line = f"{name}：{recent}"
        if asp:
            line += f" ｜ 念想：{asp}"
        resident_items.append(line)
    if resident_items:
        sections.append({"heading": "角色非全知个人记事（L1 亲历 / L3 念想）", "items": resident_items[:10]})

    archives = [c for c in (safe.get("chronicle_archive") or ()) if isinstance(c, Mapping)]
    if archives:
        latest_c = archives[-1]
        sections.append({
            "heading": str(latest_c.get("title") or "最近章节纪事"),
            "body": str(latest_c.get("epilogue") or ""),
            "items": list(latest_c.get("key_turns") or ())[-5:],
        })

    is_observer = safe.get("experience_mode") == "observer"
    stance_cards = []
    if active_thread is not None and not is_observer:
        stance_cards = [
            {"id": st, "title": STANCE_LABELS[st], "tag": "可选表态", "subtitle": hint}
            for st, hint in (
                ("investigate", "主动过去查看细节、问清来龙去脉"),
                ("confide", "找信任的人把想法或担心摊开商量"),
                ("protect", "优先照看同伴、替对方稳住局面"),
                ("pursue_dream", "顺着心里的念想动手把一件事做成"),
                ("mediate", "温和沟通，试着化解彼此的误解或隔阂"),
                ("conceal", "暂且不动声色，把察觉到的事收在心里"),
            )
        ]

    actions = []
    if active_thread is not None:
        if not is_observer:
            actions.append({"action_id": "record_player_stance", "label": "记下我的态度", "style": "secondary", "required_selection": 1})
        actions.append({"action_id": "advance_fate_beat", "label": "推进下一拍", "style": "primary"})
    else:
        actions.append({"action_id": "pluck_fate_thread", "label": "拨动一条新丝线", "style": "primary"})
    actions.append({
        "action_id": "toggle_inner_thoughts",
        "label": "隐藏角色心路" if reveal else "查看角色心路",
        "style": "secondary",
    })
    actions.append({"action_id": "refresh_fate", "label": "刷新", "style": "secondary"})

    ctx_node = active_thread.get("active_node_id") if active_thread else len(archives)
    return {
        "view_only": is_observer,
        "live_autonomy": True,
        "title": f"命运编年史 · {realm_name}",
        "badge": badge,
        "summary": summary,
        "sections": sections,
        **({"cards": stance_cards, "select_limit": 1} if stance_cards else {}),
        "actions": actions,
        "context_id": f"{safe.get('session_id')}:{ctx_node}:{int(reveal)}:{len(resident_items)}",
    }
