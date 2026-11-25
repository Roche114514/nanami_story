"""End-to-end tests for the upgraded Fate Weaver core engine (nanami_story v0.3.0)."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from _shinsekai import STORY_CODE, WORLD_CODE

from plugins.nanami_story import story_page
from plugins.nanami_story.adapters.llm_adapter import ask_llm_json
from plugins.nanami_story.adapters.persona_adapter import PersonaAdapter
from plugins.nanami_story.adapters.world_adapter import WorldAdapter
from plugins.nanami_story.beat_weaver import _pick_stage_anchor
from plugins.nanami_story.choice_engine import _grounded_catalog
from plugins.nanami_story.fate_runtime import FateRuntime
from plugins.nanami_story.plugin import NanamiStoryPlugin
from plugins.nanami_story.settings import SettingsStore, StorySettings
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.external_bridge import (
    WorldBridge,
    register_world_bridge,
    unregister_world_bridge,
)
from plugins.nanami_world.heartbeat import WorldHeartbeat
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


class StoryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

        self.base_nanami = self.root / "data" / "nanami"
        self.main_state = self.base_nanami / "state"
        self.main_state.mkdir(parents=True, exist_ok=True)
        (self.base_nanami / "pointers.yaml").write_text(
            "active_edit_id: nanami_chiaki\npointers:\n"
            "  - id: nanami_chiaki\n    enabled: true\n    character_name: 七海千秋\n"
            "    profile_dir: profiles/nanami_chiaki\n    state_root: \"\"\n",
            encoding="utf-8",
        )
        chiaki_prof = self.base_nanami / "profiles" / "nanami_chiaki"
        chiaki_prof.mkdir(parents=True, exist_ok=True)
        (chiaki_prof / "soul.md").write_text("我是七海千秋，喜欢电子游戏和和大家安静相处。", encoding="utf-8")
        self.mind_seed = json.dumps({"shinsekai:main": {"energy": 0.4, "moodV": 0.2, "updatedAt": 9999.0}})
        self.rel_seed = json.dumps({"targets": {"user": {"known": True, "rel_stage": "friend", "trust": 0.88}}})
        self.traits_seed = json.dumps({
            "traits": {
                "effortfulControl": 0.80,
                "surgency": 0.35,
                "needProfile": {"safety": 0.45, "relatedness": 0.85, "competence": 0.72},
                "social": {"prosocial": 0.90, "normCompliance": 0.72, "affiliation": 0.80},
                "attachment": {"avoidance": 0.28},
            }
        })
        (self.main_state / "mind.json").write_text(self.mind_seed, encoding="utf-8")
        (self.main_state / "relations.json").write_text(self.rel_seed, encoding="utf-8")
        (self.main_state / "traits.json").write_text(self.traits_seed, encoding="utf-8")

        (self.root / "data" / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "config" / "characters.yaml").write_text(
            "- name: 七海千秋\n  setting: 超高校级的游戏玩家\n  desc: 沉着温柔的少女\n",
            encoding="utf-8",
        )

        self.clock = patch("time.time", return_value=datetime(2026, 8, 1, 14, 0).timestamp())
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

    def test_zero_direct_plugin_coupling(self) -> None:
        world_dir = WORLD_CODE
        story_dir = STORY_CODE
        for py in world_dir.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            self.assertNotIn("import nanami_story", src, msg=f"{py} must not import nanami_story")
            self.assertNotIn("from nanami_story", src, msg=f"{py} must not import nanami_story")
        for py in story_dir.rglob("*.py"):
            if "tests" in py.relative_to(story_dir).parts:
                continue
            src = py.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\b(?:import|from)\s+.*nanami_persona\b", src), msg=f"{py} must not import nanami_persona")
            self.assertIsNone(re.search(r"\b(?:import|from)\s+.*nanami_world\b", src), msg=f"{py} must not directly import nanami_world")

    def test_settings_migration_and_zero_main_persona_mutation(self) -> None:
        migrated = StorySettings.from_mapping({"default_experience_mode": "blind_play", "poll_interval_sec": 2.0})
        self.assertEqual(migrated.default_experience_mode, "co_play")
        self.assertEqual(migrated.poll_interval_sec, 2.0)

        # Creating & progressing a standalone story must NEVER touch main persona files
        session = self.fate_runtime.start_standalone_stage({
            "title": "秋夜汤宿的灯",
            "world_mode": "town",
            "imported_characters": ["七海千秋"],
            "native_npc_count": 2,
            "building_count": 4,
            "use_llm": False,
        })
        self.addCleanup(self.fate_runtime.detach_session, session["session_id"], delete_world=True)
        for _ in range(5):
            cur = self.fate_runtime.tick_session(session["session_id"], force_evaluate=True, use_llm=False)
            if cur["threads"][0]["status"] == "resolved":
                break

        self.assertEqual((self.main_state / "mind.json").read_text(encoding="utf-8"), self.mind_seed)
        self.assertEqual((self.main_state / "relations.json").read_text(encoding="utf-8"), self.rel_seed)
        self.assertEqual((self.main_state / "traits.json").read_text(encoding="utf-8"), self.traits_seed)

    def test_standalone_story_interior_focus_and_native_personality_divergence(self) -> None:
        # Three contrasting native archetypes (no imported host chars needed)
        session = self.fate_runtime.start_standalone_stage({
            "title": "二楼窗边的信",
            "world_mode": "town",
            "experience_mode": "observer",
            "native_npc_count": 3,
            "building_count": 4,
            "theme_compass": "mystery_ripple",
            "max_nodes": 4,
            "use_llm": False,
        })
        sid = session["session_id"]
        realm = session["realm_space_id"]
        self.addCleanup(self.fate_runtime.detach_session, sid, delete_world=True)

        ws = self.world_adapter.query_world_state(realm)
        node1 = session["threads"][0]["nodes"][0]
        # Zero clues scattered onto town center plaza
        self.assertNotEqual(node1["target_space_id"], realm)
        deployed = set(node1["deployed_entity_ids"])
        self.assertGreaterEqual(len(deployed), 1)
        prop_ent = next(e for e in ws["realm_entities"] if e["entity_id"] in deployed)
        self.assertNotEqual(prop_ent["space_id"], realm)

        # Native residents start across building spaces, not clumped on center plaza
        npcs = [a for a in ws["actors"] if not a.get("is_player")]
        self.assertEqual(len(npcs), 3)
        self.assertTrue(all(a["space_id"] != realm for a in npcs))

        # Advance time so observer cast perceive the thread and make divergent autonomous choices
        self.engine.tick_physics(advance_minutes=4.0)
        ticked = self.fate_runtime.tick_session(sid, use_llm=False)
        choices = ticked["threads"][0]["nodes"][0]["participant_choices"]
        self.assertEqual(len(choices), 3)
        self.assertGreaterEqual(len({c["stance"] for c in choices.values()}), 2)

        # Sequential beat moves into an interior space and ends cleanly into afterglow chapter_chronicle
        for _ in range(5):
            cur = self.fate_runtime.tick_session(sid, force_evaluate=True, use_llm=False)
            if cur["threads"][0]["status"] == "resolved":
                break
        thread = cur["threads"][0]
        self.assertEqual(thread["status"], "resolved")
        self.assertEqual(thread["phase"], "afterglow")
        self.assertTrue(all(n["target_space_id"] != realm for n in thread["nodes"]))
        self.assertTrue(cur["chronicle_archive"])

    def test_story_page_rpc_and_plugin_lifecycle(self) -> None:
        register_world_bridge(self.bridge)
        self.addCleanup(unregister_world_bridge)

        sandbox_player = self.engine.ensure_player_avatar(space_id="room_default")["actor_id"]
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
        self.assertTrue(any(p.page_id == "story_lobby" for p in reg.pages))
        self.assertEqual(next(c.action["mode"] for c in reg.chat_ui if c.contribution_id == "open_story"), "overlay")

        lobby = reg.actions["get_lobby_state"]({})
        self.assertTrue(lobby["ok"])
        self.assertTrue(lobby["world_available"])
        self.assertTrue(any(c["name"] == "七海千秋" for c in lobby["characters"]))

        created = reg.actions["create_fate_story"]({
            "title": "茶室里的老怀表",
            "world_mode": "town",
            "building_count": 3,
            "imported_characters": ["七海千秋"],
            "native_npc_count": 2,
            "use_llm": False,
        })
        self.assertTrue(created["ok"])
        self.assertEqual(self.engine.snapshot_for_ui()["active_space_id"], "room_default")
        self.assertEqual(self.engine.live_actor_frame(sandbox_player)["space_id"], "room_default")

        sid = created["session"]["session_id"]
        realm = created["session"]["realm_space_id"]
        self.assertTrue(reg.actions["switch_to_story_world"]({"session_id": sid})["ok"])
        self.assertEqual(self.engine.snapshot_for_ui()["realm_space_id"], realm)

        deleted = reg.actions["detach_fate_session"]({"session_id": sid, "delete_world": True})
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["sessions"], [])
        self.assertNotIn(realm, self.engine._world["spaces"])
        self.assertEqual(self.engine.live_actor_frame(sandbox_player)["space_id"], "room_default")

        # Verify plugin entry initializes pure FateRuntime cleanly
        plugin = NanamiStoryPlugin()
        p_reg = _DummyRegister()
        fake_host = type("Host", (), {"project_data_dir": str(self.root / "host_data")})()
        plugin.initialize(p_reg, STORY_CODE, fake_host)
        self.addCleanup(plugin.shutdown)
        self.assertIsNotNone(plugin.fate_runtime)
        plugin.shutdown()

    def test_llm_adapter_decodes_host_response_formats(self) -> None:
        class _MockAdapter:
            def __init__(self) -> None:
                self.calls = 0

            def chat(self, *, messages, temperature, max_tokens, stream=False, **kwargs):
                self.calls += 1
                if "response_format" in kwargs:
                    raise TypeError("unexpected keyword argument 'response_format'")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='```json\n{"stance": "confront", "choice_summary": "核对怀表"}\n```'))]
                )

            def decode_response(self, resp):
                return SimpleNamespace(content=resp.choices[0].message.content)

        mock_ad = _MockAdapter()
        res = ask_llm_json("sys", "usr", adapter_provider=lambda: mock_ad, timeout_sec=3.0)
        self.assertIsInstance(res, dict)
        self.assertEqual(res["stance"], "confront")
        self.assertEqual(res["choice_summary"], "核对怀表")
        self.assertEqual(mock_ad.calls, 2)

        class _RawChoicesAdapter:
            def chat(self, *, messages, temperature, max_tokens, stream=False, response_format=None):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true, "title": "钟楼命案"}'))]
                )

        res2 = ask_llm_json("sys", "usr", adapter_provider=lambda: _RawChoicesAdapter(), timeout_sec=3.0)
        self.assertEqual(res2, {"ok": True, "title": "钟楼命案"})

    def test_murder_case_grounding_cross_space_inspection_and_resident_identity(self) -> None:
        session = self.fate_runtime.start_standalone_stage({
            "title": "雨夜小镇杀人事件",
            "outline": "在一场暴雨之后，小镇的钟楼上发现了一具尸体，村民中有四个可疑的嫌疑人",
            "world_mode": "town",
            "experience_mode": "observer",
            "imported_characters": ["七海千秋"],
            "native_npc_count": 4,
            "building_count": 5,
            "use_llm": False,
        })
        sid = session["session_id"]
        realm = session["realm_space_id"]
        self.addCleanup(self.fate_runtime.detach_session, sid, delete_world=True)

        thread = session["threads"][0]
        node1 = thread["nodes"][0]
        ws = self.world_adapter.query_world_state(realm)

        self.assertEqual(thread["theme_compass"], "mystery_ripple")
        self.assertEqual((ws.get("weather") or {}).get("current"), "rain")
        self.assertIn("遗体", thread.get("public_incident_fact") or "")
        deployed_ids = list(node1.get("deployed_entity_ids") or ())
        self.assertGreaterEqual(len(deployed_ids), 2)
        self.assertNotEqual(node1["target_space_id"], realm)

        fake_ws = {
            "realm_space_id": "t_x",
            "spaces": [
                {"space_id": "t_x", "name": "镇", "space_kind": "town"},
                {"space_id": "t_x__b1", "name": "普通木屋", "space_kind": "building", "floor_index": 0, "zones": [{"zone_id": "z1", "name": "客厅", "enclosed": True}]},
                {"space_id": "t_x__b2", "name": "老钟楼 2F", "space_kind": "building", "floor_index": 1, "zones": [{"zone_id": "z2", "name": "齿轮密室", "enclosed": True}]},
            ],
        }
        matched_anchor = _pick_stage_anchor(fake_ws, hint_text="在一场暴雨之后，小镇的钟楼上发现了一具尸体")
        self.assertEqual(matched_anchor["space_id"], "t_x__b2")

        first_actor = next(a["actor_id"] for a in ws["actors"] if a.get("is_native_npc"))
        cat = _grounded_catalog(first_actor, ws, node1)
        rel_ids = [e["entity_id"] for e in cat["relevant_entities"]]
        for eid in deployed_ids:
            self.assertIn(eid, rel_ids[:len(deployed_ids)])
        for row in cat["relevant_entities"]:
            ent = cat["entities"][row["entity_id"]]
            self.assertNotIn("portal", ent.get("traits") or {})
            self.assertNotIn("interior_door", ent.get("tags") or ())

        native_aids = [aid for aid, lg in session["ledgers"].items() if lg.get("is_native_npc")]
        imp_aids = [aid for aid, lg in session["ledgers"].items() if not lg.get("is_native_npc") and not lg.get("is_player")]
        self.assertEqual(len(native_aids), 4)
        self.assertEqual(len(imp_aids), 1)
        for naid in native_aids:
            lg = session["ledgers"][naid]
            sr = lg.get("story_role") or {}
            self.assertEqual(sr.get("role_category"), "local_resident")
            self.assertTrue(sr.get("local_role"))
            self.assertTrue(sr.get("relationship_to_incident"))
            raw_act = self.engine._world["actors"][naid]
            cog = raw_act["autonomy"]["cognition"]
            self.assertGreaterEqual(len(cog.get("places") or {}), 2)
            self.assertTrue(cog.get("portals"))
            self.assertEqual(cog.get("last_space"), raw_act["space_id"])

        from plugins.nanami_world import persona_bridge
        n0_name = session["ledgers"][native_aids[0]]["name"]
        n1_name = session["ledgers"][native_aids[1]]["name"]
        imp_name = session["ledgers"][imp_aids[0]]["name"]
        rels_n0 = persona_bridge.read_actor_relations(
            self.root, n0_name, space_id=realm, world_data_root=self.engine.data_root,
            known_actors=[n1_name, imp_name], isolated_persona=True,
        )
        self.assertTrue(rels_n0[f"char:{n1_name}"]["known"])
        self.assertFalse(rels_n0[f"char:{imp_name}"]["known"])

        self.heartbeat.step(advance_minutes=0.5, use_llm=False)
        for naid in native_aids:
            exp = self.engine._world["actors"][naid]["autonomy"]["cognition"].get("experience") or ""
            self.assertNotIn("第一次走进", exp)

        self.engine.tick_physics(advance_minutes=4.0)
        after_choices = self.fate_runtime.tick_session(sid, use_llm=False)
        choices = after_choices["threads"][0]["nodes"][0]["participant_choices"]
        self.assertEqual(len(choices), 5)
        for rec in choices.values():
            blob = f"{rec.get('choice_summary', '')} {rec.get('inner_reason', '')} {rec.get('speech_seed', '')}"
            self.assertNotIn("刚才的动静", blob)
            self.assertNotIn("只是风声", blob)
        ext_state = (self.engine._world.get("external_actor_state") or {}).get(realm) or {}
        for st in ext_state.values():
            self.assertNotIn("异样动静", st.get("private_brief") or "")
            self.assertNotIn("刚才的动静", st.get("private_brief") or "")

        # Pacing gate does not jump after only 2.5 sim minutes while entity choices are pending
        self.engine.tick_physics(advance_minutes=2.5)
        mid_tick = self.fate_runtime.tick_session(sid, use_llm=False)
        self.assertEqual(len(mid_tick["threads"][0]["nodes"]), 1)

        # Cross-space entity commitment: place actor in a different building, run heartbeat until it crosses spaces,
        # opens any interior door, inspects/reads the real deployed prop, updates L1 ledger and private_brief
        entity_actor = next(
            (aid for aid, rec in choices.items() if (rec.get("world_intent") or {}).get("kind") == "entity" and (rec.get("world_intent") or {}).get("target") in deployed_ids),
            None,
        )
        self.assertIsNotNone(entity_actor)
        target_eid = choices[entity_actor]["world_intent"]["target"]
        other_sp = next(
            sp["space_id"] for sp in ws["spaces"]
            if sp.get("space_kind") != "town" and sp["space_id"] != node1["target_space_id"]
        )
        with self.engine._lock:
            ox, oy = self.engine._find_open_floor_cell_locked(
                self.engine._world["spaces"][other_sp],
                ignore_actor_id=entity_actor,
            )
        self.engine.place_actor(entity_actor, other_sp, ox, oy)
        for _ in range(25):
            self.heartbeat.step(advance_minutes=0.5, force_autonomy=True, use_llm=False, actor_id=entity_actor)
            cur = self.fate_runtime.tick_session(sid, use_llm=False)
            ch_now = cur["threads"][0]["nodes"][0]["participant_choices"][entity_actor]
            if ch_now.get("execution_status") == "completed":
                break
        final_cur = self.fate_runtime.get_session(sid)
        final_ch = final_cur["threads"][0]["nodes"][0]["participant_choices"][entity_actor]
        self.assertEqual(final_ch["execution_status"], "completed")
        actor_exps = final_cur["ledgers"][entity_actor]["experienced_events"]
        self.assertTrue(any(e.get("entity_id") == target_eid and e.get("kind") in ("entity_inspected", "entity_read") and "查见内容" in e.get("summary", "") for e in actor_exps))
        updated_brief = self.engine.get_actor_private_brief(entity_actor, space_id=realm)
        self.assertIn("亲见实证", updated_brief)


if __name__ == "__main__":
    unittest.main()
