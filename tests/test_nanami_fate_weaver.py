"""End-to-end tests for Fate Weaver v3 (命运丝线架构): chronicle, autonomous choice, rolling weaving, and UI RPCs."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from _shinsekai import STORY_CODE, WORLD_CODE

from plugins.nanami_story import story_page
from plugins.nanami_story.adapters.persona_adapter import PersonaAdapter
from plugins.nanami_story.adapters.world_adapter import WorldAdapter
from plugins.nanami_story.beat_weaver import build_fate_overlay
from plugins.nanami_story.choice_engine import decide_actor_choice
from plugins.nanami_story.fate_runtime import FateRuntime
from plugins.nanami_story.fate_schema import redact_fate_session_for_ui
from plugins.nanami_story.plugin import NanamiStoryPlugin
from plugins.nanami_story.settings import SettingsStore, StorySettings
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.external_bridge import (
    WorldBridge,
    register_world_bridge,
    unregister_world_bridge,
)
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.live_simulation import LiveActor
from plugins.nanami_world.outdoor_designer import synthesize_outdoor_blueprint
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService


class _DummyRegister:
    def __init__(self) -> None:
        self.actions: dict[str, object] = {}
        self.pages: list[object] = []
        self.config_pages: list[object] = []
        self.chat_ui: list[object] = []

    def register_frontend_page(self, page) -> None:
        self.pages.append(page)

    def register_frontend_config_page(self, contrib) -> None:
        self.config_pages.append(contrib)
        for act in contrib.actions:
            self.actions[act.id] = act.run

    def register_frontend_chat_ui(self, contrib) -> None:
        self.chat_ui.append(contrib)


class FateWeaverTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

        # Configure two contrasting read-only personas under data/nanami
        self.base_nanami = self.root / "data" / "nanami"
        (self.base_nanami / "profiles" / "nanami_chiaki" / "state").mkdir(parents=True, exist_ok=True)
        (self.base_nanami / "profiles" / "komaeda_bold" / "state").mkdir(parents=True, exist_ok=True)
        (self.base_nanami / "profiles" / "hinata_cautious" / "state").mkdir(parents=True, exist_ok=True)
        (self.base_nanami / "state").mkdir(parents=True, exist_ok=True)

        (self.base_nanami / "pointers.yaml").write_text(
            "active_edit_id: nanami_chiaki\npointers:\n"
            "  - id: nanami_chiaki\n    enabled: true\n    character_name: 七海千秋\n"
            "    profile_dir: profiles/nanami_chiaki\n    state_root: \"\"\n"
            "  - id: komaeda_bold\n    enabled: true\n    character_name: 星野勇\n"
            "    profile_dir: profiles/komaeda_bold\n    state_root: profiles/komaeda_bold\n"
            "  - id: hinata_cautious\n    enabled: true\n    character_name: 雾岛静\n"
            "    profile_dir: profiles/hinata_cautious\n    state_root: profiles/hinata_cautious\n",
            encoding="utf-8",
        )

        # Bold/curious/protector profile (星野勇)
        (self.base_nanami / "profiles" / "komaeda_bold" / "soul.md").write_text(
            "我是星野勇，面对谜团总是按捺不住好奇心，习惯亲自去现场翻看确认。",
            encoding="utf-8",
        )
        (self.base_nanami / "profiles" / "komaeda_bold" / "state" / "traits.json").write_text(
            json.dumps({
                "traits": {
                    "effortfulControl": 0.45,
                    "surgency": 0.88,
                    "needProfile": {"safety": 0.18, "relatedness": 0.42, "competence": 0.92},
                    "social": {"prosocial": 0.50, "normCompliance": 0.45, "affiliation": 0.48},
                    "attachment": {"avoidance": 0.15},
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # Cautious/avoidant profile (雾岛静)
        (self.base_nanami / "profiles" / "hinata_cautious" / "soul.md").write_text(
            "我是雾岛静，做事极其谨慎慢热，遇到突然的变动会先退回安全角落观察。",
            encoding="utf-8",
        )
        (self.base_nanami / "profiles" / "hinata_cautious" / "state" / "traits.json").write_text(
            json.dumps({
                "traits": {
                    "effortfulControl": 0.86,
                    "surgency": 0.14,
                    "needProfile": {"safety": 0.94, "relatedness": 0.28, "competence": 0.35},
                    "social": {"prosocial": 0.40, "normCompliance": 0.85, "affiliation": 0.30},
                    "attachment": {"avoidance": 0.90},
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # Warm/empathetic baseline (七海千秋)
        (self.base_nanami / "profiles" / "nanami_chiaki" / "soul.md").write_text(
            "我是七海千秋，平时有些嗜睡，但很在意大家有没有好好在一起。",
            encoding="utf-8",
        )
        (self.base_nanami / "state" / "traits.json").write_text(
            json.dumps({
                "traits": {
                    "effortfulControl": 0.78,
                    "surgency": 0.36,
                    "needProfile": {"safety": 0.45, "relatedness": 0.90, "competence": 0.70},
                    "social": {"prosocial": 0.92, "normCompliance": 0.72, "affiliation": 0.82},
                    "attachment": {"avoidance": 0.25},
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.base_nanami / "state" / "relations.json").write_text(
            json.dumps({
                "targets": {
                    "char:星野勇": {"known": True, "rel_stage": "friend", "trust": 0.85, "warmth": 0.82, "target_name": "星野勇"},
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        (self.root / "data" / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "config" / "characters.yaml").write_text(
            "- name: 七海千秋\n  desc: 沉着温柔\n"
            "- name: 星野勇\n  desc: 好奇直爽\n"
            "- name: 雾岛静\n  desc: 谨慎安静\n",
            encoding="utf-8",
        )

        self.clock = patch("time.time", return_value=datetime(2026, 8, 1, 16, 0).timestamp())
        self.clock.start()
        self.addCleanup(self.clock.stop)

        self.world_settings = WorldSettings(heartbeat_use_llm=False)
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world_data", project_root=self.root)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.world_settings, project_root=self.root)
        self.addCleanup(self.heartbeat.stop)
        self.chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.world_settings)
        self.heartbeat._chat_service_provider = lambda: self.chat
        self.bridge = WorldBridge(self.engine, world_chat=self.chat, heartbeat=self.heartbeat)

        self.story_settings = StorySettings(enabled=True, use_llm_compiler=False)
        self.world_adapter = WorldAdapter(bridge=self.bridge)
        self.persona_adapter = PersonaAdapter(self.root)
        self.fate_runtime = FateRuntime(
            self.world_adapter,
            self.persona_adapter,
            self.root / "story_data",
            settings_provider=lambda: self.story_settings,
        )
        self.addCleanup(self.fate_runtime.close)

    def _build_living_town(self, name: str = "暮色微风镇") -> dict[str, str]:
        created = self.bridge.dispatch(
            "create_world",
            {
                "mode": "town",
                "name": name,
                "prompt_text": "带有二楼窗景房间和地下储藏室的静谧木屋小镇",
                "building_count": 3,
                "switch_active": True,
                "use_llm": False,
            },
        )
        realm = str(created["space_id"])
        player = self.bridge.dispatch("ensure_player", {"space_id": realm, "name": "旅人"})["actor_id"]
        bold_id = self.bridge.dispatch(
            "upsert_actor",
            {"name": "星野勇", "host_character": "星野勇", "space_id": realm, "ephemeral_realm_id": realm},
        )["actor_id"]
        cautious_id = self.bridge.dispatch(
            "upsert_actor",
            {"name": "雾岛静", "host_character": "雾岛静", "space_id": realm, "ephemeral_realm_id": realm},
        )["actor_id"]
        return {"realm": realm, "player": player, "bold": bold_id, "cautious": cautious_id}

    def test_world_bridge_fate_primitives_and_multi_floor_state(self) -> None:
        town = self._build_living_town()
        realm = town["realm"]

        realms_res = self.world_adapter.list_realms()
        self.assertTrue(realms_res["ok"])
        found_realm = next((r for r in realms_res["realms"] if r["realm_space_id"] == realm), None)
        self.assertIsNotNone(found_realm)
        self.assertGreaterEqual(found_realm["building_count"], 3)

        ws = self.world_adapter.query_world_state(realm)
        self.assertIn("weather", ws)
        self.assertIn("lighting", ws)
        enclosed_space = next(
            (
                sp for sp in ws["spaces"]
                if sp.get("space_kind") != "town" and any(z.get("enclosed") for z in sp.get("zones") or ())
            ),
            None,
        )
        self.assertIsNotNone(enclosed_space, "Generated town must include enclosed sub-rooms")
        target_zone = next(z for z in enclosed_space["zones"] if z.get("enclosed"))
        other_zone = next((z for z in enclosed_space["zones"] if z.get("zone_id") != target_zone["zone_id"]), None)
        self.assertIn("floor_label", enclosed_space)

        # Place entity targeted to target_zone
        synth = self.world_adapter.synthesize_entity(
            space_id=enclosed_space["space_id"],
            zone_id=target_zone["zone_id"],
            custom_name="黄铜怀表",
            prompt_text="一只停在十一点半的黄铜老怀表",
            brief="窗台边放着一只老怀表",
            detail="表盖刻着几道浅浅的刻痕。",
            collectible=True,
            use_llm=False,
        )
        ent = synth["entity"]
        zx, zy, zw, zh = target_zone["x"], target_zone["y"], target_zone["w"], target_zone["h"]
        self.assertTrue(zx <= ent["x"] < zx + zw and zy <= ent["y"] < zy + zh)

        # Ambient cue classifies in-zone witness vs adjacent-zone audible
        with self.engine._lock:
            sp_obj = self.engine._world["spaces"][enclosed_space["space_id"]]
            b_cell = self.engine._find_open_floor_cell_locked(sp_obj, ignore_actor_id=town["bold"])
            in_zone_cell = next(
                (
                    (cx, cy)
                    for cy in range(zy, zy + zh)
                    for cx in range(zx, zx + zw)
                    if self.engine._is_floor_walkable_locked(sp_obj, cx, cy, ignore_actor_id=town["bold"])
                ),
                b_cell,
            )
            self.engine.place_actor(town["bold"], enclosed_space["space_id"], in_zone_cell[0], in_zone_cell[1])
            if other_zone is not None:
                ozx, ozy, ozw, ozh = other_zone["x"], other_zone["y"], other_zone["w"], other_zone["h"]
                out_cell = next(
                    (
                        (cx, cy)
                        for cy in range(ozy, ozy + ozh)
                        for cx in range(ozx, ozx + ozw)
                        if self.engine._is_floor_walkable_locked(sp_obj, cx, cy, ignore_actor_id=town["cautious"])
                    ),
                    None,
                )
                if out_cell is not None:
                    self.engine.place_actor(town["cautious"], enclosed_space["space_id"], out_cell[0], out_cell[1])

        cue = self.world_adapter.emit_ambient_cue(
            enclosed_space["space_id"],
            "怀表盖子轻轻弹开了一道缝。",
            zone_id=target_zone["zone_id"],
        )
        self.assertIn(town["bold"], cue["witness_actor_ids"])
        if other_zone is not None:
            self.assertIn(town["cautious"], cue["audible_actor_ids"])

        # Environment control (weather) + voluntary persona_choice commitment cross-space resolution
        env_res = self.world_adapter.set_world_environment(realm, weather_mode="rain")
        self.assertEqual(env_res["weather"]["current"], "rain")
        commit_res = self.world_adapter.propose_actor_commitment(
            town["bold"],
            {"kind": "visit", "target": realm, "reason": "出去看看雨势"},
            space_id=realm,
            preempt=False,
        )
        self.assertTrue(commit_res["proposed"])
        for _ in range(8):
            self.heartbeat.step(advance_minutes=0.8, use_llm=False)
            if self.engine.live_actor_frame(town["bold"])["space_id"] == realm:
                break
        self.assertEqual(self.engine.live_actor_frame(town["bold"])["space_id"], realm)

    def test_non_omniscient_character_chronicle_with_enclosed_zones(self) -> None:
        town = self._build_living_town()
        realm = town["realm"]
        ws = self.world_adapter.query_world_state(realm)
        enc_space = next(
            sp for sp in ws["spaces"]
            if len([z for z in (sp.get("zones") or ()) if z.get("enclosed")]) >= 2
        )
        enc_zones = [z for z in enc_space["zones"] if z.get("enclosed")]
        z1, z2 = enc_zones[0], enc_zones[1]

        # Move bold + player to z1, cautious behind enclosed wall in z2
        with self.engine._lock:
            sp_obj = self.engine._world["spaces"][enc_space["space_id"]]

            def _cell_in(z, aid):
                for cy in range(z["y"], z["y"] + z["h"]):
                    for cx in range(z["x"], z["x"] + z["w"]):
                        if self.engine._is_floor_walkable_locked(sp_obj, cx, cy, ignore_actor_id=aid):
                            return cx, cy
                return None

            c_bold = _cell_in(z1, town["bold"])
            c_caut = _cell_in(z2, town["cautious"])
            self.assertIsNotNone(c_bold)
            self.assertIsNotNone(c_caut)
            self.engine.place_actor(town["bold"], enc_space["space_id"], c_bold[0], c_bold[1])
            self.engine.place_actor(town["cautious"], enc_space["space_id"], c_caut[0], c_caut[1])

        prop = self.world_adapter.synthesize_entity(
            space_id=enc_space["space_id"],
            zone_id=z1["zone_id"],
            custom_name="旧日记本",
            prompt_text="皮面旧日记本",
            brief="桌边摆着一本旧日记本",
            detail="里页夹着一张车票。",
            collectible=False,
            use_llm=False,
        )
        session = self.fate_runtime.attach_world(realm, auto_pluck=False, use_llm=False)
        sid = session["session_id"]

        # Cautious actor sits in z2; bold actor inspects prop in z1
        self.engine.execute_act(town["bold"], prop["entity_id"], "look")
        self.world_adapter.emit_ambient_cue(enc_space["space_id"], "房间里传出翻动书页的轻响。", zone_id=z1["zone_id"])

        updated = self.fate_runtime.tick_session(sid, use_llm=False)
        bold_exps = updated["ledgers"][town["bold"]]["experienced_events"]
        caut_exps = updated["ledgers"][town["cautious"]]["experienced_events"]

        self.assertTrue(any(e["kind"] == "entity_inspected" and e["visibility"] == "direct" for e in bold_exps))
        # Cautious in enclosed z2 must NOT get entity_inspected from z1, but CAN overhear ambient_cue
        self.assertFalse(any(e["kind"] == "entity_inspected" for e in caut_exps))
        self.assertTrue(any(e["kind"] == "ambient_cue" and e["visibility"] == "overheard" for e in caut_exps))

    def test_choice_prompt_distinguishes_stage_setup_from_live_proximity(self) -> None:
        actors = [
            {"actor_id": "a", "name": "日向创", "space_id": "yard", "x": 4, "y": 4},
            {"actor_id": "b", "name": "哈贝特洛特", "space_id": "yard", "x": 5, "y": 4},
            {"actor_id": "c", "name": "七海千秋", "space_id": "inside", "x": 4, "y": 4},
        ]
        ws = {"actors": actors, "spaces": [{"space_id": "yard", "name": "庭院"}, {"space_id": "inside", "name": "教室"}]}
        session = {"ledgers": {actor["actor_id"]: {"name": actor["name"]} for actor in actors}}
        node = {
            "situation": "七海快走到眼前了。",
            "dialogue_focus": {"actor_ids": ["a", "b"], "topic": "商量接下来怎么开口"},
        }
        prompts = []

        def fake_llm(system, user):
            prompts.append((system, user))
            return {"stance": "confide", "choice_summary": "之后去找七海。",
                    "world_intent": {"kind": "social", "target": "c"}}

        choice = decide_actor_choice(session, {}, node, "a", ws, None, llm_fn=fake_llm)
        system, user = prompts[0]
        self.assertIn("可能描述预定情景", system)
        self.assertIn("哈贝特洛特 [在跟前]", user)
        self.assertIn("七海千秋 [当前不在跟前，可之后去找]", user)
        self.assertIn("对方 ID=b", user)
        self.assertEqual(choice["world_intent"]["target"], "c")

    def test_contrasting_personas_make_divergent_choices_and_land_world_intents(self) -> None:
        town = self._build_living_town()
        realm = town["realm"]
        session = self.fate_runtime.attach_world(
            realm,
            auto_pluck=True,
            theme_compass="mystery_ripple",
            prompt_hint="二楼窗边的深夜木盒",
            use_llm=False,
        )
        sid = session["session_id"]
        # Advance clock past node pressure deadline so both contrasting residents evaluate the node
        self.engine.tick_physics(advance_minutes=4.0)
        updated = self.fate_runtime.tick_session(sid, use_llm=False)

        thread = updated["threads"][0]
        node = thread["nodes"][0]
        choices = node["participant_choices"]
        self.assertIn(town["bold"], choices)
        self.assertIn(town["cautious"], choices)

        bold_choice = choices[town["bold"]]
        caut_choice = choices[town["cautious"]]
        self.assertNotEqual(bold_choice["stance"], caut_choice["stance"])
        self.assertNotEqual(bold_choice["inner_reason"], caut_choice["inner_reason"])
        self.assertIn(bold_choice["stance"], ("investigate", "confront"))
        self.assertIn(caut_choice["stance"], ("withdraw", "conceal"))
        self.assertNotEqual(bold_choice["world_intent"]["kind"], "observe")

        # Verify mock-LLM path executes outside lock without blocking get_session
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow_llm(sys_p, _usr_p):
            if "站在角色的灵魂内部" in sys_p or "命运织机" in sys_p:
                entered.set()
                release.wait(2.0)
            return {
                "stance": "protect",
                "choice_summary": "她决定去照看伙伴。",
                "inner_reason": "就算怕也要确认同伴平安。",
                "world_intent": {"kind": "social", "target": "不存在的目标名", "reason": "找人聊聊"},
                "arc_delta": {"promises": ["陪在大家身边"]},
            }

        self.fate_runtime._llm_fn = slow_llm
        pluck = self.fate_runtime.pluck_thread(sid, theme_compass="crisis_test", use_llm=False)
        self.assertIsNotNone(pluck["thread"])
        # Non-blocking read test while async LLM decision runs
        self.fate_runtime.tick_session(sid, force_evaluate=True, use_llm=True, sync_models=False)
        if entered.wait(1.5):
            # get_session must return immediately while async worker is blocked in slow_llm
            got = self.fate_runtime.get_session(sid)
            self.assertIsNotNone(got)
            release.set()
            self.assertTrue(self.fate_runtime.wait_for_idle(3.0))

    def test_choice_driven_rolling_weaving_branch_divergence_and_persistence(self) -> None:
        # Run 1: investigate branch (use bold resident alone -> investigates)
        created1 = self.bridge.dispatch("create_world", {"mode": "room", "name": "北阁书房", "prompt_text": "安静书房", "use_llm": False})
        r1 = created1["space_id"]
        bold1 = self.bridge.dispatch("upsert_actor", {"name": "星野勇", "host_character": "星野勇", "space_id": r1, "ephemeral_realm_id": r1})["actor_id"]
        s1 = self.fate_runtime.attach_world(r1, auto_pluck=True, theme_compass="mystery_ripple", use_llm=False)
        for _ in range(5):
            cur = self.fate_runtime.tick_session(s1["session_id"], force_evaluate=True, use_llm=False)
            if cur["threads"][0]["status"] == "resolved":
                break
        t1 = cur["threads"][0]

        # Run 2: cautious branch (use cautious resident alone -> withdraws/conceals)
        created2 = self.bridge.dispatch("create_world", {"mode": "room", "name": "北阁书房", "prompt_text": "安静书房", "use_llm": False})
        r2 = created2["space_id"]
        caut2 = self.bridge.dispatch("upsert_actor", {"name": "雾岛静", "host_character": "雾岛静", "space_id": r2, "ephemeral_realm_id": r2})["actor_id"]
        s2 = self.fate_runtime.attach_world(r2, auto_pluck=True, theme_compass="mystery_ripple", use_llm=False)
        for _ in range(5):
            cur2 = self.fate_runtime.tick_session(s2["session_id"], force_evaluate=True, use_llm=False)
            if cur2["threads"][0]["status"] == "resolved":
                break
        t2 = cur2["threads"][0]

        self.assertEqual(t1["status"], "resolved")
        self.assertEqual(t2["status"], "resolved")
        self.assertEqual(t1["phase"], "afterglow")
        self.assertEqual(t2["phase"], "afterglow")
        self.assertNotEqual(t1["nodes"][1]["title"], t2["nodes"][1]["title"])
        self.assertNotEqual(t1["chapter_chronicle"]["epilogue"], t2["chapter_chronicle"]["epilogue"])
        self.assertTrue(cur["ledgers"][bold1]["choice_records"])
        self.assertTrue(cur2["ledgers"][caut2]["choice_records"])

        # Verify restore across runtime restart preserves threads, ledgers, and overlay callback
        restarted = FateRuntime(
            self.world_adapter,
            self.persona_adapter,
            self.root / "story_data",
            settings_provider=lambda: self.story_settings,
        )
        self.addCleanup(restarted.close)
        restored = restarted.restore_sessions()
        restored_s1 = next((x for x in restored if x["session_id"] == s1["session_id"]), None)
        self.assertIsNotNone(restored_s1)
        self.assertEqual(restored_s1["threads"][0]["status"], "resolved")
        self.assertTrue(restored_s1["chronicle_archive"])

    def test_decisions_2a_3a_and_attached_detach_preserves_living_town(self) -> None:
        town = self._build_living_town()
        realm = town["realm"]
        session = self.fate_runtime.attach_world(
            realm,
            auto_pluck=True,
            theme_compass="daily_aspiration",
            reveal_inner_thoughts=False,
            use_llm=False,
        )
        sid = session["session_id"]

        # Trigger AI choice on active node
        self.engine.tick_physics(advance_minutes=4.0)
        active = self.fate_runtime.tick_session(sid, use_llm=False)
        # Decision 3.A: while active and reveal=False, inner_reason is redacted in UI projection
        ui_hidden = redact_fate_session_for_ui(active)
        active_node_ui = ui_hidden["threads"][0]["nodes"][0]
        self.assertTrue(active_node_ui["participant_choices"])
        for rec in active_node_ui["participant_choices"].values():
            self.assertEqual(rec["inner_reason"], "")
            self.assertTrue(rec["inner_hidden"])

        # Toggle reveal_inner_thoughts -> UI reveals authentic inner_reason
        revealed = self.fate_runtime.set_reveal_inner_thoughts(sid, True)
        ui_shown = redact_fate_session_for_ui(revealed)
        for rec in ui_shown["threads"][0]["nodes"][0]["participant_choices"].values():
            self.assertTrue(rec["inner_reason"])
            self.assertFalse(rec["inner_hidden"])

        # Hide again before verifying auto-reveal on thread resolution
        self.fate_runtime.set_reveal_inner_thoughts(sid, False)

        # Decision 2.A: natural player inspection enters L1 chronicle without modal, optional stance works via overlay
        prop_eid = active["threads"][0]["nodes"][0]["deployed_entity_ids"][0]
        prop_ent = self.engine._world["entities"][prop_eid]
        with self.engine._lock:
            px, py = self.engine._find_open_floor_cell_locked(
                self.engine._world["spaces"][prop_ent["space_id"]],
                preferred_x=prop_ent["x"],
                preferred_y=prop_ent["y"],
                ignore_actor_id=town["player"],
            )
            self.engine.place_actor(town["player"], prop_ent["space_id"], px, py)
        self.engine.execute_act(town["player"], prop_eid, "look")
        after_look = self.fate_runtime.tick_session(sid, use_llm=False)
        self.assertTrue(any(e["entity_id"] == prop_eid for e in after_look["ledgers"][town["player"]]["experienced_events"]))

        ov_res = self.bridge.handle_overlay_action(
            "record_player_stance",
            space_id=realm,
            payload={"selected_ids": ["pursue_dream"]},
        )
        self.assertTrue(ov_res["ok"])
        after_stance = self.fate_runtime.get_session(sid)
        self.assertEqual(after_stance["threads"][0]["nodes"][0]["player_stance"]["stance"], "pursue_dream")

        # Resolve thread -> inner_reason automatically unlocks even with reveal_inner_thoughts=False
        for _ in range(6):
            cur = self.fate_runtime.tick_session(sid, force_evaluate=True, use_llm=False)
            if cur["threads"][0]["status"] == "resolved":
                break
        self.assertEqual(cur["threads"][0]["status"], "resolved")
        ui_resolved = redact_fate_session_for_ui(cur)
        for rec in ui_resolved["threads"][0]["nodes"][0]["participant_choices"].values():
            self.assertTrue(rec["inner_reason"])
            self.assertFalse(rec["inner_hidden"])

        # Decision 1.A invariant: detaching an attached town NEVER deletes the town or its residents!
        detached = self.fate_runtime.detach_session(sid, delete_world=True)
        self.assertTrue(detached["preserved_world"])
        ws_after = self.world_adapter.query_world_state(realm)
        self.assertEqual(ws_after["realm_space_id"], realm)
        self.assertGreaterEqual(len(ws_after["actors"]), 3)

    def test_fate_page_rpcs_standalone_creation_and_zero_native_dialogs(self) -> None:
        register_world_bridge(self.bridge)
        self.addCleanup(unregister_world_bridge)

        town = self._build_living_town()
        realm = town["realm"]

        reg = _DummyRegister()
        store = SettingsStore(self.root / "story_plugin_settings.json")
        story_page.register_pages(
            reg,
            code_dir=STORY_CODE,
            fate_runtime=self.fate_runtime,
            world_adapter=self.world_adapter,
            persona_adapter=self.persona_adapter,
            settings_store=store,
        )

        lobby = reg.actions["get_lobby_state"]({})
        self.assertTrue(lobby["ok"])
        self.assertTrue(any(r["realm_space_id"] == realm for r in lobby["realms"]))

        attached = reg.actions["attach_fate_world"]({"realm_space_id": realm, "theme_compass": "bond_turning"})
        self.assertTrue(attached["ok"])
        fsid = attached["session"]["session_id"]

        ticked = reg.actions["tick_fate_session"]({"session_id": fsid, "force_evaluate": True})
        self.assertTrue(ticked["ok"])
        self.assertTrue(ticked["fate_sessions"])

        toggled = reg.actions["toggle_fate_thoughts"]({"session_id": fsid, "reveal": True})
        self.assertTrue(toggled["session"]["reveal_inner_thoughts"])

        stanced = reg.actions["record_fate_player_stance"]({"session_id": fsid, "stance": "confide", "note": "想聊聊"})
        self.assertTrue(stanced["ok"])

        detached = reg.actions["detach_fate_session"]({"session_id": fsid, "delete_world": False})
        self.assertTrue(detached["ok"])
        self.assertTrue(detached["preserved_world"])
        self.assertEqual(detached["fate_sessions"], [])

        # Standalone story creation puts opening prop strictly inside an interior floor/zone (never town center)
        created = reg.actions["create_fate_story"]({
            "title": "雪夜阁楼的旧钢琴",
            "theme_compass": "daily_aspiration",
            "world_mode": "town",
            "experience_mode": "co_play",
            "imported_characters": ["七海千秋"],
            "native_npc_count": 2,
            "building_count": 4,
            "use_llm": False,
        })
        self.assertTrue(created["ok"])
        ss = created["session"]
        self.assertEqual(ss["mode"], "standalone")
        s_realm = ss["realm_space_id"]
        ws = self.world_adapter.query_world_state(s_realm)
        node1 = ss["threads"][0]["nodes"][0]
        self.assertNotEqual(node1["target_space_id"], s_realm)
        prop_ids = set(node1["deployed_entity_ids"])
        self.assertTrue(prop_ids)
        self.assertFalse(any(e["space_id"] == s_realm and e["entity_id"] in prop_ids for e in ws["realm_entities"]))

        deleted = reg.actions["detach_fate_session"]({"session_id": ss["session_id"], "delete_world": True})
        self.assertTrue(deleted["ok"])
        self.assertFalse(deleted["preserved_world"])
        self.assertNotIn(s_realm, self.engine._world["spaces"])

        # Zero native browser dialogs in story.html
        html_src = (STORY_CODE / "web" / "story.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b(?:window\.)?(?:alert|confirm|prompt)\s*\(", html_src))

        # Plugin lifecycle initializes fate_runtime cleanly
        plugin = NanamiStoryPlugin()
        p_reg = _DummyRegister()
        fake_host = type("Host", (), {"project_data_dir": str(self.root / "host_p")})()
        plugin.initialize(p_reg, STORY_CODE, fake_host)
        self.addCleanup(plugin.shutdown)
        self.assertIsNotNone(plugin.fate_runtime)

        # Verify standalone relation boundary between imported companions vs native locals
        stage = self.fate_runtime.start_standalone_stage({
            "title": "雾夜阁楼谜案",
            "world_mode": "town",
            "experience_mode": "observer",
            "imported_characters": ["星野勇", "雾岛静"],
            "native_npc_count": 2,
            "building_count": 4,
            "use_llm": False,
        })
        st_realm = stage["realm_space_id"]
        self.addCleanup(self.fate_runtime.detach_session, stage["session_id"], delete_world=True)
        from plugins.nanami_world import persona_bridge
        nat_name = next(lg["name"] for lg in stage["ledgers"].values() if lg.get("is_native_npc"))
        rels_imp = persona_bridge.read_actor_relations(
            self.root, "星野勇", space_id=st_realm, world_data_root=self.engine.data_root,
            known_actors=["雾岛静", nat_name], isolated_persona=True,
        )
        self.assertTrue(rels_imp["char:雾岛静"]["known"])
        self.assertFalse(rels_imp[f"char:{nat_name}"]["known"])

    def test_observer_view_only_portal_safety_and_selective_night_active(self) -> None:
        co_stage = self.fate_runtime.start_standalone_stage({
            "title": "街角小聚",
            "world_mode": "town",
            "experience_mode": "co_play",
            "imported_characters": ["日向创"],
            "native_npc_count": 1,
            "building_count": 3,
            "use_llm": False,
        })
        self.addCleanup(self.fate_runtime.detach_session, co_stage["session_id"], delete_world=True)
        self.assertFalse(build_fate_overlay(co_stage)["view_only"])

        obs_stage = self.fate_runtime.start_standalone_stage({
            "title": "暴雨夜的钟楼疑案",
            "outline": "雨夜钟楼发生神秘案件，住户们神色各异",
            "world_mode": "town",
            "experience_mode": "observer",
            "imported_characters": ["七海千秋"],
            "native_npc_count": 2,
            "building_count": 3,
            "use_llm": False,
        })
        obs_sid = obs_stage["session_id"]
        obs_realm = obs_stage["realm_space_id"]
        self.addCleanup(self.fate_runtime.detach_session, obs_sid, delete_world=True)
        self.assertTrue(build_fate_overlay(obs_stage)["view_only"])

        # Place an NPC inside a building room and verify observer room exit NEVER teleports them out
        ws = self.world_adapter.query_world_state(obs_realm)
        npc_id = next(aid for aid, lg in obs_stage["ledgers"].items() if not lg.get("is_player"))
        indoor_sid = next(sp["space_id"] for sp in ws["spaces"] if sp["space_id"] != obs_realm)
        with self.engine._lock:
            fx, fy = self.engine._find_open_floor_cell_locked(self.engine._world["spaces"][indoor_sid], ignore_actor_id=npc_id)
        self.engine.place_actor(npc_id, indoor_sid, fx, fy)
        exit_door = next(
            e for e in self.engine.snapshot_for_ui(indoor_sid)["entities"]
            if (e.get("traits") or {}).get("portal", {}).get("target_space_id") == obs_realm
        )
        res_obs = self.engine.enter_portal(exit_door["entity_id"], observe_only=True)
        self.assertEqual(res_obs["mode"], "observer")
        self.assertEqual(self.engine.live_actor_frame(npc_id)["space_id"], indoor_sid)
        res_guard = self.engine.enter_portal(exit_door["entity_id"], actor_id=npc_id, observe_only=False)
        self.assertEqual(res_guard["mode"], "observer")
        self.assertEqual(self.engine.live_actor_frame(npc_id)["space_id"], indoor_sid)

        # Before pending urgent commitments exist, actors should NOT stay night_active all night
        self.assertFalse(self.engine.cognitive_scene(npc_id)["nocturnal_active"])

        # Advance time so actors make choices without resolving the node; verify selective night_active & completion reset
        self.engine.tick_physics(advance_minutes=4.0)
        cur = self.fate_runtime.tick_session(obs_sid, use_llm=False)
        thread_now = cur["threads"][0]
        active_nid = thread_now["active_node_id"]
        node = next(n for n in thread_now["nodes"] if n["node_id"] == active_nid)
        self.assertEqual(node["status"], "active")
        p_choices = node["participant_choices"]
        deployed = list(node.get("deployed_entity_ids") or ())
        passive_aid = next(
            (aid for aid, rec in p_choices.items() if rec.get("stance") in ("conceal", "withdraw") or (rec.get("world_intent") or {}).get("kind") == "observe"),
            None,
        )
        if passive_aid is not None:
            self.assertFalse(self.engine.cognitive_scene(passive_aid)["nocturnal_active"])

        if deployed:
            ent_aid = next(iter(p_choices)) if p_choices else npc_id
            target_eid = next(
                (eid for eid in deployed if "inscribable" in (self.engine._world["entities"][eid].get("traits") or {})),
                deployed[0],
            )
            tgt_ent = self.engine._world["entities"][target_eid]
            act_verb = "read" if "inscribable" in (tgt_ent.get("traits") or {}) else "look"
            with self.fate_runtime._lock:
                live_s = self.fate_runtime._sessions[obs_sid]
                t_live = live_s["threads"][0]
                n_live = next(n for n in t_live["nodes"] if n["node_id"] == t_live["active_node_id"])
                base_choice = dict(n_live["participant_choices"].get(ent_aid) or {
                    "choice_id": "test_c1",
                    "actor_id": ent_aid,
                    "actor_name": live_s["ledgers"][ent_aid]["name"],
                    "thread_id": t_live["thread_id"],
                    "node_id": n_live["node_id"],
                    "node_title": n_live.get("title") or "",
                })
                n_live["participant_choices"][ent_aid] = {
                    **base_choice,
                    "stance": "investigate",
                    "execution_status": "pending",
                    "world_intent": {"kind": "entity", "target": target_eid, "action": act_verb, "reason": "核实线索", "note_text": ""},
                }
                self.fate_runtime._refresh_actor_brief_locked(live_s, t_live, n_live, ent_aid)
            self.engine.set_realm_initial_hour(obs_realm, 12.0)
            self.assertFalse(self.engine.cognitive_scene(ent_aid)["nocturnal_active"])
            self.engine.set_realm_initial_hour(obs_realm, 23.0)
            self.assertTrue(self.engine.cognitive_scene(ent_aid)["nocturnal_active"])
            # Move actor right next to target entity and inspect/read it -> completion flips nocturnal_active back to False
            tgt_sid = tgt_ent["space_id"]
            with self.engine._lock:
                sp_obj = self.engine._world["spaces"][tgt_sid]
                adj = [
                    (tgt_ent["x"] + dx, tgt_ent["y"] + dy)
                    for dx in (-1, 0, 1)
                    for dy in (-1, 0, 1)
                    if (dx or dy) and 0 <= tgt_ent["x"] + dx < sp_obj["width"] and 0 <= tgt_ent["y"] + dy < sp_obj["height"]
                    and self.engine._is_floor_walkable_locked(sp_obj, tgt_ent["x"] + dx, tgt_ent["y"] + dy, ignore_actor_id=ent_aid)
                ]
                tx, ty = adj[0] if adj else (tgt_ent["x"], tgt_ent["y"])
            self.engine.place_actor(ent_aid, tgt_sid, tx, ty)
            self.engine.execute_act(ent_aid, target_eid, act_verb)
            done_s = self.fate_runtime.tick_session(obs_sid, use_llm=False)
            done_node = next(n for n in done_s["threads"][0]["nodes"] if n["node_id"] == active_nid)
            self.assertEqual(done_node["participant_choices"][ent_aid]["execution_status"], "completed")
            self.assertFalse(self.engine.cognitive_scene(ent_aid)["nocturnal_active"])

    def test_story_inherits_text_world_skins_and_initial_relations(self) -> None:
        # Prepare a Text World cast skin & custom_frames for 七海千秋
        nanami_sandbox = self.engine.import_actor(name="七海千秋", host_character="七海千秋", space_id="room_default", x=4, y=4)
        custom_down = [
            "................" if y < 2 or y >= 22 else ("....aaaaaaaa...." if y < 10 else "....bbbbbbbb....")
            for y in range(24)
        ]
        self.engine.set_actor_appearance(
            nanami_sandbox["actor_id"],
            appearance={
                **(nanami_sandbox.get("appearance") or {}),
                "hair_style": "bob",
                "hair_color": "#eab4c6",
                "custom_frames": {"w": 16, "h": 24, "palette": {"a": "#eab4c6", "b": "#6c8c78"}, "down": custom_down},
            },
            map_style="portrait",
        )

        # Configure relations in Text World:
        # 1) __default__: 七海千秋 <-> 雾岛静 is hostile/guarded with custom call_name & memory
        self.bridge.dispatch("set_actor_relation", {
            "space_id": "__default__",
            "subject_character": "七海千秋",
            "target_character": "雾岛静",
            "known": True,
            "rel_stage": "hostile",
            "attitude_stance": "guarded",
            "call_name": "雾岛小姐",
            "relation_tag": "旧日心结",
            "affective_valence": -0.72,
            "trust": 0.15,
            "warmth": 0.18,
            "shared_memories": "曾在旧日赛事产生过严重分歧。",
        })
        # 2) __default__: explicit stranger between 七海千秋 and 星野勇 (must NOT be overwritten by 同行旅伴)
        self.bridge.dispatch("set_actor_relation", {
            "space_id": "__default__",
            "subject_character": "七海千秋",
            "target_character": "星野勇",
            "known": False,
            "rel_stage": "stranger",
            "attitude_stance": "shy",
        })
        # 3) __default__: 七海千秋 -> user relation
        self.bridge.dispatch("set_actor_relation", {
            "space_id": "__default__",
            "subject_character": "七海千秋",
            "target_character": "user",
            "known": True,
            "rel_stage": "close",
            "attitude_stance": "warm",
            "call_name": "玩家君",
            "relation_tag": "最信任的伙伴",
            "affective_valence": 0.90,
            "trust": 0.92,
            "shared_memories": "一起在小镇通关过许多夜晚。",
        })

        captured_prompts: list[tuple[str, str]] = []

        def _fake_llm(sys_p: str, usr_p: str) -> dict:
            captured_prompts.append((sys_p, usr_p))
            if "命运织机" in sys_p:
                return {
                    "title": "雾夜旧约",
                    "premise": "众人齐聚在雨夜旅馆中。",
                    "public_incident_fact": "客厅桌上的旧钟停在十一点半。",
                    "node_title": "初入厅堂",
                    "situation": "雨声敲窗，大家正面对桌上的旧钟。",
                    "dilemma_frame": "谁先上前查看？",
                    "world_setup": {
                        "spawn_props": [{
                            "name": "停摆座钟",
                            "visual_prompt": "一只停在十一点半的木制座钟",
                            "brief": "停在十一点半的座钟",
                            "detail": "表盘后面夹着半张旧字条。",
                            "collectible": False,
                        }],
                    },
                }
            return {
                "stance": "investigate",
                "choice_summary": "决定先上前查看停摆座钟。",
                "inner_reason": "想确认这只钟到底和谁有关。",
                "world_intent": {"kind": "observe", "target": "", "action": "", "reason": "原地观察"},
                "speech_seed": "先看看那只钟吧。",
            }

        self.fate_runtime._llm_fn = _fake_llm
        sess = self.fate_runtime.start_standalone_stage({
            "title": "雨夜关系测试",
            "world_mode": "room",
            "experience_mode": "co_play",
            "imported_characters": ["七海千秋", "星野勇", "雾岛静"],
            "native_npc_count": 0,
            "relation_source": "auto",
            "use_llm": True,
        })
        realm_id = sess["realm_space_id"]

        # Verify skins inherited onto the ephemeral story actors
        story_nanami = next(
            a for a in self.engine.list_actors()
            if a.get("space_id") == realm_id or self.engine.realm_space_id(a["space_id"]) == realm_id
            if not a.get("is_player") and (a.get("host_character") == "七海千秋" or a.get("name") == "七海千秋")
        )
        self.assertEqual(story_nanami["map_style"], "portrait")
        self.assertEqual(story_nanami["appearance"]["hair_style"], "bob")
        self.assertEqual(story_nanami["appearance"]["hair_color"], "#eab4c6")
        self.assertEqual(story_nanami["appearance"]["custom_frames"]["down"], custom_down)

        # Live skin update in Character Manager syncs immediately to running story actor
        self.engine.set_actor_appearance(
            nanami_sandbox["actor_id"],
            appearance={**nanami_sandbox["appearance"], "hair_color": "#ff9dbb"},
            map_style="doll",
        )
        refreshed_story_nanami = next(a for a in self.engine.list_actors() if a["actor_id"] == story_nanami["actor_id"])
        self.assertEqual(refreshed_story_nanami["appearance"]["hair_color"], "#ff9dbb")
        self.assertEqual(refreshed_story_nanami["map_style"], "doll")

        # Verify Story Realm's world relations preserved Text World pointers instead of 同行旅伴
        rels = self.world_adapter.get_actor_relations(realm_id, "七海千秋", known_actors=["星野勇", "雾岛静"])
        self.assertEqual(rels["char:雾岛静"]["rel_stage"], "hostile")
        self.assertEqual(rels["char:雾岛静"]["call_name"], "雾岛小姐")
        self.assertFalse(rels["char:星野勇"]["known"])
        self.assertEqual(rels["char:星野勇"]["rel_stage"], "stranger")
        self.assertEqual(rels["user"]["call_name"], "玩家君")
        self.assertEqual(rels["user"]["rel_stage"], "close")

        # Verify weave_opening_thread prompt included the inherited relationship pointers
        self.assertTrue(any("旧日心结" in usr and "雾岛小姐" in usr for _sys, usr in captured_prompts))

        # Trigger choice engine and verify its prompt receives the world's inherited pointers
        before_cnt = len(captured_prompts)
        self.fate_runtime.tick_session(sess["session_id"], force_evaluate=True, use_llm=True, sync_models=True)
        choice_prompts = [usr for _sys, usr in captured_prompts[before_cnt:]]
        self.assertTrue(any("玩家君" in p and "雾岛小姐" in p and "尚不认识" in p for p in choice_prompts))

        # Verify isolating mutations inside story realm never pollutes __default__ or room_default
        self.world_adapter.set_actor_relation(realm_id, "七海千秋", "雾岛静", {"call_name": "临时改名", "rel_stage": "friend"})
        def_after = self.world_adapter.get_actor_relations("__default__", "七海千秋", known_actors=["雾岛静"])
        self.assertEqual(def_after["char:雾岛静"]["call_name"], "雾岛小姐")
        self.assertEqual(def_after["char:雾岛静"]["rel_stage"], "hostile")

        # Verify relation_source='story_fresh' restores default '同行旅伴'
        sess_fresh = self.fate_runtime.start_standalone_stage({
            "title": "全新关系模式测试",
            "world_mode": "room",
            "experience_mode": "observer",
            "imported_characters": ["七海千秋", "雾岛静"],
            "native_npc_count": 0,
            "relation_source": "story_fresh",
            "use_llm": False,
        })
        fresh_rels = self.world_adapter.get_actor_relations(sess_fresh["realm_space_id"], "七海千秋", known_actors=["雾岛静"])
        self.assertEqual(fresh_rels["char:雾岛静"]["relation_tag"], "同行旅伴")

    @staticmethod
    def _extract_peer_id(usr_p: str, name: str) -> str:
        peer_line = next((ln for ln in usr_p.splitlines() if ln.strip().startswith("- 人物：")), "")
        m = re.search(rf"([A-Za-z0-9_:-]+)\s*->\s*{re.escape(name)}(?:[（(\s\[;；]|$)", peer_line)
        return m.group(1) if m else ""

    def test_protagonist_decision_immediately_advances_continuous_fate(self) -> None:
        llm_calls: list[str] = []

        def fake_story_llm(sys_p: str, usr_p: str) -> dict:
            llm_calls.append(usr_p)
            if "命运织机" in sys_p and "连续命运编排器" not in sys_p:
                return {
                    "title": "希望峰学院最终表白",
                    "premise": "日向创鼓起勇气想在哈贝特洛特的帮助下向七海千秋表白。",
                    "public_incident_fact": "放学后的走廊里，日向创决定找哈贝特洛特商量向七海千秋表白的时机。",
                    "node_title": "求助哈贝特洛特",
                    "situation": "日向创站在室内，准备决定第一步去找谁商量。",
                    "dilemma_frame": "立刻去找哈贝特洛特打气，还是自己独自准备？",
                    "world_setup": {"spawn_props": []},
                }
            if "连续命运编排器" in sys_p:
                seq_m = re.search(r"第(\d+)幕", usr_p)
                cur_seq = int(seq_m.group(1)) if seq_m else 1
                if cur_seq >= 5:
                    return {
                        "action": "resolve",
                        "outcome_summary": "日向创在表白后收获了真诚的回应。",
                        "chapter_chronicle": {
                            "title": "《希望峰学院最终表白》· 终章",
                            "epilogue": "经历一路犹豫与鼓舞，心意终于传到了七海千秋手中。",
                            "key_turns": ["找哈贝特洛特商量", "鼓起勇气向七海千秋表白"],
                        },
                    }
                return {
                    "action": "next_node",
                    "outcome_summary": f"第{cur_seq}幕主人公的决定立刻推动了后续发展。",
                    "phase": "crossroads" if cur_seq < 4 else "climax",
                    "tension": min(0.95, 0.35 + 0.12 * cur_seq),
                    "node": {
                        "title": f"连续推进第{cur_seq + 1}幕",
                        "phase": "crossroads" if cur_seq < 4 else "climax",
                        "situation": "紧接刚才的对话与决定，日向创必须立刻决定下一步行动。",
                        "dilemma_frame": "现在是否直接走到七海千秋面前说出那句话？",
                        "world_setup": {"spawn_props": []},
                    },
                }
            # Actor choice engine
            if "【我是谁】日向创" in usr_p:
                hab_id = self._extract_peer_id(usr_p, "哈贝特洛特")
                nan_id = self._extract_peer_id(usr_p, "七海千秋")
                tgt = nan_id if "求助哈贝特洛特" in usr_p and "已完成" in usr_p and nan_id else (hab_id or nan_id)
                return {
                    "stance": "confide",
                    "choice_summary": "决定立刻找同伴说清心意。",
                    "inner_reason": "不能再等了，先把这一步走出去。",
                    "world_intent": {"kind": "social", "target": tgt, "action": "", "reason": "找同伴谈心"},
                    "speech_seed": "我有件很重要的事想请你帮忙出个主意。",
                }
            return {
                "stance": "observe",
                "choice_summary": "在旁留意气氛变化。",
                "inner_reason": "看看大家接下来的打算。",
                "world_intent": {"kind": "observe", "target": "", "action": "", "reason": "在旁留意"},
            }

        self.fate_runtime._llm_fn = fake_story_llm
        orig_cw = self.world_adapter.create_world
        with patch.object(
            self.world_adapter,
            "create_world",
            side_effect=lambda *a, **kw: orig_cw(*a, **{**kw, "use_llm": False}),
        ):
            sess = self.fate_runtime.start_standalone_stage({
                "title": "希望峰学院最终表白",
                "outline": "日向创在哈贝特洛特的帮助下去向七海千秋表白",
                "world_mode": "room",
                "experience_mode": "observer",
                "imported_characters": ["日向创", "哈贝特洛特", "七海千秋"],
                "native_npc_count": 1,
                "max_nodes": 0,
                "use_llm": True,
            })
        sid = sess["session_id"]
        realm = sess["realm_space_id"]
        self.addCleanup(self.fate_runtime.detach_session, sid, delete_world=True)

        thread0 = sess["threads"][0]
        self.assertEqual(thread0["max_nodes"], 0)
        hinata_id = next(aid for aid, lg in sess["ledgers"].items() if lg.get("name") == "日向创")
        hab_id = next(aid for aid, lg in sess["ledgers"].items() if lg.get("name") == "哈贝特洛特")
        nat_id = next(aid for aid, lg in sess["ledgers"].items() if lg.get("is_native_npc"))
        self.assertIn(hinata_id, thread0["lead_actor_ids"])
        self.assertIn(hab_id, thread0["lead_actor_ids"])

        # 1. Immediate lead ignition at ZERO extra sim-time; bystander native NPC does NOT stall or trigger early
        t1 = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        n1 = t1["threads"][0]["nodes"][0]
        self.assertIn(hinata_id, n1["participant_choices"])
        self.assertNotIn(nat_id, n1["participant_choices"])
        self.assertEqual(n1["participant_choices"][hinata_id]["world_intent"]["target"], hab_id)
        self.assertEqual(n1["participant_choices"][hinata_id]["execution_status"], "pending")

        # 2. 日向创 finishes talking with 哈贝特洛特 -> fate advances IMMEDIATELY without waiting for bystander NPC or +6/+28 min
        with self.engine._lock:
            self.engine._append_event_locked(
                "dialogue_exchange",
                actor_id=hinata_id,
                space_id=realm,
                payload={
                    "speaker_id": hinata_id,
                    "partner_id": hab_id,
                    "speaker_name": "日向创",
                    "partner_name": "哈贝特洛特",
                    "summary": "日向创向哈贝特洛特请教怎样向七海千秋表白。",
                },
            )
        t2 = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        self.assertEqual(len(t2["threads"][0]["nodes"]), 2)
        n2 = t2["threads"][0]["nodes"][1]
        self.assertEqual(n2["status"], "active")
        self.assertIn(hinata_id, n2["catalyst_actor_ids"])

        # 3. On the very next tick (no sim-time stall), 日向创 immediately makes his next decision in node 2
        t3 = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        self.assertIn(hinata_id, t3["threads"][0]["nodes"][1]["participant_choices"])

        # 4. Continuous mode (`max_nodes=0`) progresses organically past 4 beats until LLM resolves at beat 5
        cur = t3
        for _ in range(10):
            cur = self.fate_runtime.tick_session(sid, force_evaluate=True, use_llm=True, sync_models=True)
            if cur["threads"][0]["status"] == "resolved":
                break
        final_t = cur["threads"][0]
        self.assertEqual(final_t["status"], "resolved")
        self.assertEqual(len(final_t["nodes"]), 5)
        self.assertEqual(final_t["nodes"][0]["participant_choices"][hinata_id]["execution_status"], "completed")

    def test_focused_dialogue_skips_joint_visit_and_accepts_one_grounded_reply(self) -> None:
        realm = self.engine.create_space(name="楼梯走廊", width=17, height=10, wall_rows=0)["space_id"]
        lead, partner, helper = (
            self.engine.import_actor(name=name, space_id=realm, x=x, y=y)["actor_id"]
            for name, x, y in (("日向创", 3, 4), ("七海千秋", 4, 4), ("哈贝特洛特", 3, 5))
        )
        bench_blueprint = self.engine.save_blueprint(synthesize_outdoor_blueprint("park_bench"))
        bench = self.engine.place_from_blueprint(bench_blueprint["blueprint_id"], realm, 10, 4)
        self.world_settings = WorldSettings(heartbeat_use_llm=True)
        live = self.heartbeat.live
        live.social_service = lambda: self.chat
        for aid in (lead, partner, helper):
            live._actors[aid] = LiveActor("test", self.engine.live_actor_frame(aid), float("inf"), 0, speed=4)

        confession = "七海，我喜欢你。"
        rejection = "谢谢你愿意告诉我。我很珍惜你，不过我想和你做朋友。"
        judgments = []

        def story_model(system, user):
            if "连续命运编排器" in system:
                judgments.append(user)
                return {
                    "action": "resolve",
                    "outcome_summary": "日向表明心意，七海希望两人仍是朋友。",
                    "chapter_chronicle": {"epilogue": "两人说清了自己的想法。"},
                    "dialogue_result": {"outcome": "answered", "line_ids": [99, " 6 ", 6, True]},
                }
            if "命运织机" in system:
                return {
                    "title": "木椅前的表白", "premise": "日向想向七海表明心意。",
                    "situation": "二人在楼梯口站着，木椅在走廊另一头。",
                    "target_space_id": realm, "dialogue_focus": {
                        "actor_ids": [lead, partner], "topic": "日向表达心意，七海给出真实回应",
                    },
                }
            target = partner if "【我是谁】日向创" in user else lead
            return {
                "stance": "confide", "choice_summary": "走到木椅旁谈一谈",
                "world_intent": {"kind": "social", "target": target},
                "speech_seed": "我有话想当面说。",
            }

        self.fate_runtime._llm_fn = story_model
        session = self.fate_runtime.attach_world(realm, theme_compass="bond_turning", use_llm=True)
        sid = session["session_id"]
        self.engine.tick_physics(advance_minutes=1.1)
        self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        self.assertFalse(self.heartbeat._cognitive_talk(helper, lead, "顺路搭话"))
        self.engine.record_custom_event("dialogue_exchange", actor_id=helper, space_id=realm, payload={
            "speaker_id": helper, "partner_id": lead, "transcript": [{"a": "快去吧。", "b": "我知道。"}],
        })
        before = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)["threads"][0]
        self.assertEqual(before["status"], "active")
        self.assertEqual(before["nodes"][0]["participant_choices"][helper]["execution_status"], "completed")
        self.assertFalse(judgments)
        dialogue_calls = []

        def world_model(system, user, **kwargs):
            if '"intent_a"' not in system:
                return {}
            dialogue_calls.append(user)
            if len(dialogue_calls) == 1:
                return {
                    "intent_a": "去木椅那边坐坐吧，我有话想跟你说。",
                    "intent_b": "好啊，我们一起过去。",
                    "end_chat": True, "speech_act": {
                        "shared_entity_id": bench["entity_id"], "shared_action": "rest", "seated": "b",
                    },
                }
            pairs = [("我想了挺久。", "你慢慢说。"), (confession, rejection)]
            a_line, b_line = pairs[len(dialogue_calls) - 2]
            return {"intent_a": a_line, "intent_b": b_line, "end_chat": len(dialogue_calls) == 3}

        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", side_effect=world_model), \
                patch.object(self.chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent), \
                patch.object(live._social, "submit") as dispatch:
            self.assertTrue(live._meet(lead, partner, "约在木椅旁聊"))
            job = dispatch.call_args
            job.args[0](*job.args[1:], **job.kwargs)
            end = live._actors[lead].speech_until
            self.assertTrue(live._busy_locked(partner, time.monotonic()))
            self.assertFalse(live._meet(helper, lead, "随便聊聊", user_requested=True))
            self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
            self.assertFalse(judgments)
            live._flush_played_dialogues(end)
            after_first = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)["threads"][0]
            focus = after_first["nodes"][0]["dialogue_focus"]
            self.assertEqual(focus["revision"], 1)
            self.assertEqual(after_first["status"], "active")
            self.assertIn("我有话想跟你说", judgments[0])
            self.assertFalse(self.engine.live_actor_frame(lead)["reflection_state"]["joint_visit_id"])
            self.assertIn("已经实际交谈", judgments[0])
            dispatch.reset_mock()
            now = end
            for _ in range(40):
                now += 0.5
                self.engine.tick_physics(advance_minutes=0.08)
                with patch("plugins.nanami_world.live_simulation.time.monotonic", return_value=now):
                    self.assertFalse(live._meet(helper, lead, "路过搭话"))
                    for aid in (lead, partner):
                        if live._actors[aid].next_at <= now:
                            live._advance(aid, live._actors[aid], self.world_settings)
                if dispatch.called:
                    break
            self.assertTrue(dispatch.called, "原搭档应绕过普通社交冷却并继续对话")
            self.assertGreater(self.engine.chebyshev_distance(self.engine.live_actor_frame(lead), bench), 1)
            job = dispatch.call_args
            job.args[0](*job.args[1:], **job.kwargs)
            self.assertIn("我有话想跟你说", dialogue_calls[1])
            live._flush_played_dialogues(live._actors[lead].speech_until)

        final = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)["threads"][0]
        self.assertEqual(final["status"], "resolved")
        self.assertEqual(final["nodes"][0]["dialogue_focus"]["outcome"], "answered")
        self.assertIn(rejection, judgments[-1])
        self.assertFalse(live._dialogue_focus)

    def test_focused_dialogue_timeout_releases_pair_and_records_interruption(self) -> None:
        realm = self.engine.create_space(name="等待中的房间", width=17, height=10, wall_rows=0)["space_id"]
        pair = [self.engine.import_actor(name=name, space_id=realm, x=x, y=4)["actor_id"]
                for name, x in (("日向创", 3), ("七海千秋", 12))]
        self.fate_runtime._llm_fn = lambda system, user: {
            "title": "等待中的谈话", "premise": "两人需要当面说说各自的打算。",
            "situation": "两人在走廊的两头。", "target_space_id": realm,
            "dialogue_focus": {"actor_ids": pair, "topic": "当面谈一谈各自的打算"},
        }
        session = self.fate_runtime.attach_world(realm, theme_compass="bond_turning", use_llm=True)
        self.assertTrue(self.heartbeat.live._dialogue_focus)
        self.engine.tick_physics(advance_minutes=31)
        updated = self.fate_runtime.tick_session(session["session_id"], use_llm=False, sync_models=True)
        thread = updated["threads"][0]
        self.assertEqual(thread["nodes"][0]["dialogue_focus"]["outcome"], "interrupted")
        self.assertEqual(thread["status"], "active")
        self.assertIn("尚未", thread["nodes"][1]["situation"])
        self.assertFalse(self.heartbeat.live._dialogue_focus)

    def test_focused_dialogue_reviews_saved_lines_then_rejoins_across_a_portal(self) -> None:
        realm = self.engine.create_default_town()["town_space_id"]
        cafe = f"{realm}__cafe"
        lead = self.engine.import_actor(name="哈贝特洛特", space_id=realm, x=19, y=15)["actor_id"]
        partner = self.engine.import_actor(name="日向创", space_id=cafe, x=5, y=4)["actor_id"]
        pair = [lead, partner]
        self.world_settings = WorldSettings(heartbeat_use_llm=True)
        live = self.heartbeat.live
        live.social_service = lambda: self.chat
        for aid in pair:
            live._actors[aid] = LiveActor("test", self.engine.live_actor_frame(aid), float("inf"), 0, speed=4)

        def model(system, user):
            if "连续命运编排器" in system:
                self.assertIn("1. " + lead + "：草稿这一句你怎么看？", user)
                return {"action": "continue_dialogue"}
            return {
                "title": "先听听草稿", "premise": "两人要商量草稿。",
                "situation": "两人分开了，还没有谈清草稿。", "target_space_id": realm,
                "dialogue_focus": {"actor_ids": pair, "topic": "确认草稿里哪句是真心话", "required_face_to_face": True},
            }

        self.fate_runtime._llm_fn = model
        session = self.fate_runtime.attach_world(realm, theme_compass="bond_turning", use_llm=True)
        sid = session["session_id"]
        with self.fate_runtime._lock:
            active = self.fate_runtime._sessions[sid]
            focus = active["threads"][0]["nodes"][0]["dialogue_focus"]
            focus.pop("evidence_version")
            focus["revision"] = 1
            focus["exchanges"] = [{
                "speaker_id": lead, "partner_id": partner,
                "transcript": [{"a": "草稿这一句你怎么看？", "b": "我得想想。"}],
            }]
            self.fate_runtime._store.save(active)
        restored = self.fate_runtime.restore_sessions()[0]
        focus = restored["threads"][0]["nodes"][0]["dialogue_focus"]
        self.assertTrue(focus["review_saved"])
        self.assertTrue(live._dialogue_focus[realm][lead].review_pending)
        reviewed = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        focus = reviewed["threads"][0]["nodes"][0]["dialogue_focus"]
        self.assertNotIn("review_saved", focus)
        self.assertEqual(focus["revision"], 1)

        with patch.object(live._social, "submit") as dispatch, patch.object(live, "_request"):
            now = time.monotonic()
            for _ in range(100):
                now += 0.5
                self.engine.tick_physics(advance_minutes=0.015)
                with patch("plugins.nanami_world.live_simulation.time.monotonic", return_value=now):
                    for aid in pair:
                        if live._actors[aid].next_at <= now:
                            live._advance(aid, live._actors[aid], self.world_settings)
                if dispatch.called:
                    break
            self.assertTrue(dispatch.called, "找人的角色应穿过实体门，与原地等候的搭档续聊")
            self.assertEqual(dispatch.call_args.args[2:4], (lead, partner))
        self.assertEqual(self.engine.live_actor_frame(lead)["space_id"], cafe)
        receiver = self.engine.live_actor_frame(partner)
        self.assertEqual((receiver["x"], receiver["y"]), (5, 4))

    def test_focused_dialogue_releases_pair_when_reunion_path_stays_blocked(self) -> None:
        realm = self.engine.create_space(name="隔开的走廊", width=17, height=10, wall_rows=0)["space_id"]
        pair = [self.engine.import_actor(name=name, space_id=realm, x=x, y=4)["actor_id"]
                for name, x in (("日向创", 3), ("七海千秋", 12))]
        self.fate_runtime._llm_fn = lambda _system, _user: {
            "title": "隔开的走廊", "premise": "两人有话还没说完。",
            "situation": "两人在走廊的两头。", "target_space_id": realm,
            "dialogue_focus": {"actor_ids": pair, "topic": "接着说刚才的话"},
        }
        session = self.fate_runtime.attach_world(realm, theme_compass="bond_turning", use_llm=True)
        sid = session["session_id"]
        with self.fate_runtime._lock:
            active = self.fate_runtime._sessions[sid]
            focus = active["threads"][0]["nodes"][0]["dialogue_focus"]
            focus["revision"] = 1
            focus["exchanges"] = [{
                "speaker_id": pair[0], "partner_id": pair[1], "transcript": [{"a": "等一下。", "b": "嗯？"}],
            }]
            self.fate_runtime._sync_dialogue_focus_locked(active)
        live = self.heartbeat.live
        settings = WorldSettings(heartbeat_use_llm=True)
        for aid in pair:
            live._actors[aid] = LiveActor("test", self.engine.live_actor_frame(aid), float("inf"), 0, speed=4)
        with patch.object(self.engine, "plan_actor_route", return_value=[]), patch.object(live, "_request"):
            live._advance(pair[0], live._actors[pair[0]], settings)
            self.engine.tick_physics(advance_minutes=1.6)
            live._advance(pair[0], live._actors[pair[0]], settings)
        interrupted = self.fate_runtime.tick_session(sid, use_llm=False, sync_models=True)
        thread = interrupted["threads"][0]
        self.assertIn("无法到达", thread["nodes"][0]["outcome_summary"])
        self.assertIn("等一下。", thread["nodes"][1]["situation"])
        self.assertEqual(thread["status"], "active")
        self.assertFalse(live._dialogue_focus)

    def test_manual_advance_and_exchange_limit_preserve_the_spoken_plan(self) -> None:
        for force, count in ((True, 1), (False, 8)):
            with self.subTest(force=force):
                realm = self.engine.create_space(name="讨论草稿的房间", width=12, height=10, wall_rows=0)["space_id"]
                pair = [self.engine.import_actor(name=name, space_id=realm, x=x, y=4)["actor_id"]
                        for name, x in (("哈贝特洛特", 3), ("七海千秋", 4))]
                self.fate_runtime._llm_fn = lambda _system, _user: {
                    "title": "去请日向", "premise": "两人商量安排。", "target_space_id": realm,
                    "situation": "两位当事人正在房间里。",
                    "dialogue_focus": {"actor_ids": pair, "topic": "商量谁去叫日向"},
                }
                session = self.fate_runtime.attach_world(realm, theme_compass="bond_turning", use_llm=True)
                sid = session["session_id"]
                with self.fate_runtime._lock:
                    focus = self.fate_runtime._sessions[sid]["threads"][0]["nodes"][0]["dialogue_focus"]
                    focus["revision"] = count - 1
                    focus["exchanges"] = [{
                        "speaker_id": pair[0], "partner_id": pair[1],
                        "transcript": [{"a": "我去男生宿舍叫日向。", "b": "好，我留在这里。"}],
                    } for _ in range(count)]
                self.assertIn(realm, self.heartbeat.live._dialogue_focus)
                thread = self.fate_runtime.tick_session(sid, force_evaluate=force, use_llm=False)["threads"][0]
                self.assertEqual(thread["status"], "active")
                self.assertEqual(thread["active_node_id"], thread["nodes"][1]["node_id"])
                self.assertIn("我去男生宿舍叫日向", thread["nodes"][1]["situation"])
                self.assertFalse(thread["nodes"][1]["dialogue_focus"])
                self.assertNotIn(realm, self.heartbeat.live._dialogue_focus)

    def test_supporting_and_cross_space_preparation_scenes_do_not_reserve_actors(self) -> None:
        realm = self.engine.create_space(name="中庭", width=12, height=10, wall_rows=0)["space_id"]
        study = self.engine.create_space(name="静读室", width=12, height=10, wall_rows=0, parent_space_id=realm)["space_id"]
        lead, helper, partner = [self.engine.import_actor(name=name, space_id=sid, x=x, y=4)["actor_id"]
                                 for name, sid, x in (("日向创", realm, 3), ("哈贝特洛特", realm, 4), ("七海千秋", study, 4))]
        session = self.fate_runtime.attach_world(realm, auto_pluck=False, use_llm=True)
        for peer in (helper, partner):
            with self.subTest(peer=peer):
                self.fate_runtime._llm_fn = lambda _system, _user: {
                    "title": "准备草稿", "situation": "大家要先把草稿准备好。", "target_space_id": realm,
                    "core_actor_ids": [lead, partner],
                    "dialogue_focus": {"actor_ids": [lead, peer], "topic": "先商量草稿的准备工作"},
                }
                thread = self.fate_runtime.pluck_thread(session["session_id"], theme_compass="bond_turning", use_llm=True)["thread"]
                self.assertEqual(thread["core_actor_ids"], [lead, partner])
                self.assertFalse(thread["nodes"][0]["dialogue_focus"])
                self.assertNotIn(realm, self.heartbeat.live._dialogue_focus)

    def test_no_forced_advance_on_pending_timeout_nudge_and_verbatim_dialogue_grounding(self) -> None:
        weaver_prompts: list[str] = []

        def fake_story_llm(sys_p: str, usr_p: str) -> dict:
            if "连续命运编排器" in sys_p:
                weaver_prompts.append(usr_p)
                return {
                    "action": "next_node",
                    "outcome_summary": "两人刚坦承一时还没想好措辞。",
                    "phase": "crossroads",
                    "tension": 0.55,
                    "node": {
                        "title": "街角再谈",
                        "phase": "crossroads",
                        "situation": "日向创站在原地思考该如何正式开口。",
                        "dilemma_frame": "接下来要不要直白地说出心意？",
                        "world_setup": {"spawn_props": []},
                    },
                }
            if "命运织机" in sys_p:
                return {
                    "title": "希望峰学院最终的表白",
                    "premise": "日向创准备向七海千秋表达心意。",
                    "public_incident_fact": "放学后日向创想亲自找到七海千秋把话说明白。",
                    "node_title": "走廊停步",
                    "situation": "两人各自留在不同空间，日向创打算迈出第一步。",
                    "dilemma_frame": "此刻是主动去找她，还是留在原地？",
                    "world_setup": {"spawn_props": []},
                }
            if "【我是谁】日向创" in usr_p:
                tgt = self._extract_peer_id(usr_p, "七海千秋")
                return {
                    "stance": "confide",
                    "choice_summary": "决定去找七海千秋当面说清楚。",
                    "inner_reason": "不能再逃避，要当面跟她说话。",
                    "world_intent": {"kind": "social", "target": tgt, "action": "", "reason": "找七海当面说话"},
                    "speech_seed": "七海，我有件事一直想亲口跟你说。",
                }
            if "【我是谁】林间" in usr_p:
                tgt = self._extract_peer_id(usr_p, "苏晴")
                return {
                    "stance": "mediate",
                    "choice_summary": "先和旁边的苏晴通个气。",
                    "inner_reason": "顺手了解走廊情况。",
                    "world_intent": {"kind": "social", "target": tgt, "action": "", "reason": "跟苏晴通个气"},
                }
            return {
                "stance": "observe",
                "choice_summary": "在原处安静等待。",
                "inner_reason": "先看清眼前动静。",
                "world_intent": {"kind": "observe", "target": "", "action": "", "reason": "原地留意"},
            }

        self.fate_runtime._llm_fn = fake_story_llm
        orig_cw = self.world_adapter.create_world
        with patch.object(
            self.world_adapter,
            "create_world",
            side_effect=lambda *a, **kw: orig_cw(*a, **{**kw, "use_llm": False}),
        ):
            sess = self.fate_runtime.start_standalone_stage({
                "title": "希望峰学院最终的表白",
                "outline": "日向创想鼓起勇气向七海千秋表白",
                "world_mode": "town",
                "experience_mode": "observer",
                "imported_characters": ["日向创", "七海千秋"],
                "native_npc_count": 2,
                "building_count": 3,
                "max_nodes": 0,
                "use_llm": True,
            })
        sid = sess["session_id"]
        realm = sess["realm_space_id"]
        self.addCleanup(self.fate_runtime.detach_session, sid, delete_world=True)

        hinata_id = next(aid for aid, lg in sess["ledgers"].items() if lg.get("name") == "日向创")
        nanami_id = next(aid for aid, lg in sess["ledgers"].items() if lg.get("name") == "七海千秋")
        nat_ids = [aid for aid, lg in sess["ledgers"].items() if lg.get("is_native_npc")]

        # Protagonist framing avoids "因旅途或骤雨暂留本镇" in a bond_turning story, and prioritizes speech_seed
        t1 = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        brief_h = self.engine.get_actor_private_brief(hinata_id, space_id=realm)
        self.assertIn("当前决定", brief_h)
        self.assertIn("见面想表达（对「七海千秋」）", brief_h)
        self.assertNotIn("因旅途或骤雨", brief_h)

        # Even if 10 simulated minutes elapse with NO completed lead action, engine MUST NOT fake-advance!
        self.engine.tick_physics(advance_minutes=10.0)
        # Native bystanders finish deciding & chatting with each other -> MUST NOT hijack/advance while lead is pending!
        t2 = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        self.assertEqual(len(t2["threads"][0]["nodes"]), 1)
        ch_h = t2["threads"][0]["nodes"][0]["participant_choices"][hinata_id]
        self.assertGreaterEqual(int(ch_h.get("nudge_count") or 0), 1)

        with self.engine._lock:
            self.engine._append_event_locked(
                "dialogue_exchange",
                actor_id=nat_ids[0],
                space_id=realm,
                payload={
                    "speaker_id": nat_ids[0],
                    "partner_id": nat_ids[1],
                    "speaker_name": "林间",
                    "partner_name": "苏晴",
                    "summary": "两人寒暄了走廊秩序。",
                    "transcript": [{"a": "门外没人吧？", "b": "都安静着呢。"}],
                },
            )
        t3 = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        self.assertEqual(len(t3["threads"][0]["nodes"]), 1)

        # Now 日向创 actually talks to 七海千秋 with verbatim transcript -> ledger & weaver prompt receive exact lines & locations!
        with self.engine._lock:
            self.engine._append_event_locked(
                "dialogue_exchange",
                actor_id=hinata_id,
                space_id=realm,
                payload={
                    "speaker_id": hinata_id,
                    "partner_id": nanami_id,
                    "speaker_name": "日向创",
                    "partner_name": "七海千秋",
                    "summary": "日向创坦言还没想好措辞。",
                    "transcript": [{"a": "我紧张得一时不知道怎么开头。", "b": "慢慢想，我不催你。"}],
                },
            )
        t4 = self.fate_runtime.tick_session(sid, use_llm=True, sync_models=True)
        self.assertEqual(len(t4["threads"][0]["nodes"]), 2)
        self.assertTrue(weaver_prompts)
        last_wp = weaver_prompts[-1]
        self.assertIn("在场角色当前真实位置与姿态", last_wp)
        self.assertIn("原话：", last_wp)
        self.assertIn("我紧张得一时不知道怎么开头", last_wp)


if __name__ == "__main__":
    unittest.main()
