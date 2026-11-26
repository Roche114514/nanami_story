"""角色节点自主抉择引擎：基于人格底色与非全知个人账本，让角色在命运节点做出自己的选择。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .adapters.llm_adapter import ask_llm_json
from .character_chronicle import active_director_whispers, summarize_ledger_for_prompt
from .fate_schema import VALID_STANCES, clip_text, normalize_choice_record


_DIALOGUE_APPROACHES = {
    "investigate": ("probe", "留意对方绕开的细节，找机会轻声求证"),
    "confront": ("confront", "抓住最难回避的一点，等合适的时机问清"),
    "protect": ("deflect", "先缓和眼前的压力，给彼此留些余地"),
    "confide": ("confide", "确认对方愿意听，再试着透露一点自己的感受"),
    "conceal": ("conceal", "先把最难启齿的部分留在心里"),
    "withdraw": ("silent_observe", "允许沉默，先观察对方接下来的反应"),
    "pursue_dream": ("propose_shift", "寻找眼下双方都愿意试的一小步"),
    "mediate": ("propose_shift", "顺着双方都认同的地方尝试缓和分歧"),
}


def decide_dialogue_actor_choice(
    session: Mapping[str, Any], thread: Mapping[str, Any], node: Mapping[str, Any],
    actor_id: str, persona_adapter: Any, *, use_llm: bool = True, llm_fn=None,
    custom_persona_dir: str = "",
) -> dict[str, Any]:
    ledger = session["ledgers"][actor_id]
    name = ledger.get("host_character") or ledger["name"]
    options = {"custom_persona_dir": custom_persona_dir}
    profile = persona_adapter.read_character_profile(name, **options) if persona_adapter else {}
    relations = persona_adapter.read_character_relations(name, **options) if persona_adapter else {}
    traits = profile.get("traits_summary") or ledger.get("traits_summary") or {}
    stance, _ = _score_stances_offline(
        traits, thread["theme_compass"], relations, ledger.get("choice_records", []),
    )
    kind, direction = _DIALOGUE_APPROACHES[stance]
    role = (thread.get("resident_roles") or {}).get(actor_id, ledger.get("story_role", {}))
    offline = {
        "stance": stance, "choice_summary": direction,
        "inner_reason": role.get("relationship_to_incident") or f"此刻更愿意{direction}。",
        "dialogue_intent": {"kind": kind, "target": "", "hint": direction},
        "speech_seed": direction,
        "arc_delta": {"aspirations": [direction]},
    }
    result = offline
    is_observer = session.get("experience_mode") == "observer"
    if use_llm:
        context = {
            "character": name, "soul_text": profile.get("soul_text") or ledger.get("persona_brief"),
            "traits_summary": traits, "style_tags": profile.get("style_tags") or ledger.get("style_tags"),
            "relations": relations, "story_role": role,
            "experience_mode": session.get("experience_mode", "co_play"),
            "other_cast": [item for item in session.get("dialogue_cast", []) if item != name],
            "personal_chronicle": summarize_ledger_for_prompt(ledger, max_events=5, max_choices=3)
            if ledger.get("experienced_events") or ledger.get("choice_records") else "",
            "situation": node["situation"], "dilemma_frame": node["dilemma_frame"],
            "public_incident_fact": thread.get("public_incident_fact"),
            "director_whispers": active_director_whispers(session, name),
        }
        system_prompt = (
            "根据单个角色的视角写其尚未付诸行动的对话意图。只返回 JSON，包含 stance、"
            "choice_summary、inner_reason、dialogue_intent {kind,target,hint}、speech_seed、arc_delta。"
            "kind 用 probe/confide/conceal/confront/deflect/propose_shift/silent_observe。"
            "speech_seed 和 hint 只能写语气、时机或交谈方向，禁止写可照念的对白。"
            "arc_delta 只能记录内在意向，禁止捏造已说的承诺、事实或已发生的行为。"
        )
        if is_observer:
            system_prompt += "当前为纯观剧模式，玩家不在场；交谈对象仅限其他在场角色（或独处自省），target 不要指向玩家。"
        try:
            raw = (llm_fn or ask_llm_json)(
                system_prompt,
                json.dumps(context, ensure_ascii=False),
            )
        except Exception:
            raw = None
        if isinstance(raw, Mapping) and isinstance(raw.get("dialogue_intent"), Mapping):
            result = {**offline, **{key: raw[key] for key in offline if key in raw}}
            intent = dict(result["dialogue_intent"])
            # Keep a model-provided direction only when it is phrased as guidance.
            for key, value in (("speech_seed", result["speech_seed"]), ("hint", intent.get("hint"))):
                if not isinstance(value, str) or not re.match(
                    r"^(?:先|试着|可以|留意|观察|倾向|等|用|以|视|保持|避免|轻声|轻轻|给|顺着|找|从|让|若)",
                    value.strip(),
                ) or re.search(r'["“”「」『』\n]', value):
                    value = direction
                if key == "hint":
                    intent[key] = value
                else:
                    result[key] = value
            result["dialogue_intent"] = intent
            arc = result["arc_delta"]
            result["arc_delta"] = {"aspirations": arc.get("aspirations", [])} if isinstance(arc, Mapping) else {}
    return normalize_choice_record(
        result, actor_id=actor_id, actor_name=name, thread_id=thread["thread_id"],
        node_id=node["node_id"], node_title=node["title"],
    )


def _grounded_catalog(
    actor_id: str,
    world_state: Mapping[str, Any],
    node: Mapping[str, Any],
) -> dict[str, Any]:
    spaces = {
        str(sp.get("space_id") or ""): sp
        for sp in (world_state.get("spaces") or ())
        if isinstance(sp, Mapping) and sp.get("space_id")
    }
    actors = {
        str(a.get("actor_id") or ""): a
        for a in (world_state.get("actors") or ())
        if isinstance(a, Mapping) and a.get("actor_id")
    }
    entities = {
        str(e.get("entity_id") or ""): e
        for e in (world_state.get("realm_entities") or world_state.get("entities") or ())
        if isinstance(e, Mapping) and e.get("entity_id")
    }
    me = actors.get(actor_id) or {}
    my_sid = str(me.get("space_id") or "")
    my_zid = str(me.get("zone_id") or "")
    target_sid = str(node.get("target_space_id") or "")
    deployed = set(str(x) for x in (node.get("deployed_entity_ids") or ()) if str(x))

    deployed_entities: list[dict[str, str]] = []
    other_entities: list[dict[str, str]] = []
    for eid, ent in entities.items():
        esid = str(ent.get("space_id") or "")
        pid = str(ent.get("parent_id") or "")
        if pid.startswith("inventory:") and pid != f"inventory:{actor_id}":
            continue
        traits = ent.get("traits") or {}
        tags = [str(t) for t in (ent.get("tags") or ())]
        if ("portal" in traits or "interior_door" in tags) and eid not in deployed:
            continue
        if eid in deployed or esid in (target_sid, my_sid):
            row = {
                "entity_id": eid,
                "name": str(ent.get("name") or eid),
                "space_id": esid,
                "brief": clip_text(ent.get("brief"), 80, ""),
                "action_hint": "read" if "inscribable" in traits else ("open" if "container" in traits else "look"),
            }
            if eid in deployed:
                deployed_entities.append(row)
            else:
                other_entities.append(row)
    relevant_entities = deployed_entities + other_entities

    peers = []
    for aid, a in actors.items():
        if aid == actor_id or a.get("actor_status", "active") != "active":
            continue
        same_room = bool(my_sid and a.get("space_id") == my_sid and (not my_zid or a.get("zone_id") == my_zid))
        coords = (me.get("x"), me.get("y"), a.get("x"), a.get("y"))
        nearby = same_room and all(isinstance(v, (int, float)) for v in coords) and max(
            abs(coords[0] - coords[2]), abs(coords[1] - coords[3]),
        ) <= 1
        peers.append({
            "actor_id": aid,
            "name": str(a.get("name") or aid),
            "is_player": bool(a.get("is_player", False)),
            "space_id": str(a.get("space_id") or ""),
            "same_room": same_room,
            "nearby": nearby,
        })

    return {
        "me": me,
        "my_space": spaces.get(my_sid) or {},
        "spaces": spaces,
        "entities": entities,
        "relevant_entities": relevant_entities[:12],
        "actors": actors,
        "peers": peers,
    }


def _ground_world_intent(
    raw_intent: Any,
    actor_id: str,
    catalog: Mapping[str, Any],
) -> dict[str, str]:
    src = raw_intent if isinstance(raw_intent, Mapping) else {}
    kind = str(src.get("kind") or "observe").strip().lower()
    raw_target = clip_text(src.get("target"), 80, "")
    action = clip_text(src.get("action"), 24, "").lower()
    reason = clip_text(src.get("reason"), 48, "")
    note_text = clip_text(src.get("note_text"), 120, "")

    spaces = catalog["spaces"]
    entities = catalog["entities"]
    actors = catalog["actors"]

    if kind == "visit" and raw_target:
        if raw_target not in spaces:
            matched = next(
                (sid for sid, sp in spaces.items() if raw_target in str(sp.get("name") or "") or str(sp.get("floor_label") or "") == raw_target),
                "",
            )
            raw_target = matched
        if raw_target in spaces:
            return {"kind": "visit", "target": raw_target, "action": "", "reason": reason, "note_text": note_text}

    elif kind == "entity" and raw_target:
        if raw_target not in entities:
            matched = next(
                (eid for eid, ent in entities.items() if raw_target in str(ent.get("name") or "")),
                "",
            )
            raw_target = matched
        if raw_target in entities:
            ent = entities[raw_target]
            traits = ent.get("traits") or {}
            if action not in ("look", "read", "open", "close", "take", "write"):
                action = "read" if "inscribable" in traits else "look"
            return {"kind": "entity", "target": raw_target, "action": action, "reason": reason, "note_text": note_text}

    elif kind in ("social", "seek_player") and raw_target:
        if raw_target not in actors:
            matched = next(
                (aid for aid, a in actors.items() if aid != actor_id and raw_target in str(a.get("name") or "")),
                "",
            )
            raw_target = matched
        if raw_target in actors and raw_target != actor_id:
            tgt_actor = actors[raw_target]
            eff_kind = "seek_player" if tgt_actor.get("is_player") else "social"
            return {"kind": eff_kind, "target": raw_target, "action": "", "reason": reason, "note_text": note_text}

    return {"kind": "observe", "target": "", "action": "", "reason": reason or "暂且留心观察周围动静", "note_text": ""}


def _score_stances_offline(
    traits: Mapping[str, Any],
    theme_compass: str,
    relations: Mapping[str, Mapping[str, Any]],
    prior_choices: list[Mapping[str, Any]],
    *,
    is_native_npc: bool = False,
) -> tuple[str, dict[str, float]]:
    safety = float(traits.get("safety_need", 0.52))
    surgency = float(traits.get("surgency", 0.40))
    effortful = float(traits.get("effortful_control", 0.72))
    prosocial = float(traits.get("prosocial", 0.68))
    relatedness = float(traits.get("relatedness_need", 0.62))
    avoidance = float(traits.get("avoidance", 0.45))
    competence = float(traits.get("competence_need", 0.65))
    norm = float(traits.get("norm_compliance", 0.70))

    avg_warmth = (
        sum(float(r.get("warmth", 0.45)) for r in relations.values()) / len(relations)
        if relations else 0.48
    )
    avg_trust = (
        sum(float(r.get("trust", 0.55)) for r in relations.values()) / len(relations)
        if relations else 0.55
    )

    scores: dict[str, float] = {
        "investigate": 0.45 * competence + 0.42 * surgency + 0.25 * (1.0 - avoidance) + (0.22 if theme_compass == "mystery_ripple" else 0.0),
        "confront": 0.50 * surgency + 0.28 * competence + 0.22 * (1.0 - safety) + (0.18 if theme_compass in ("mystery_ripple", "crisis_test") else 0.0),
        "protect": 0.48 * prosocial + 0.38 * relatedness + 0.24 * effortful + (0.18 if theme_compass == "crisis_test" else 0.0),
        "confide": 0.45 * relatedness + 0.35 * avg_warmth + 0.30 * (1.0 - avoidance) + (0.22 if theme_compass == "bond_turning" else 0.0),
        "pursue_dream": 0.45 * competence + 0.35 * prosocial + 0.25 * surgency + (0.28 if theme_compass == "daily_aspiration" else 0.0),
        "mediate": 0.42 * norm + 0.38 * prosocial + 0.25 * avg_trust + (0.14 if theme_compass == "bond_turning" else 0.0),
        "conceal": 0.46 * safety + 0.40 * avoidance + 0.25 * (1.0 - surgency) + 0.12 * effortful,
        "withdraw": 0.50 * safety + 0.45 * avoidance + 0.30 * (1.0 - surgency),
    }

    if is_native_npc and theme_compass in ("mystery_ripple", "crisis_test"):
        scores["pursue_dream"] -= 0.38
        scores["investigate"] -= 0.12
        scores["conceal"] += 0.10
        scores["confide"] += 0.06
        scores["protect"] += 0.06
        scores["confront"] += 0.06

    # 避免连续同一姿态机械重复（仅做轻度惯性/新鲜度平衡）
    if prior_choices:
        last_stance = str(prior_choices[-1].get("stance") or "")
        if last_stance in scores:
            scores[last_stance] -= 0.08

    chosen = max(VALID_STANCES, key=lambda st: (round(scores.get(st, 0.0), 4), -VALID_STANCES.index(st)))
    return chosen, scores


def _decide_offline(
    session: Mapping[str, Any],
    thread: Mapping[str, Any],
    node: Mapping[str, Any],
    actor_id: str,
    ledger: Mapping[str, Any],
    profile: Mapping[str, Any],
    relations: Mapping[str, Mapping[str, Any]],
    catalog: Mapping[str, Any],
    sim_min: float,
) -> dict[str, Any]:
    traits = profile.get("traits_summary") if isinstance(profile.get("traits_summary"), Mapping) else {}
    theme = str(thread.get("theme_compass") or "daily_aspiration")
    is_native = bool(ledger.get("is_native_npc", False))
    story_role = ledger.get("story_role") if isinstance(ledger.get("story_role"), Mapping) else {}
    role_label = str(story_role.get("local_role") or "")
    stake_note = str(story_role.get("relationship_to_incident") or "")
    node_title = str(node.get("title") or thread.get("title") or "眼前的事件")
    prior_choices = [c for c in (ledger.get("choice_records") or ()) if isinstance(c, Mapping)]
    stance, _scores = _score_stances_offline(
        traits, theme, relations, prior_choices, is_native_npc=is_native,
    )

    actor_name = str(ledger.get("name") or actor_id)
    me = catalog["me"]
    my_sid = str(me.get("space_id") or "")
    target_sid = str(node.get("target_space_id") or my_sid)
    target_sname = str(node.get("target_space_name") or (catalog["spaces"].get(target_sid) or {}).get("name") or "现场")
    floor_lbl = str(node.get("target_floor_label") or "")
    target_loc_label = f"{target_sname}({floor_lbl})" if floor_lbl and floor_lbl not in target_sname else target_sname

    rel_ents = catalog["relevant_entities"]
    deployed = set(str(x) for x in (node.get("deployed_entity_ids") or ()) if str(x))
    focus_ent = next((e for e in rel_ents if e["entity_id"] in deployed), None) or (rel_ents[0] if rel_ents else None)
    peers = catalog["peers"]
    player_peer = next((p for p in peers if p.get("is_player")), None)
    def _npc_affinity(peer: Mapping[str, Any]) -> float:
        info = relations.get(peer["name"])
        score = 0.9
        if isinstance(info, Mapping):
            if not info.get("known", True):
                score = 0.15
            else:
                st = str(info.get("rel_stage") or "").strip().lower()
                if st == "hostile":
                    score = -1.0
                elif st in ("rival", "guarded"):
                    score = -0.2
                else:
                    score = (
                        float(info.get("trust", 0.55))
                        + float(info.get("warmth", 0.45))
                        + 0.5 * float(info.get("affective_valence", 0.0))
                    )
        last_tgt = str((prior_choices[-1].get("world_intent") or {}).get("target") or "") if prior_choices else ""
        if last_tgt and peer["actor_id"] == last_tgt and prior_choices[-1].get("execution_status") == "completed":
            score -= 2.5
        return score

    best_npc = max(
        [p for p in peers if not p.get("is_player")],
        key=_npc_affinity,
        default=None,
    )
    trusted_peer = best_npc or player_peer

    safety = float(traits.get("safety_need", 0.52))
    courage_break = safety >= 0.55 and stance in ("confront", "protect", "investigate")

    intent: dict[str, str] = {"kind": "observe", "target": "", "action": "", "reason": "原地观察局势", "note_text": ""}
    arc_delta: dict[str, Any] = {"aspirations": [], "promises": [], "secrets": [], "turning_points": [], "stance_toward": {}}
    speech_seed = ""
    inner_reason = "眼前的事情还不明朗，先稳住呼吸看清周围人的反应再决定下一步。"

    if stance in ("investigate", "pursue_dream"):
        if focus_ent is not None:
            intent = {
                "kind": "entity",
                "target": focus_ent["entity_id"],
                "action": focus_ent["action_hint"],
                "reason": clip_text(f"查看「{focus_ent['name']}」", 48),
                "note_text": "",
            }
            choice_summary = (
                f"{actor_name}决定前往{target_loc_label}亲自查看「{focus_ent['name']}」。"
                if stance == "investigate"
                else f"{actor_name}决定把心中的念想落到实处，走向{target_loc_label}的「{focus_ent['name']}」。"
            )
            speech_seed = f"{target_loc_label}里那件「{focus_ent['name']}」的细节很关键，我打算过去核实清楚。"
        elif target_sid and target_sid in catalog["spaces"]:
            intent = {"kind": "visit", "target": target_sid, "action": "", "reason": f"去{target_sname}看看", "note_text": ""}
            choice_summary = f"{actor_name}决定动身前往{target_loc_label}，亲自核实「{node_title}」的现场情况。"
            speech_seed = f"我想亲自去{target_loc_label}看看「{node_title}」到底是什么状况。"
        else:
            choice_summary = f"{actor_name}决定留心搜集眼前的具体线索，理清事件来龙去脉。"
            speech_seed = f"关于「{node_title}」，我觉得还有关键细节没对上。"
        inner_reason = (
            f"哪怕心里有点忐忑，也不能放任猜测乱传。亲自确认过{target_loc_label}的情况，才能心里有底。"
            if courage_break
            else (
                f"身为{role_label or '本镇居民'}，{stake_note or target_loc_label + '的事'}直接牵扯到我熟悉的街坊，我得亲眼看个明白。"
                if is_native and theme in ("mystery_ripple", "crisis_test")
                else f"{target_loc_label}的线索摆在眼前，顺着物证核实清楚才不会走偏。"
            )
        )
        if stance == "pursue_dream":
            arc_delta["aspirations"].append(f"想亲手把「{thread.get('title') or target_sname}」这件事落到实处")
        arc_delta["turning_points"].append(f"选择不再观望，迈步前往{target_loc_label}")

    elif stance in ("confide", "mediate"):
        if trusted_peer is not None:
            intent = {
                "kind": "seek_player" if trusted_peer.get("is_player") else "social",
                "target": trusted_peer["actor_id"],
                "action": "",
                "reason": clip_text(f"找{trusted_peer['name']}对一对消息", 48),
                "note_text": "",
            }
            pname = trusted_peer["name"]
            choice_summary = f"{actor_name}决定主动去找{pname}，把围绕「{node_title}」的线索与各自的顾虑当面摊开商量。"
            inner_reason = (
                f"{stake_note or '这件事'}压在心头只会让人更加不安，如果是{pname}的话，兴许能一起想出稳妥的办法。"
                if is_native and stake_note
                else f"光靠自己憋在心里只会越猜越偏。如果是{pname}的话，应该能听懂我的顾虑。"
            )
            speech_seed = f"关于{target_loc_label}「{node_title}」这件事……我想和你核对一下彼此知道的情况。"
            arc_delta["stance_toward"][pname] = "觉得遇到事情时可以试着把想法说给对方听"
            arc_delta["promises"].append(f"与{pname}商量如何面对「{thread.get('title') or '眼前的事'}」")
        else:
            choice_summary = f"{actor_name}决定放缓语气，试着先稳住各方紧张的情绪。"
            inner_reason = "大家如果彼此提防却谁都不肯开口，只怕会让真正重要的线索白白溜走。"
            speech_seed = f"关于「{node_title}」，大家都别急着下定论，先把各自经过的时段说清楚吧。"

    elif stance in ("protect", "confront"):
        if stance == "protect" and best_npc is not None:
            intent = {
                "kind": "social",
                "target": best_npc["actor_id"],
                "action": "",
                "reason": clip_text(f"确认{best_npc['name']}眼下安危", 48),
                "note_text": "",
            }
            pname = best_npc["name"]
            choice_summary = f"{actor_name}决定先站出来照应{pname}，无论风波多大都不让熟人独自面对。"
            inner_reason = (
                f"其实我心里也在打鼓……但如果连我都不往前站一步，{pname}现在肯定更孤立。"
                if courage_break
                else (
                    f"{stake_note or '眼下的风波太急'}——无论如何我得先确认{pname}平平安安的。"
                    if is_native
                    else f"比起事件本身，我更在意{pname}会不会卷进危险里。先确认好对方没事再说。"
                )
            )
            speech_seed = f"{pname}，你现在还好吗？「{node_title}」这事别一个人扛着。"
            arc_delta["promises"].append(f"关键时刻不想丢下{pname}不管")
            arc_delta["turning_points"].append(f"在「{node_title}」顶住不安选择照应{pname}")
        elif focus_ent is not None:
            intent = {
                "kind": "entity",
                "target": focus_ent["entity_id"],
                "action": focus_ent["action_hint"],
                "reason": clip_text(f"当面核对「{focus_ent['name']}」", 48),
                "note_text": "",
            }
            choice_summary = f"{actor_name}决定不兜圈子，直接去{target_loc_label}核对「{focus_ent['name']}」，免得被人平白猜忌。"
            inner_reason = (
                f"既然牵扯到「{stake_note or target_loc_label}」，退缩反而显得心里有鬼，我必须当场核对「{focus_ent['name']}」。"
                if is_native
                else f"与其站在一旁胡乱猜疑，不如亲自去看看「{focus_ent['name']}」到底写着什么。"
            )
            speech_seed = f"我想把话摊开说清楚，先一起去确认一下{target_loc_label}的「{focus_ent['name']}」。"
            arc_delta["turning_points"].append(f"在「{node_title}」选择直面「{focus_ent['name']}」核实真相")
        elif target_sid and target_sid != my_sid and target_sid in catalog["spaces"]:
            intent = {"kind": "visit", "target": target_sid, "action": "", "reason": f"去{target_sname}当面问清楚", "note_text": ""}
            choice_summary = f"{actor_name}决定不兜圈子，直接前往{target_loc_label}把话说开。"
            inner_reason = "与其互相猜忌拖下去，不如当面问个清楚。"
            speech_seed = f"有些涉及{target_loc_label}的事，我想当面说清楚。"
            arc_delta["turning_points"].append(f"在「{node_title}」选择正面迎上去问清楚")
        else:
            choice_summary = f"{actor_name}决定守住自己的立场，不再被动退让。"
            speech_seed = f"「{node_title}」既然已经摆在桌面上，大家不如直说自己的行踪。"

    elif stance == "conceal":
        quiet_sid = next(
            (sid for sid, sp in catalog["spaces"].items() if sp.get("floor_index", 0) >= 1 and sid != target_sid and sp.get("space_kind") != "town"),
            my_sid,
        )
        if quiet_sid and quiet_sid != my_sid:
            intent = {"kind": "visit", "target": quiet_sid, "action": "", "reason": "暂避旁人先把实情压一压", "note_text": ""}
        choice_summary = f"{actor_name}决定暂不吐露自己知道的隐情，先看清众人对「{node_title}」的反应再作打算。"
        inner_reason = (
            f"「{stake_note}」——这事现在还说不清楚，贸然说出口只会把自己或熟人推上风口浪尖，我得先按住不动。"
            if stake_note
            else "现在局势不明，贸然把手里掌握的底牌抖出来太冒险了，先观察大家的言行再说。"
        )
        speech_seed = f"关于「{node_title}」……我当时并不清楚全部经过，还是先听听别人怎么说吧。"
        arc_delta["secrets"].append(f"对「{node_title}」一事暂且选择隐瞒部分实情")

    else:  # withdraw
        home_cand = str(story_role.get("home_space_id") or "") or next(
            (sid for sid, sp in catalog["spaces"].items() if sp.get("floor_index", 0) >= 1 and sp.get("space_kind") != "town"),
            my_sid,
        )
        if home_cand and home_cand != my_sid and home_cand in catalog["spaces"]:
            intent = {"kind": "visit", "target": home_cand, "action": "", "reason": "回自己的住处暂避理顺思路", "note_text": ""}
        choice_summary = f"{actor_name}决定暂且退回安静处所避开争执，独自消化刚才发生的事。"
        inner_reason = "眼下的指认和气氛都太紧绷，我必须先一个人静下来想想该如何护住自己的安稳。"
        speech_seed = "我先回自己常待的屋里静一静，稍后再出来照面。"

    grounded = _ground_world_intent(intent, actor_id, catalog)
    return normalize_choice_record(
        {
            "actor_name": actor_name,
            "stance": stance,
            "choice_summary": choice_summary,
            "inner_reason": inner_reason,
            "world_intent": grounded,
            "speech_seed": speech_seed,
            "arc_delta": arc_delta,
        },
        actor_id=actor_id,
        actor_name=actor_name,
        thread_id=str(thread.get("thread_id") or ""),
        node_id=str(node.get("node_id") or ""),
        node_title=str(node.get("title") or ""),
        sim_min=sim_min,
    )


def _normalize_world_relations(
    raw_rels: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_rels, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for tkey, row in raw_rels.items():
        if not isinstance(row, Mapping):
            continue
        k = str(tkey or "").strip()
        if k == "user":
            tname = str(row.get("target_name") or "用户").strip() or "用户"
        elif k.startswith("char:"):
            tname = str(row.get("target_name") or k.split(":", 1)[1]).strip()
        else:
            tname = str(row.get("target_name") or k).strip()
        if not tname:
            continue
        out[tname] = {
            "target_type": str(row.get("target_type") or ("user" if k == "user" else "character")),
            "known": bool(row.get("known", False)),
            "rel_stage": str(row.get("rel_stage") or "stranger").strip().lower(),
            "attitude_stance": str(row.get("attitude_stance") or "neutral").strip().lower(),
            "call_name": str(row.get("call_name") or "").strip()[:24],
            "relation_tag": str(row.get("relation_tag") or "").strip()[:40],
            "affective_valence": float(row.get("affective_valence") or 0.0),
            "trust": float(row.get("trust") if row.get("trust") is not None else 0.55),
            "warmth": float(row.get("warmth") if row.get("warmth") is not None else 0.45),
            "social_willingness": float(row.get("social_willingness") if row.get("social_willingness") is not None else 0.50),
            "shared_memories": clip_text(row.get("shared_memories"), 140, ""),
        }
    return out


def decide_actor_choice(
    session: Mapping[str, Any],
    thread: Mapping[str, Any],
    node: Mapping[str, Any],
    actor_id: str,
    world_state: Mapping[str, Any],
    persona_adapter: Any,
    *,
    llm_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
    use_llm: bool = True,
    custom_persona_dir: str = "",
    world_relations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """让单个角色仅基于自身人格、情感关系与个人账本，对当前故事节点做出自主抉择。"""
    ledger = (session.get("ledgers") or {}).get(actor_id) or {}
    actor_name = str(ledger.get("name") or actor_id)
    host_char = str(ledger.get("host_character") or "").strip()
    lookup_name = host_char or actor_name

    profile: dict[str, Any] = {}
    relations: dict[str, dict[str, Any]] = _normalize_world_relations(world_relations)
    if persona_adapter is not None and not ledger.get("is_native_npc"):
        try:
            profile = persona_adapter.read_character_profile(lookup_name, custom_persona_dir=custom_persona_dir)
        except Exception:
            profile = {}
        if not relations:
            try:
                relations = persona_adapter.read_character_relations(lookup_name, custom_persona_dir=custom_persona_dir)
            except Exception:
                relations = {}
    if (ledger.get("is_native_npc") or not profile.get("has_traits_file")) and ledger.get("traits_summary"):
        profile = dict(profile or {})
        profile["traits_summary"] = dict(ledger["traits_summary"])
        if ledger.get("style_tags"):
            profile["style_tags"] = list(ledger["style_tags"])
        if ledger.get("persona_brief") and not profile.get("soul_text"):
            profile["soul_text"] = str(ledger["persona_brief"])

    catalog = _grounded_catalog(actor_id, world_state, node)
    sim_min = float(world_state.get("minutes") if world_state.get("minutes") is not None else session.get("last_sim_min") or 0.0)

    if use_llm:
        caller = llm_fn or (
            lambda sys_p, usr_p: ask_llm_json(sys_p, usr_p, timeout_sec=20.0, max_tokens=1000)
        )
        me = catalog["me"]
        my_sp = catalog["my_space"]
        weather_desc = str((world_state.get("weather") or {}).get("label") or "晴朗")
        lighting_desc = str((world_state.get("lighting") or {}).get("period") or "日常")
        peer_names = {p["name"] for p in catalog["peers"]}
        rel_items: list[str] = []
        for name, info in list(relations.items())[:10]:
            is_known = info.get("known", True)
            if not is_known:
                if name in peer_names or info.get("target_type") == "user":
                    rel_items.append(f"{name}(尚不认识)")
                continue
            call_str = f"·称「{info['call_name']}」" if info.get("call_name") else ""
            tag_str = f"·{info['relation_tag']}" if info.get("relation_tag") else ""
            mem_str = f"·记忆:{info['shared_memories'][:48]}" if info.get("shared_memories") else ""
            rel_items.append(
                f"{name}({info.get('rel_stage')}/{info.get('attitude_stance', 'neutral')}"
                f"/好感{float(info.get('affective_valence', 0.0)):+.2f}/信任{float(info.get('trust', 0.55)):.2f}"
                f"{call_str}{tag_str}{mem_str})"
            )
        rel_txt = "；".join(rel_items) or ("街坊熟识为主" if ledger.get("is_native_npc") else "只认识同行旅伴，与镇民初见")
        spaces_opt = [
            f"{sid} -> {sp.get('name')}({sp.get('floor_label') or '1F'})"
            for sid, sp in list(catalog["spaces"].items())[:12]
        ]
        ents_opt = [
            f"{e['entity_id']} -> {e['name']}{' [' + e['brief'] + ']' if e.get('brief') else ''} (推荐动作:{e['action_hint']})"
            for e in catalog["relevant_entities"]
        ]
        peers_opt = [
            f"{p['actor_id']} -> {p['name']}{'(玩家)' if p['is_player'] else ''} "
            + ("[在跟前]" if p["nearby"] else "[同一区域，未确认在跟前]" if p["same_room"] else "[当前不在跟前，可之后去找]")
            for p in catalog["peers"]
        ]
        stake = ((thread.get("cast_stakes") or {}).get(actor_id) or {}).get("stake") or ""
        sr = ledger.get("story_role") if isinstance(ledger.get("story_role"), Mapping) else {}
        if ledger.get("is_native_npc"):
            role_desc = (
                f"本镇常住居民（职业/身份：{sr.get('local_role') or '本地住户'}，常驻：{sr.get('home_space_name') or '本镇'}）。"
                f"你是事件里的本地当事人/知情街坊（利害关系：{sr.get('relationship_to_incident') or '关心镇上安危'}），"
                "切忌把自己演成刚来探案的外来侦探或问候‘第一次来这里’！"
            )
        elif str(sr.get("role_category") or "") == "protagonist":
            role_desc = (
                f"本篇故事核心主角（{sr.get('local_role') or '当事主角'}；"
                f"处境：{sr.get('relationship_to_incident') or thread.get('premise') or '正身处事件之中'}）。"
            )
        else:
            role_desc = f"暂留本镇的外来旅人（并非本镇村民；背景：{thread.get('visitor_framing') or '因旅途或骤雨留驻本镇'}）。"

        system_prompt = (
            "你正站在角色的灵魂内部，协助她/他在这一刻做出真正属于自己的人生选择。\n"
            "原则：\n"
            "1. 这不是机械日常本能，也不是导演命令。允许角色因为在意的人、价值观、利害顾虑、好奇心或勇气跨出舒适区，也允许她保护秘密或暂避。\n"
            "2. 严格非全知：只能根据角色自己的灵魂底色、人际感情、亲历记忆和眼前事实作答，禁止未卜先知。\n"
            "本幕局势和十字路口可能描述预定情景；涉及此刻谁在跟前、谁已走来，只以【人物实时位置与可选靶点】为准。"
            "要和眼前之外的人谈，可以决定去找。内心与行为决定要写成接下来想做的事，不能写成已经碰面、已看到对方或已经说过。\n"
            "3. 严格接地：必须针对【已发生的公开事实】和具体可达的物件/人物/地点表态，优先查证 relevant_entities 中的具体物证，严禁编造‘好像楼上有奇怪动静’等空洞话术。\n"
            "4. 若决定找某人商量、求助、表白、护住或当面对质，请务必将 world_intent 设为 {kind:'social', target:'对方actor_id', reason:'...'}（不要只选 observe）；"
            "若【此前做过的选择】已标明‘已完成’，不要机械重复同一轮寒暄或调查，请顺着结果做出切实的下一步决定。\n"
            "5. world_intent 必须只从提供的可达 ID 中选择，或者选 kind='observe' 原地留意。\n"
            "仅输出一个 JSON 对象，字段包括：\n"
            "stance (confront|protect|confide|investigate|conceal|withdraw|pursue_dream|mediate), "
            "choice_summary (外部可见的行为决定<=80字), "
            "inner_reason (第一人称真实内心独白<=160字), "
            "world_intent: {kind: visit|entity|social|seek_player|observe, target, action, reason}, "
            "speech_seed (将来见到 world_intent.target 才会说的拟议开场<=60字), "
            "arc_delta: {aspirations: [], promises: [], secrets: [], turning_points: [], stance_toward: {}}。"
        )
        user_prompt = (
            f"【我是谁】{actor_name}\n"
            f"【身份与当事处境】{role_desc}\n"
            f"性格底色：{profile.get('soul_text') or '温和而有自己的坚持'}\n"
            f"气质标签：{'、'.join(profile.get('style_tags') or ())}\n"
            f"人际情感：{rel_txt}\n"
            f"【当前位置】{my_sp.get('name', '小镇')}（{my_sp.get('floor_label') or '1F'} / {me.get('zone_name') or '公共区域'}），"
            f"时段：{lighting_desc}，天气：{weather_desc}\n"
            f"【我的个人记事与念想】\n{summarize_ledger_for_prompt(ledger)}\n"
            f"【命运丝线】《{thread.get('title')}》（基调:{thread.get('theme_compass')}，与我相关：{stake or '身处其中'}）\n"
            f"已发生的公开事实：{thread.get('public_incident_fact') or thread.get('premise')}\n"
            f"【当前节点 · {node.get('title')}】\n局势：{node.get('situation')}\n十字路口：{node.get('dilemma_frame')}\n"
            f"【世界可选行动靶点】\n"
            f"- 地点：{'; '.join(spaces_opt) or '无'}\n"
            f"- 物件：{'; '.join(ents_opt) or '无'}\n"
            f"【人物实时位置与可选靶点】\n- 人物：{'; '.join(peers_opt) or '无'}\n"
        )
        focus = node.get("dialogue_focus") or {}
        participants = focus.get("actor_ids", ())
        if actor_id in participants:
            partner = next(aid for aid in participants if aid != actor_id)
            user_prompt += (
                f"【本幕当面要谈的事】{focus['topic']}，对方 ID={partner}。"
                "若愿意谈、想婉拒或想明确改期，请等实际见到这位对方再说。"
                "若 world_intent.target 就是这位对方，speech_seed 可写届时的第一句话。\n"
            )
        try:
            raw_res = caller(system_prompt, user_prompt)
        except Exception:
            raw_res = None
        if isinstance(raw_res, Mapping) and raw_res.get("choice_summary"):
            grounded_intent = _ground_world_intent(raw_res.get("world_intent"), actor_id, catalog)
            return normalize_choice_record(
                {
                    "actor_name": actor_name,
                    "stance": raw_res.get("stance"),
                    "choice_summary": raw_res.get("choice_summary"),
                    "inner_reason": raw_res.get("inner_reason"),
                    "world_intent": grounded_intent,
                    "speech_seed": raw_res.get("speech_seed"),
                    "arc_delta": raw_res.get("arc_delta"),
                },
                actor_id=actor_id,
                actor_name=actor_name,
                thread_id=str(thread.get("thread_id") or ""),
                node_id=str(node.get("node_id") or ""),
                node_title=str(node.get("title") or ""),
                sim_min=sim_min,
            )

    return _decide_offline(
        session, thread, node, actor_id, ledger, profile, relations, catalog, sim_min,
    )
