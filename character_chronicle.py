"""非全知全角色记事引擎：从世界事件流维护 L1 亲历 / L2 抉择 / L3 生命弧光，并判定节点触达。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .fate_schema import clip_text, make_character_ledger

_MAX_EVENTS_PER_LEDGER = 120
_MAX_ARC_ITEMS = 10


def _space_label(space: Mapping[str, Any] | None) -> str:
    if not isinstance(space, Mapping):
        return ""
    name = str(space.get("name") or space.get("space_id") or "")
    fl = str(space.get("floor_label") or "").strip()
    if fl and fl not in name:
        return f"{name}({fl})"
    return name


def _zone_at_point(space: Mapping[str, Any] | None, x: int, y: int) -> dict[str, Any] | None:
    if not isinstance(space, Mapping):
        return None
    ix, iy = int(x), int(y)
    best: dict[str, Any] | None = None
    best_area = 10**9
    for z in space.get("zones") or ():
        if not isinstance(z, Mapping):
            continue
        zx, zy = int(z.get("x") or 0), int(z.get("y") or 0)
        zw, zh = int(z.get("w") or 0), int(z.get("h") or 0)
        if zx <= ix < zx + zw and zy <= iy < zy + zh:
            area = zw * zh
            if area < best_area:
                best_area = area
                best = dict(z)
    return best


def _zone_is_enclosed(space: Mapping[str, Any] | None, zone_id: str) -> bool:
    if not isinstance(space, Mapping) or not zone_id:
        return False
    for z in space.get("zones") or ():
        if isinstance(z, Mapping) and str(z.get("zone_id") or "") == zone_id:
            return bool(z.get("enclosed", False))
    return False


def _same_compartment(
    space: Mapping[str, Any] | None,
    zid_a: str,
    zid_b: str,
) -> bool:
    if zid_a == zid_b:
        return True
    enc_a = _zone_is_enclosed(space, zid_a)
    enc_b = _zone_is_enclosed(space, zid_b)
    return not enc_a and not enc_b


def sync_cast_ledgers(
    session: dict[str, Any],
    world_state: Mapping[str, Any] | None,
    persona_adapter: Any = None,
    *,
    custom_persona_dir: str = "",
) -> bool:
    """确保当前世界中的所有居民（玩家与 AI 角色）都在 session['ledgers'] 中拥有账本。"""
    if not isinstance(world_state, Mapping):
        return False
    ledgers = session.setdefault("ledgers", {})
    changed = False
    for actor in world_state.get("actors") or ():
        if not isinstance(actor, Mapping):
            continue
        aid = str(actor.get("actor_id") or "").strip()
        if not aid:
            continue
        aname = str(actor.get("name") or aid).strip()
        host_char = str(actor.get("host_character") or "").strip()
        is_player = bool(actor.get("is_player", False))
        is_native = bool(actor.get("is_native_npc", False))
        if aid not in ledgers:
            profile: dict[str, Any] | None = None
            if not is_player and persona_adapter is not None:
                lookup_name = host_char or aname
                try:
                    profile = persona_adapter.read_character_profile(
                        lookup_name,
                        custom_persona_dir=custom_persona_dir,
                    )
                except Exception:
                    profile = None
            ledgers[aid] = make_character_ledger(
                aid,
                aname,
                host_character=host_char,
                is_player=is_player,
                is_native_npc=is_native,
                persona_profile=profile,
            )
            changed = True
        else:
            row = ledgers[aid]
            if aname and row.get("name") != aname:
                row["name"] = clip_text(aname, 32, row["name"])
                changed = True
    return changed


def _append_experience(
    ledger: dict[str, Any],
    *,
    seq: int,
    sim_min: float,
    kind: str,
    summary: str,
    space_id: str,
    space_label: str = "",
    zone_id: str = "",
    zone_name: str = "",
    visibility: str = "direct",
    entity_id: str = "",
    related_actor_ids: Sequence[str] = (),
) -> bool:
    clean_sum = clip_text(summary, 220, "")
    if not clean_sum:
        return False
    exps = ledger.setdefault("experienced_events", [])
    dedup_key = (int(seq), str(kind), str(visibility))
    for prev in exps[-12:]:
        if (prev.get("seq"), prev.get("kind"), prev.get("visibility")) == dedup_key:
            return False
    exps.append({
        "seq": int(seq),
        "sim_min": round(float(sim_min), 2),
        "kind": str(kind),
        "summary": clean_sum,
        "space_id": str(space_id or ""),
        "space_label": clip_text(space_label, 48, ""),
        "zone_id": str(zone_id or ""),
        "zone_name": clip_text(zone_name, 32, ""),
        "visibility": str(visibility),
        "entity_id": str(entity_id or ""),
        "related_actor_ids": [str(a) for a in related_actor_ids if str(a).strip()],
    })
    if len(exps) > _MAX_EVENTS_PER_LEDGER:
        del exps[:-_MAX_EVENTS_PER_LEDGER]
    return True


def apply_choice_to_ledger(ledger: dict[str, Any], choice_record: Mapping[str, Any]) -> None:
    """将节点抉择写入 L2，并将心念变化增量沉淀进 L3 生命弧光。"""
    records = ledger.setdefault("choice_records", [])
    cid = str(choice_record.get("choice_id") or "")
    nid = str(choice_record.get("node_id") or "")
    tid = str(choice_record.get("thread_id") or "")
    replaced = False
    for idx, row in enumerate(records):
        if (cid and row.get("choice_id") == cid) or (nid and tid and row.get("thread_id") == tid and row.get("node_id") == nid):
            records[idx] = dict(choice_record)
            replaced = True
            break
    if not replaced:
        records.append(dict(choice_record))
        if len(records) > 40:
            del records[:-40]

    arc = ledger.setdefault("arc_state", {
        "aspirations": [], "promises": [], "secrets": [], "stance_toward": {}, "turning_points": [],
    })
    delta = choice_record.get("arc_delta")
    if isinstance(delta, Mapping):
        for field in ("aspirations", "promises", "secrets", "turning_points"):
            bucket = arc.setdefault(field, [])
            for item in delta.get(field) or ():
                txt = clip_text(item, 100, "")
                if txt and txt not in bucket:
                    bucket.append(txt)
            if len(bucket) > _MAX_ARC_ITEMS:
                del bucket[:-_MAX_ARC_ITEMS]
        stance_map = arc.setdefault("stance_toward", {})
        for k, v in (delta.get("stance_toward") or {}).items():
            ck = clip_text(k, 32, "")
            cv = clip_text(v, 80, "")
            if ck and cv:
                stance_map[ck] = cv


def _mark_choice_execution(
    session: dict[str, Any],
    actor_id: str,
    *,
    kind: str,
    target_match: str = "",
    note: str = "",
    sim_min: float = 0.0,
    completed_actors: set[str] | None = None,
) -> bool:
    updated = False
    for thread in session.get("threads") or ():
        if thread.get("status") != "active":
            continue
        for node in thread.get("nodes") or ():
            if node.get("status") != "active":
                continue
            rec = (node.get("participant_choices") or {}).get(actor_id)
            if not isinstance(rec, dict) or rec.get("execution_status") != "pending":
                continue
            intent = rec.get("world_intent") or {}
            ikind = str(intent.get("kind") or "")
            itarget = str(intent.get("target") or "")
            matched = False
            if ikind == "visit" and kind == "space" and target_match == itarget:
                matched = True
            elif ikind == "entity" and kind == "entity" and target_match == itarget:
                matched = True
            elif ikind in ("social", "seek_player") and kind == "social" and target_match == itarget:
                focus = node.get("dialogue_focus") or {}
                matched = set(focus.get("actor_ids") or ()) != {actor_id, target_match}
            if matched:
                rec["execution_status"] = "completed"
                rec["execution_note"] = clip_text(note, 160, "已实际前往并完成行动")
                rec["completed_at_min"] = round(float(sim_min), 2)
                ledger = (session.get("ledgers") or {}).get(actor_id)
                if isinstance(ledger, dict):
                    apply_choice_to_ledger(ledger, rec)
                if completed_actors is not None:
                    completed_actors.add(actor_id)
                updated = True
    return updated


def ingest_world_events(
    session: dict[str, Any],
    events: Sequence[Mapping[str, Any]],
    world_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """非全知解析世界事件流 -> 更新各角色 L1 记事、抉择执行状态与节点触达集合。"""
    ws = world_state if isinstance(world_state, Mapping) else {}
    spaces = {
        str(sp.get("space_id") or ""): sp
        for sp in (ws.get("spaces") or ())
        if isinstance(sp, Mapping) and sp.get("space_id")
    }
    actors_by_id = {
        str(a.get("actor_id") or ""): a
        for a in (ws.get("actors") or ())
        if isinstance(a, Mapping) and a.get("actor_id")
    }
    raw_entities = ws.get("realm_entities") or ws.get("entities") or ()
    entities_by_id = {
        str(e.get("entity_id") or ""): e
        for e in raw_entities
        if isinstance(e, Mapping) and e.get("entity_id")
    }
    ledgers = session.setdefault("ledgers", {})
    sim_min = float(ws.get("minutes") if ws.get("minutes") is not None else session.get("last_sim_min") or 0.0)
    changed = False
    if sim_min > float(session.get("last_sim_min") or 0.0):
        session["last_sim_min"] = round(sim_min, 2)
        changed = True

    active_target_zones: set[tuple[str, str]] = set()
    active_deployed_eids: set[str] = set()
    active_dialogues: dict[str, dict[str, Any]] = {}
    for thread in session.get("threads") or ():
        if thread.get("status") != "active":
            continue
        for node in thread.get("nodes") or ():
            if node.get("status") != "active":
                continue
            if node.get("dialogue_focus"):
                active_dialogues[node["node_id"]] = node["dialogue_focus"]
            if node.get("target_space_id") and node.get("target_zone_id"):
                active_target_zones.add((str(node["target_space_id"]), str(node["target_zone_id"])))
            for eid in node.get("deployed_entity_ids") or ():
                active_deployed_eids.add(str(eid))

    max_seq = int(session.get("event_cursor") or 0)
    actor_cue_hits: dict[str, set[str]] = {}
    actor_entity_hits: dict[str, set[str]] = {}
    actor_dialogue_hits: set[str] = set()
    dialogue_partners: dict[str, set[str]] = {}
    actor_space_hits: dict[str, set[str]] = {}
    discovered_actor_ids: set[str] = set()
    completed_actor_ids: set[str] = set()
    player_events: list[dict[str, Any]] = []

    def _actor_name(aid: str) -> str:
        if aid in actors_by_id:
            return str(actors_by_id[aid].get("name") or aid)
        if aid in ledgers:
            return str(ledgers[aid].get("name") or aid)
        return aid or "某人"

    for ev in events:
        if not isinstance(ev, Mapping):
            continue
        seq = int(ev.get("seq") or 0)
        if seq > max_seq:
            max_seq = seq
        kind = str(ev.get("kind") or "")
        ev_aid = str(ev.get("actor_id") or "")
        ev_sid = str(ev.get("space_id") or "")
        payload = ev.get("payload") if isinstance(ev.get("payload"), Mapping) else {}
        sp = spaces.get(ev_sid)
        sp_lbl = _space_label(sp)

        if kind == "dialogue_reunion_blocked":
            focus = active_dialogues.get(str(payload.get("dialogue_scene_id") or ""))
            if (focus and not focus.get("interruption") and ev_aid == focus["actor_ids"][0]
                    and payload.get("dialogue_revision") == focus["revision"]):
                focus["interruption"] = "当事人当前无法到达对方所在的位置，本幕的谈话暂告中断。"
                changed = True
        elif kind in (
            "entity_inspected",
            "entity_read",
            "entity_inscribed",
            "entity_consumed",
            "inventory_changed",
            "entity_moved",
            "gift_transferred",
        ):
            eid = str(payload.get("entity_id") or "")
            ent = entities_by_id.get(eid) or {}
            ename = str(payload.get("name") or payload.get("item_name") or ent.get("name") or eid or "物件")
            ex = ent.get("x") if "x" in ent else (actors_by_id.get(ev_aid) or {}).get("x", 0)
            ey = ent.get("y") if "y" in ent else (actors_by_id.get(ev_aid) or {}).get("y", 0)
            ez = _zone_at_point(sp, int(ex or 0), int(ey or 0))
            ez_id = str((ez or {}).get("zone_id") or "")
            ez_name = str((ez or {}).get("name") or "")

            verb_txt = {
                "entity_inspected": f"仔细查看了「{ename}」",
                "entity_read": f"阅读了「{ename}」上的文字",
                "entity_inscribed": f"在「{ename}」上写下了留言",
                "entity_consumed": f"使用了「{ename}」",
                "inventory_changed": f"{'拿起了' if str(payload.get('operation') or '') in ('pickup', 'take', 'stow', 'claim') else '挪动了'}「{ename}」",
                "entity_moved": f"移动了「{ename}」",
                "gift_transferred": f"把「{ename}」交给了{_actor_name(str(payload.get('recipient_id') or ''))}",
            }.get(kind, f"触碰了「{ename}」")

            detail_note = ""
            if kind in ("entity_inspected", "entity_read"):
                raw_info = ent.get("detail") or payload.get("detail") or ent.get("brief") or payload.get("brief") or ""
                detail_snip = clip_text(raw_info, 96, "")
                if detail_snip and (eid in active_deployed_eids or ent.get("detail") or ent.get("brief")):
                    detail_note = f"（查见内容：{detail_snip}）"

            if ev_aid in ledgers:
                changed |= _append_experience(
                    ledgers[ev_aid],
                    seq=seq,
                    sim_min=sim_min,
                    kind=kind,
                    summary=f"在{sp_lbl or '此处'}{('·' + ez_name) if ez_name else ''}{verb_txt}{detail_note}。",
                    space_id=ev_sid,
                    space_label=sp_lbl,
                    zone_id=ez_id,
                    zone_name=ez_name,
                    visibility="direct",
                    entity_id=eid,
                    related_actor_ids=([str(payload["recipient_id"])] if payload.get("recipient_id") else ()),
                )
                actor_entity_hits.setdefault(ev_aid, set()).add(eid)
                if eid in active_deployed_eids and kind in ("entity_inspected", "entity_read"):
                    discovered_actor_ids.add(ev_aid)
                changed |= _mark_choice_execution(
                    session, ev_aid, kind="entity", target_match=eid,
                    note=f"{verb_txt}{detail_note}", sim_min=sim_min, completed_actors=completed_actor_ids,
                )
                if ledgers[ev_aid].get("is_player") and (eid in active_deployed_eids or kind == "entity_inscribed"):
                    player_events.append({"kind": kind, "entity_id": eid, "summary": f"玩家{verb_txt}", "space_id": ev_sid})

            if kind == "gift_transferred":
                rid = str(payload.get("recipient_id") or "")
                if rid in ledgers:
                    changed |= _append_experience(
                        ledgers[rid],
                        seq=seq,
                        sim_min=sim_min,
                        kind=kind,
                        summary=f"在{sp_lbl or '此处'}收下了{_actor_name(ev_aid)}递来的「{ename}」。",
                        space_id=ev_sid,
                        space_label=sp_lbl,
                        zone_id=ez_id,
                        zone_name=ez_name,
                        visibility="direct",
                        entity_id=eid,
                        related_actor_ids=(ev_aid,),
                    )

            for other_aid, other in actors_by_id.items():
                if other_aid in (ev_aid, str(payload.get("recipient_id") or "")) or other_aid not in ledgers:
                    continue
                if other.get("space_id") != ev_sid or other.get("actor_status", "active") != "active":
                    continue
                other_zid = str(other.get("zone_id") or "")
                if _same_compartment(sp, ez_id, other_zid):
                    changed |= _append_experience(
                        ledgers[other_aid],
                        seq=seq,
                        sim_min=sim_min,
                        kind=kind,
                        summary=f"亲眼看到{_actor_name(ev_aid)}{verb_txt}{detail_note}。",
                        space_id=ev_sid,
                        space_label=sp_lbl,
                        zone_id=other_zid,
                        zone_name=str(other.get("zone_name") or ""),
                        visibility="witnessed",
                        entity_id=eid,
                        related_actor_ids=(ev_aid,),
                    )
                    actor_entity_hits.setdefault(other_aid, set()).add(eid)
                    if eid in active_deployed_eids and kind in ("entity_inspected", "entity_read"):
                        discovered_actor_ids.add(other_aid)

        elif kind == "entity_state_changed":
            eid = str(payload.get("entity_id") or "")
            ent = entities_by_id.get(eid) or {}
            ename = str(ent.get("name") or payload.get("name") or "门/物件")
            if "is_open" in payload:
                state_txt = f"{'解锁并打开了' if payload.get('unlocked') else ('打开了' if payload['is_open'] else '关上了')}「{ename}」"
            elif "is_on" in payload:
                state_txt = f"{'点亮了' if payload['is_on'] else '熄灭了'}「{ename}」"
            else:
                state_txt = f"改变了「{ename}」的状态"
            ez = _zone_at_point(sp, int(ent.get("x") or 0), int(ent.get("y") or 0)) if ent else None
            ez_id = str((ez or {}).get("zone_id") or "")
            ez_name = str((ez or {}).get("name") or "")
            if ev_aid in ledgers:
                changed |= _append_experience(
                    ledgers[ev_aid],
                    seq=seq,
                    sim_min=sim_min,
                    kind=kind,
                    summary=f"在{sp_lbl or '此处'}{state_txt}。",
                    space_id=ev_sid,
                    space_label=sp_lbl,
                    zone_id=ez_id,
                    zone_name=ez_name,
                    visibility="direct",
                    entity_id=eid,
                )
                actor_entity_hits.setdefault(ev_aid, set()).add(eid)
                changed |= _mark_choice_execution(
                    session, ev_aid, kind="entity", target_match=eid,
                    note=state_txt, sim_min=sim_min, completed_actors=completed_actor_ids,
                )
            for other_aid, other in actors_by_id.items():
                if other_aid == ev_aid or other_aid not in ledgers:
                    continue
                if other.get("space_id") != ev_sid or other.get("actor_status", "active") != "active":
                    continue
                other_zid = str(other.get("zone_id") or "")
                same_room = _same_compartment(sp, ez_id, other_zid)
                vis = "witnessed" if same_room else "overheard"
                txt = (
                    f"看到{_actor_name(ev_aid)}{state_txt}。"
                    if same_room
                    else f"在{sp_lbl}隔壁隐约听见「{ename}」发出的声响。"
                )
                changed |= _append_experience(
                    ledgers[other_aid],
                    seq=seq,
                    sim_min=sim_min,
                    kind=kind,
                    summary=txt,
                    space_id=ev_sid,
                    space_label=sp_lbl,
                    zone_id=other_zid,
                    zone_name=str(other.get("zone_name") or ""),
                    visibility=vis,
                    entity_id=eid,
                    related_actor_ids=([ev_aid] if same_room and ev_aid else ()),
                )

        elif kind == "ambient_cue":
            cue_text = clip_text(payload.get("text"), 110, "周围传来细微异常。")
            cue_zid = str(payload.get("zone_id") or "")
            cue_zname = str(payload.get("zone_name") or "")
            wids = [str(w) for w in (payload.get("witness_actor_ids") or ()) if str(w)]
            aids = [str(a) for a in (payload.get("audible_actor_ids") or ()) if str(a)]
            for wid in wids:
                if wid in ledgers:
                    changed |= _append_experience(
                        ledgers[wid],
                        seq=seq,
                        sim_min=sim_min,
                        kind=kind,
                        summary=f"在{sp_lbl}{('·' + cue_zname) if cue_zname else ''}当场察觉：{cue_text}",
                        space_id=ev_sid,
                        space_label=sp_lbl,
                        zone_id=cue_zid,
                        zone_name=cue_zname,
                        visibility="witnessed",
                    )
                    actor_cue_hits.setdefault(wid, set()).add(ev_sid)
            for aid_over in aids:
                if aid_over in ledgers:
                    changed |= _append_experience(
                        ledgers[aid_over],
                        seq=seq,
                        sim_min=sim_min,
                        kind=kind,
                        summary=f"在{sp_lbl}邻近隔间隐约听到：{cue_text}",
                        space_id=ev_sid,
                        space_label=sp_lbl,
                        visibility="overheard",
                    )
                    actor_cue_hits.setdefault(aid_over, set()).add(ev_sid)

        elif kind == "dialogue_exchange":
            spk = str(payload.get("speaker_id") or ev_aid or "")
            prt = str(payload.get("partner_id") or "")
            focus = active_dialogues.get(str(payload.get("dialogue_scene_id") or ""))
            if (focus and set(focus["actor_ids"]) == {spk, prt}
                    and payload.get("dialogue_revision") == focus["revision"]):
                exchanges = focus["exchanges"]
                transcript = [
                    {"a": turn["a"], "b": turn["b"]}
                    for turn in (payload.get("transcript") or ())[:4]
                    if isinstance(turn, Mapping) and all(
                        isinstance(turn.get(side), str) and turn[side].strip() for side in ("a", "b")
                    )
                ]
                if transcript and len(exchanges) == focus["revision"]:
                    exchanges.append({"speaker_id": spk, "partner_id": prt, "transcript": transcript})
                    changed = True
            spk_name = str(payload.get("speaker_name") or _actor_name(spk))
            prt_name = str(payload.get("partner_name") or _actor_name(prt))
            sum_txt = clip_text(payload.get("summary") or f"{spk_name}与{prt_name}交谈了一番。", 120)
            turn_snippets: list[str] = []
            for turn in (payload.get("transcript") or ())[:2]:
                if not isinstance(turn, Mapping):
                    continue
                la = clip_text(turn.get("a"), 48, "")
                lb = clip_text(turn.get("b"), 48, "")
                if la and lb:
                    turn_snippets.append(f"{spk_name}：「{la}」/{prt_name}：「{lb}」")
                elif la:
                    turn_snippets.append(f"{spk_name}：「{la}」")
                elif lb:
                    turn_snippets.append(f"{prt_name}：「{lb}」")
            transcript_snip = "；".join(turn_snippets)
            full_sum = (
                clip_text(f"{sum_txt}（原话：{transcript_snip}）", 220)
                if transcript_snip
                else sum_txt
            )
            exec_note = (
                clip_text(f"与{{partner}}交谈（原话：{transcript_snip}）", 160)
                if transcript_snip
                else ""
            )
            spk_actor = actors_by_id.get(spk) or {}
            szid = str(spk_actor.get("zone_id") or "")
            for pid, other_pid in ((spk, prt), (prt, spk)):
                if pid and pid in ledgers:
                    changed |= _append_experience(
                        ledgers[pid],
                        seq=seq,
                        sim_min=sim_min,
                        kind=kind,
                        summary=full_sum,
                        space_id=ev_sid,
                        space_label=sp_lbl,
                        zone_id=szid,
                        visibility="direct",
                        related_actor_ids=([other_pid] if other_pid else ()),
                    )
                    actor_dialogue_hits.add(pid)
                    if other_pid:
                        dialogue_partners.setdefault(pid, set()).add(other_pid)
                        other_nm = _actor_name(other_pid)
                        note_for_pid = (
                            exec_note.replace("{partner}", other_nm)
                            if exec_note
                            else f"与{other_nm}当面交谈：{sum_txt}"
                        )
                        changed |= _mark_choice_execution(
                            session, pid, kind="social", target_match=other_pid,
                            note=note_for_pid,
                            sim_min=sim_min, completed_actors=completed_actor_ids,
                        )
            if ledgers.get(spk, {}).get("is_player") or ledgers.get(prt, {}).get("is_player"):
                player_events.append({"kind": kind, "summary": full_sum, "space_id": ev_sid})
            for other_aid, other in actors_by_id.items():
                if other_aid in (spk, prt) or other_aid not in ledgers:
                    continue
                if other.get("space_id") != ev_sid or other.get("actor_status", "active") != "active":
                    continue
                ozid = str(other.get("zone_id") or "")
                same_room = _same_compartment(sp, szid, ozid)
                changed |= _append_experience(
                    ledgers[other_aid],
                    seq=seq,
                    sim_min=sim_min,
                    kind=kind,
                    summary=sum_txt if same_room else f"隔墙听到{spk_name}和{prt_name}低声交谈。",
                    space_id=ev_sid,
                    space_label=sp_lbl,
                    zone_id=ozid,
                    visibility="witnessed" if same_room else "overheard",
                    related_actor_ids=([spk, prt] if same_room else ()),
                )

        elif kind == "actor_moved":
            via_portal = str(payload.get("via_portal") or "")
            to_sid = str(payload.get("to_space_id") or ev_sid or "")
            az = _zone_at_point(sp, int(payload.get("x") or 0), int(payload.get("y") or 0))
            az_id = str((az or {}).get("zone_id") or "")
            az_name = str((az or {}).get("name") or "")
            notable_zone = (ev_sid, az_id) in active_target_zones
            if (via_portal or notable_zone) and ev_aid in ledgers:
                door_name = str(payload.get("door_name") or "")
                msg = (
                    f"经过「{door_name}」来到{_space_label(spaces.get(to_sid)) or to_sid}。"
                    if via_portal and door_name
                    else f"走进了{_space_label(sp) or ev_sid}{('·' + az_name) if az_name else ''}。"
                )
                changed |= _append_experience(
                    ledgers[ev_aid],
                    seq=seq,
                    sim_min=sim_min,
                    kind=kind,
                    summary=msg,
                    space_id=to_sid,
                    space_label=_space_label(spaces.get(to_sid)),
                    zone_id=az_id,
                    zone_name=az_name,
                    visibility="direct",
                )
            if to_sid and ev_aid:
                actor_space_hits.setdefault(ev_aid, set()).add(to_sid)
                changed |= _mark_choice_execution(
                    session, ev_aid, kind="space", target_match=to_sid,
                    note=f"抵达了{_space_label(spaces.get(to_sid)) or to_sid}", sim_min=sim_min,
                    completed_actors=completed_actor_ids,
                )

    for aid, actor in actors_by_id.items():
        if actor.get("space_id"):
            actor_space_hits.setdefault(aid, set()).add(str(actor["space_id"]))

    if max_seq > int(session.get("event_cursor") or 0):
        session["event_cursor"] = max_seq
        changed = True

    # 检查并同步节点级“位置到达类”pending 执行状态（针对已在目标空间中的情况）
    for aid, actor in actors_by_id.items():
        if actor.get("space_id"):
            changed |= _mark_choice_execution(
                session, aid, kind="space", target_match=str(actor["space_id"]),
                note=f"已身处{_space_label(spaces.get(str(actor['space_id']))) or actor['space_id']}", sim_min=sim_min,
                completed_actors=completed_actor_ids,
            )

    # 判定每个活跃丝线当前节点的触达角色
    triggered: list[tuple[str, str, str]] = []
    for thread in session.get("threads") or ():
        if thread.get("status") != "active":
            continue
        tid = str(thread.get("thread_id") or "")
        active_nid = str(thread.get("active_node_id") or "")
        node = next(
            (n for n in (thread.get("nodes") or ()) if n.get("node_id") == active_nid and n.get("status") == "active"),
            None,
        )
        if node is None:
            continue
        p_choices = node.get("participant_choices") or {}
        triggers = node.get("entry_triggers") or ()
        deadline = float(node.get("pressure_deadline_min") or 0.0)
        t_sid = str(node.get("target_space_id") or "")
        t_zid = str(node.get("target_zone_id") or "")
        catalyst_ids = {str(x).strip() for x in (node.get("catalyst_actor_ids") or ()) if str(x).strip()}
        lead_ids = resolve_thread_lead_ids(session, thread, actors_by_id)
        node_created = float(node.get("created_at_min") or 0.0)

        for aid, ledger in ledgers.items():
            if ledger.get("is_player") or aid in p_choices:
                continue
            actor = actors_by_id.get(aid)
            if actor is not None and actor.get("actor_status", "active") != "active":
                continue

            hit = False
            if aid in (node.get("dialogue_focus") or {}).get("actor_ids", ()) or aid in catalyst_ids or aid in actor_dialogue_hits:
                hit = True
            elif aid in lead_ids and sim_min >= node_created:
                hit = True
            elif deadline and sim_min >= deadline:
                hit = True
            if not hit and actor is not None and t_sid and actor.get("space_id") == t_sid:
                if not t_zid or str(actor.get("zone_id") or "") == t_zid:
                    hit = True
            if not hit:
                for trig in triggers:
                    ttype = str(trig.get("type") or "")
                    if ttype == "actor_in_space":
                        want_sid = str(trig.get("space_id") or t_sid)
                        want_zid = str(trig.get("zone_id") or "")
                        if want_sid in actor_space_hits.get(aid, set()):
                            if not want_zid or (actor and str(actor.get("zone_id") or "") == want_zid):
                                hit = True
                    elif ttype == "ambient_cue":
                        want_sid = str(trig.get("space_id") or t_sid)
                        if not want_sid or want_sid in actor_cue_hits.get(aid, set()):
                            if aid in actor_cue_hits:
                                hit = True
                    elif ttype == "entity_event":
                        want_eid = str(trig.get("entity_id") or "")
                        if want_eid and want_eid in actor_entity_hits.get(aid, set()):
                            hit = True
                    elif ttype == "dialogue_with":
                        if aid in actor_dialogue_hits:
                            hit = True
                    elif ttype == "sim_time_reached":
                        at_min = float(trig.get("at_min") or deadline or 0.0)
                        if at_min and sim_min >= at_min:
                            hit = True
                    if hit:
                        break
            if hit:
                triggered.append((tid, active_nid, aid))

    return {
        "changed": changed,
        "triggered": triggered,
        "player_events": player_events,
        "discovered_actor_ids": sorted(discovered_actor_ids),
        "completed_actor_ids": sorted(completed_actor_ids),
        "dialogue_partners": {k: sorted(v) for k, v in dialogue_partners.items()},
    }


def resolve_thread_lead_ids(
    session: Mapping[str, Any],
    thread: Mapping[str, Any],
    actors_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> set[str]:
    """解析推动本条命运丝线的核心主角/主动当事角色（外来导入主角 + 标题/梗概点名角色）。"""
    ledgers = session.get("ledgers") or {}
    actors = actors_by_id or {}
    story_blob = f"{thread.get('title') or ''} {thread.get('premise') or ''} {thread.get('public_incident_fact') or ''}"

    def _active_npc(aid: str) -> bool:
        lg = ledgers.get(aid)
        if not isinstance(lg, Mapping) or lg.get("is_player"):
            return False
        act = actors.get(aid)
        return act is None or act.get("actor_status", "active") == "active"

    leads: set[str] = {
        str(aid).strip()
        for aid in (thread.get("lead_actor_ids") or ())
        if _active_npc(str(aid).strip())
    }
    for aid, lg in ledgers.items():
        if not _active_npc(aid):
            continue
        nm = str(lg.get("name") or "").strip()
        if not lg.get("is_native_npc") or (nm and nm in story_blob):
            leads.add(aid)
    return leads


def summarize_ledger_for_prompt(
    ledger: Mapping[str, Any],
    *,
    max_events: int = 8,
    max_choices: int = 5,
) -> str:
    """为抉择引擎提炼单一角色的非全知主观近况文本。"""
    lines: list[str] = []
    arc = ledger.get("arc_state") if isinstance(ledger.get("arc_state"), Mapping) else {}
    if arc.get("aspirations"):
        lines.append("当前念想：" + "；".join(arc["aspirations"][-3:]))
    if arc.get("promises"):
        lines.append("心头承诺：" + "；".join(arc["promises"][-3:]))
    if arc.get("secrets"):
        lines.append("独自守着的秘密：" + "；".join(arc["secrets"][-2:]))
    choices = list(ledger.get("choice_records") or ())[-max_choices:]
    if choices:
        c_txt = "；".join(
            f"[{c.get('node_title') or '节点'}{'·已完成' if c.get('execution_status') == 'completed' else ''}] {c.get('choice_summary')}"
            for c in choices
        )
        lines.append("此前做过的选择：" + c_txt)
    exps = list(ledger.get("experienced_events") or ())[-max_events:]
    if exps:
        e_txt = "；".join(e.get("summary") or "" for e in exps if e.get("summary"))
        lines.append("亲历近事：" + e_txt)
    if lines:
        return "\n".join(lines)
    if ledger.get("is_native_npc"):
        home_nm = str((ledger.get("story_role") or {}).get("home_space_name") or "本镇")
        return f"长年住在{home_nm}一带，熟悉小镇街坊与往日旧事。"
    return "刚刚暂留这座小镇，对镇上居民与旧事仍在逐步了解之中。"
