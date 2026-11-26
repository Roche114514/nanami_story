"""Standalone dialogue stories through the runtime, SDK hooks and lobby actions."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _shinsekai  # noqa: F401
from sdk.hooks import BeforeChatContext, MessageAddedContext

from plugins.nanami_story import story_page
from plugins.nanami_story.adapters import background_adapter
from plugins.nanami_story.adapters.background_adapter import BACKGROUND_SCENE_KEYS, BackgroundAdapter
from plugins.nanami_story.adapters.host_chat_adapter import HostChatAdapter
from plugins.nanami_story.adapters.persona_adapter import PersonaAdapter
from plugins.nanami_story.adapters.world_adapter import WorldAdapter
from plugins.nanami_story.beat_weaver import evaluate_and_weave_dialogue_beat
from plugins.nanami_story.character_chronicle import active_dialogue_beat
from plugins.nanami_story.dialogue_injector import (
    DialogueInjector, SCRIPT_HEADER, _project_dialogue, _user_prefixes, _user_text,
)
from plugins.nanami_story.fate_runtime import (
    FateRuntime, OBSERVER_DEFAULT_MAX_AUTO_TURNS, OBSERVER_MAX_SUBMIT_FAILURES, OBSERVER_PENDING_TIMEOUT_SEC,
)
from plugins.nanami_story.fate_schema import redact_fate_session_for_ui
from plugins.nanami_story.mouth_rewriter import DialogueMouthRewriter, StoryMouthDialogHandler
from plugins.nanami_story.plugin import NanamiStoryPlugin
from plugins.nanami_story.settings import SettingsStore, StorySettings, canonical_history_path, normalize_dialogue_preset


BACKGROUND_YAML = """\
- name: Night Stage
  sprite_prefix: night
  sprites:
  - path: data/backgrounds/night/platform.jpg
  - path: data/backgrounds/night/library.jpg
  - path: data/backgrounds/night/spare.jpg
  bg_tags: |
    场景 2：深夜图书馆

    场景 1: 旧站月台：雨后
    场景 3：
  bgm_list:
  - data/bgm/night/rain.flac
  - data/bgm/night/books.flac
  bgm_tags: |
    音乐 1: 雨声，静谧
    音乐 2：阅览室
- name: 透明场景
  sprites: [{path: data/backgrounds/clear.png}]
- name: 透明背景
  sprites: [{path: data/backgrounds/clear2.png}]
- name: 空背景组
  sprites: []
"""
BUILTIN_BACKGROUND = {"background_mode": "shinsekai", "opening_background": "Night Stage"}


def write_background_config(root: Path) -> Path:
    path = root / "data" / "config" / "background.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BACKGROUND_YAML, encoding="utf-8")
    return path


def user(text: str, identity: str = "") -> dict:
    return {"role": "user", "content": text, "_chatTurnId": identity}


def assistant(text: str, speaker: str = "秋", **extra) -> dict:
    return {"role": "assistant", "content": json.dumps({"dialog": [
        {"character_name": "cot", "speech": "不应写进记事"},
        {"character_name": "narr", "speech": "旁白不冒充角色"},
        {"character_name": speaker, "speech": text},
    ]}, ensure_ascii=False), **extra}


class _Register:
    def __init__(self) -> None:
        self.pages = {}
        self.hooks = {}
        self.submitted = []
        self.dialog_handler = None

    def register_frontend_config_page(self, contribution) -> None:
        self.pages[contribution.page_id] = contribution

    def register_init_chat_hook(self, hook, **_kwargs) -> None:
        self.hooks["init"] = hook

    def register_before_chat_hook(self, hook) -> None:
        self.hooks["before"] = hook

    def register_message_added_hook(self, hook) -> None:
        self.hooks["added"] = hook

    def register_message_handler(self, *, dialog_media_handler) -> None:
        self.dialog_handler = dialog_media_handler

    def frontend_user_input(self) -> HostChatAdapter:
        return HostChatAdapter(self.submitted.append)

    def action(self, name, **values) -> dict:
        action = next(row for row in self.pages["story_lobby"].actions if row.id == name)
        return action.run(values)


class BackgroundAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.adapter = BackgroundAdapter(str(self.root))

    def test_catalog_parses_numbered_labels_and_resolves_scenes(self):
        write_background_config(self.root)
        rows = self.adapter.list_backgrounds()
        self.assertEqual(["Night Stage", "空背景组"], [row["name"] for row in rows])
        group = self.adapter.get_background("night stage")
        self.assertEqual(("night", 3, 2), (group["sprite_prefix"], group["scene_count"], group["bgm_count"]))
        self.assertEqual(["旧站月台：雨后", "深夜图书馆", "场景 3"], [scene["label"] for scene in group["scenes"]])
        self.assertEqual({"index": 2, "label": "阅览室", "path": "data/bgm/night/books.flac"}, group["bgms"][1])
        first, second, _ = group["scenes"]
        self.assertEqual(first, self.adapter.resolve_scene("Night Stage", scene_index=1, scene_name="深夜图书馆"))
        self.assertEqual(second, self.adapter.resolve_scene("Night Stage", scene_index=999, scene_name="图书馆"))
        self.assertIsNone(self.adapter.resolve_scene("Night Stage", scene_index=999))
        self.assertEqual({"background_name": "Night Stage", **second}, self.adapter.find_scene_by_path(
            r".\DATA\Backgrounds\Night\LIBRARY.JPG",
        ))
        self.assertIsNone(self.adapter.find_scene_by_path("data/backgrounds/clear.png"))
        self.assertEqual(second, self.adapter.match_scene_for_text("Night Stage", "她晚上去了图书馆还书。"))
        self.assertEqual(first, self.adapter.match_scene_for_text("Night Stage", "只能沉默"))
        self.assertIsNone(self.adapter.match_scene_for_text("空背景组", "月台"))
        self.assertIsNone(self.adapter.match_scene_for_text("不存在", "月台"))
        rows[0]["scenes"][1]["label"] = "覆盖缓存"
        self.assertEqual(second, self.adapter.resolve_scene("Night Stage", scene_index=2))

    def test_missing_invalid_and_empty_yaml_clear_the_mtime_cache(self):
        self.assertEqual([], self.adapter.list_backgrounds())
        path = write_background_config(self.root)
        stamp = path.stat().st_mtime_ns
        with patch.object(self.adapter, "_read_backgrounds", wraps=self.adapter._read_backgrounds) as read:
            self.assertEqual(2, len(self.adapter.list_backgrounds()))
            self.assertEqual(2, len(self.adapter.list_backgrounds()))
            read.assert_called_once()
            for offset, content in enumerate(("- [", "", BACKGROUND_YAML), 1):
                path.write_text(content, encoding="utf-8")
                changed = stamp + offset * 1_000_000_000
                os.utime(path, ns=(changed, changed))
                self.assertEqual(2 if content == BACKGROUND_YAML else 0, len(self.adapter.list_backgrounds()))
            self.assertEqual(4, read.call_count)
        with patch.object(background_adapter, "yaml", None):
            self.assertEqual([], BackgroundAdapter(self.root).list_backgrounds())
        path.unlink()
        self.assertEqual([], self.adapter.list_backgrounds())


class DialogueStoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.history = self.root / "history" / "dialogue"
        self.data = self.root / "story"
        self.world = WorldAdapter(bridge=None)
        self.persona = PersonaAdapter(self.root)
        self.settings = SettingsStore(self.data / "settings.json")
        self.settings.update({"use_llm_compiler": False})
        self.runtime = self.new_runtime()
        self.sid = ""
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {
            "SHINSEKAI_CHAT_LAUNCH_CONFIG": json.dumps({"history": str(self.history / "active.json")}),
            "SHINSEKAI_MEMORY_SERVICE_URL": "",
        }).start()

    def new_runtime(self, **kwargs) -> FateRuntime:
        kwargs.setdefault("background_adapter", BackgroundAdapter(self.root))
        runtime = FateRuntime(self.world, self.persona, self.data, settings_provider=self.settings.get, **kwargs)
        self.addCleanup(runtime.close)
        return runtime

    def create(self, characters=("秋", "澪"), **kwargs) -> dict:
        session = self.runtime.start_dialogue_story({
            "title": "末班列车", "outline": "有人留在车站等一封信。", "characters": list(characters),
            "history_path": str(self.history), "conversation_id": "conversation-1", "use_llm": False,
            "custom_draft": {
                "premise": "留在旧站等最后一趟车", "scene_name": "旧站月台", "atmosphere": "雨声渐小",
                "public_incident_fact": "车站广播刚停。",
                "node1": {"situation": "候车室只剩一盏灯。", "dilemma_frame": "该问问对方为何折返吗？"},
                "character_roles": {
                    "秋": {"local_role": "候车旅客", "relationship_to_incident": "秋独有的信封秘密"},
                    "澪": {"local_role": "值班员", "relationship_to_incident": "澪独有的钥匙秘密"},
                },
            }, **kwargs,
        })
        self.sid = session["session_id"]
        return session

    def injector(self, *names, runtime=None, sync_models=True, mouth_rewriter=None):
        injector = DialogueInjector(runtime or self.runtime, self.settings.get, sync_models=sync_models,
                                    mouth_rewriter=mouth_rewriter)
        init = SimpleNamespace(character_names=names, session_id="host-chat", report=Mock())
        injector.init_chat(init)
        return injector, init

    def before(self, injector, messages):
        context = BeforeChatContext(messages=list(messages), tools=None, generation_kwargs={}, stream=True)
        injector.before_chat(context)
        return context.messages

    def added(self, injector, messages):
        last = messages[-1]
        injector.message_added(MessageAddedContext(last["role"], last, messages))

    def session(self):
        return self.runtime.get_session(self.sid)

    def test_builtin_background_preview_create_presets_and_validation(self):
        write_background_config(self.root)
        register = _Register()
        story_page.register_pages(register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
                                  settings_store=self.settings)
        lobby = register.action("get_lobby_state")
        self.assertFalse(lobby["world_available"])
        self.assertEqual(self.runtime.list_host_backgrounds(), lobby["backgrounds"])
        self.assertEqual("Night Stage", lobby["backgrounds"][0]["name"])

        config = {**BUILTIN_BACKGROUND, "characters": ["秋"], "outline": "她去了图书馆还书。", "use_llm": False}
        auto = self.runtime.preview_dialogue_story(config)
        self.assertEqual((2, "深夜图书馆"), (auto["background_scene_index"], auto["scene_name"]))
        self.assertEqual(2, self.runtime.preview_dialogue_story({**config, "opening_scene_index": 200})[
            "background_scene_index"])
        pinned = {**config, "opening_background": "night stage", "opening_scene_index": "1",
                  "allow_scene_transitions": False}
        draft = self.runtime.preview_dialogue_story(pinned)
        self.assertEqual((1, "旧站月台：雨后"), (draft["background_scene_index"], draft["scene_name"]))
        created = register.action("create_dialogue_story", **pinned, custom_draft={
            **auto, "background_group": "自造背景组", "background_scene_path": "自造路径",
        })
        self.assertTrue(created["ok"], created)
        session = created["session"]
        scene_context = session["scene_context"]
        node = active_dialogue_beat(session)[1]
        self.assertEqual("shinsekai", scene_context["background_mode"])
        self.assertEqual("Night Stage", scene_context["opening_background"])
        self.assertFalse(scene_context["allow_scene_transitions"])
        self.assertEqual(1, scene_context["opening_scene_index"])
        for key in (*BACKGROUND_SCENE_KEYS, "scene_name"):
            self.assertEqual(draft[key], node[key])
            self.assertEqual(draft[key], scene_context[key])

        saved = register.action("save_dialogue_preset", session_id=session["session_id"])["preset"]
        exported = register.action("export_dialogue_preset", preset_id=saved["preset_id"])["json"]
        imported = register.action("import_dialogue_preset", json=exported)["preset"]
        for key in ("background_mode", "opening_background", "opening_scene_index", "allow_scene_transitions", "custom_draft"):
            self.assertEqual(saved[key], imported[key])
        self.assertEqual(draft["background_scene_path"], imported["custom_draft"]["background_scene_path"])
        self.assertEqual(draft, self.runtime.preview_dialogue_story({**imported, "use_llm": False}))
        autoload = self.runtime.start_dialogue_story(config)
        self.assertEqual(2, active_dialogue_beat(autoload)[1]["background_scene_index"])
        self.assertEqual(0, autoload["scene_context"]["opening_scene_index"])
        for invalid in ("", "不在目录里", "空背景组"):
            for method in (self.runtime.preview_dialogue_story, self.runtime.start_dialogue_story):
                with self.subTest(group=invalid, operation=method.__name__):
                    with self.assertRaisesRegex(ValueError, "Shinsekai 内置背景组"):
                        method({**config, "opening_background": invalid})

    def test_dialogue_background_preset_defaults_and_limits(self):
        old = normalize_dialogue_preset({"opening_background": "自定义背景"})
        self.assertEqual("custom", old["background_mode"])
        self.assertEqual("自定义背景", old["opening_background"])
        self.assertEqual(0, old["opening_scene_index"])
        self.assertTrue(old["allow_scene_transitions"])
        cleaned = normalize_dialogue_preset({
            "background_mode": " SHINSEKAI ", "opening_scene_index": "201", "allow_scene_transitions": False,
            "custom_draft": {"background_group": "组" * 161, "background_scene_index": "-2",
                             "background_scene_path": "a" * 321},
        })
        self.assertEqual("shinsekai", cleaned["background_mode"])
        self.assertEqual(200, cleaned["opening_scene_index"])
        self.assertFalse(cleaned["allow_scene_transitions"])
        self.assertEqual({"background_group": "组" * 160, "background_scene_index": 0,
                          "background_scene_path": "a" * 320}, cleaned["custom_draft"])
        self.assertEqual(0, normalize_dialogue_preset({"opening_scene_index": float("inf")})["opening_scene_index"])

    def test_builtin_opening_validates_model_selection_and_enforces_pin(self):
        write_background_config(self.root)
        model = Mock(return_value={"background_scene_index": 2, "scene_name": "虚构场景"})
        runtime = self.new_runtime(llm_fn=model)
        config = {**BUILTIN_BACKGROUND, "characters": ["秋"], "outline": "旧站月台有个人"}
        pinned = runtime.preview_dialogue_story({**config, "opening_scene_index": 1})
        system_prompt, context = model.call_args.args
        context = json.loads(context)
        self.assertEqual([{"index": 1, "label": "旧站月台：雨后"}, {"index": 2, "label": "深夜图书馆"},
                          {"index": 3, "label": "场景 3"}], context["available_backgrounds"])
        self.assertEqual(context["available_backgrounds"][0], context["pinned_scene"])
        self.assertIn("background_scene_index", system_prompt)
        self.assertEqual(1, pinned["background_scene_index"])
        chosen = runtime.preview_dialogue_story(config)
        self.assertEqual("深夜图书馆", chosen["scene_name"])
        self.assertNotIn("pinned_scene", json.loads(model.call_args.args[1]))
        model.return_value = {"background_scene_index": 99, "scene_name": "深夜图书馆"}
        self.assertEqual(2, runtime.preview_dialogue_story(config)["background_scene_index"])
        model.return_value = {"background_scene_index": 99, "scene_name": "虚构场景"}
        self.assertEqual(1, runtime.preview_dialogue_story(config)["background_scene_index"])

    def test_builtin_background_beat_transition_rules(self):
        write_background_config(self.root)
        for allow, requested, expected in ((True, 2, 2), (True, 999, 1), (True, 2.5, 1), (False, 2, 1)):
            with self.subTest(allow=allow, requested=requested):
                calls = []

                def model(prompt, raw):
                    context = json.loads(raw)
                    if "transcript" not in context:
                        return None
                    calls.append((prompt, context))
                    return {"action": "next_node", "line_ids": [context["transcript"][-1]["line_id"]],
                            "next_node": {"background_scene_index": requested, "scene_name": "深夜图书馆",
                                          "background_scene_path": "模型伪造路径"}}

                runtime = self.new_runtime(llm_fn=model)
                session = runtime.start_dialogue_story({**BUILTIN_BACKGROUND, "characters": ["秋"], "use_llm": False,
                                                        "opening_scene_index": 1, "allow_scene_transitions": allow})
                sid = session["session_id"]
                runtime.ingest_host_dialogue_turn(
                    sid, user_text="想说点什么吗？", assistant_utterances=[{"character_name": "秋", "speech": "我有个约定。"}],
                    present_characters=["秋"], use_llm=False, sync_models=True,
                )
                advanced = runtime.advance_dialogue_beat(sid, use_llm=True)
                self.assertEqual(1, len(calls))
                prompt, context = calls[0]
                if allow:
                    self.assertEqual(1, context["current_scene_index"])
                    self.assertEqual([1, 2, 3], [scene["index"] for scene in context["available_backgrounds"]])
                    self.assertIn("next_node.background_scene_index", prompt)
                else:
                    self.assertNotIn("available_backgrounds", context)
                    self.assertIn("沿用当前内置背景场景", prompt)
                node = active_dialogue_beat(advanced)[1]
                scene = self.runtime.list_host_backgrounds()[0]["scenes"][expected - 1]
                self.assertEqual(2, node["seq"])
                for field, value in {"scene_name": scene["label"], "background_group": "Night Stage",
                                     "background_scene_index": expected, "background_scene_path": scene["path"]}.items():
                    self.assertEqual(value, node[field])
                    self.assertEqual(value, advanced["scene_context"][field])

    def test_builtin_background_switch_rpc_prompt_checkpoint_and_new_chapter(self):
        write_background_config(self.root)
        self.create(characters=("秋",), max_nodes=1, custom_draft={"scene_name": "深夜图书馆"})
        register = _Register()
        story_page.register_pages(register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
                                  settings_store=self.settings)
        injector, _ = self.injector("秋")
        first = [user("要等多久？", "u1"), assistant("一会儿。")]
        self.before(injector, first[:1])
        self.added(injector, first)
        locked = register.action("set_dialogue_background", session_id=self.sid, **BUILTIN_BACKGROUND,
                                 allow_scene_transitions=False)
        self.assertTrue(locked["ok"], locked)
        self.assertEqual(2, active_dialogue_beat(locked["session"])[1]["background_scene_index"])
        checkpoint = self.session()["turn_checkpoints"][-1]["state"]
        self.assertEqual(2, active_dialogue_beat(checkpoint)[1]["background_scene_index"])
        self.assertFalse(checkpoint["scene_context"]["allow_scene_transitions"])
        block = self.before(injector, first)[0]["content"]
        self.assertTrue(block.startswith(SCRIPT_HEADER))
        self.assertIn("Shinsekai 内置背景组：Night Stage", block)
        self.assertIn("当前剧情场景对应内置背景：场景 2（深夜图书馆）", block)
        self.assertIn("在回复开头的场景项切换到场景 2", block)
        self.assertIn("不要切换到其他场景背景", block)

        allowed = register.action("set_dialogue_background", session_id=self.sid, **BUILTIN_BACKGROUND,
                                  opening_scene_index="1")
        self.assertTrue(allowed["ok"], allowed)
        self.assertTrue(allowed["session"]["dialogue_preset"]["allow_scene_transitions"])
        block = self.before(injector, first[:1])[0]["content"]
        self.assertIn("场景 1（旧站月台：雨后）", block)
        self.assertIn("只切换到当前 Shinsekai 背景组内已有的内置场景", block)
        self.assertEqual(0, self.session()["turn_count"])
        self.assertEqual(1, self.session()["scene_context"]["background_scene_index"])
        rejected = register.action("set_dialogue_background", session_id=self.sid, background_mode="shinsekai",
                                   opening_background="不存在")
        self.assertFalse(rejected["ok"], rejected)
        self.assertIn("Shinsekai 内置背景组", rejected["error"])
        self.assertEqual("Night Stage", self.session()["scene_context"]["opening_background"])

        ended = self.runtime.advance_dialogue_beat(self.sid, use_llm=False)
        self.assertIsNone(active_dialogue_beat(ended)[1])
        self.runtime.set_dialogue_background(self.sid, **BUILTIN_BACKGROUND)
        chapter = self.runtime.pluck_dialogue_thread(self.sid, prompt_hint="大家在图书馆重逢", use_llm=False)
        self.assertEqual(2, chapter["thread"]["nodes"][0]["background_scene_index"])

        custom = register.action("set_dialogue_background", session_id=self.sid, background_mode="custom")
        self.assertTrue(custom["ok"], custom)
        session = custom["session"]
        node = active_dialogue_beat(session)[1]
        self.assertEqual("深夜图书馆", node["scene_name"])
        for row in (node, session["scene_context"], session["dialogue_preset"]["custom_draft"]):
            self.assertTrue(set(BACKGROUND_SCENE_KEYS).isdisjoint(row))
        for row in (session["scene_context"], session["dialogue_preset"]):
            self.assertEqual("custom", row["background_mode"])
        self.assertNotIn("Shinsekai 内置背景组", self.before(injector, first[:1])[0]["content"])

    def test_worldless_preview_presets_actions_and_opening(self):
        self.assertFalse(self.world.available())
        submitted = []
        register = _Register()
        story_page.register_pages(register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
                                  settings_store=self.settings, host_chat_adapter=HostChatAdapter(submitted.append))
        lobby = register.action("get_lobby_state")
        self.assertTrue(lobby["ok"])
        self.assertFalse(lobby["world_available"])
        self.assertEqual({}, lobby["chat_bindings"])
        self.assertEqual([], lobby["dialogue_presets"])

        config = {"characters": ["秋", "秋"], "title": "剧本", "outline": "商议修表", "use_llm": False,
                  "recent_chat_turns": [{"role": "assistant", "text": "秋：我等了很久。"}], "max_nodes": 2}
        draft = register.action("preview_dialogue_story", **config)["draft"]
        self.assertIn("我等了很久", draft["node1"]["situation"])
        self.assertEqual(["秋"], list(draft["character_roles"]))
        draft.update(scene_name="钟表店", atmosphere="天将下雨", public_incident_fact="门还开着")
        draft["node1"]["situation"] = "玻璃上落下一滴雨"
        draft["node1"]["dilemma_frame"] = "留还是走"
        draft["character_roles"]["秋"].update(local_role="顾客", relationship_to_incident="自己的旧表")
        created = register.action("create_dialogue_story", **config, custom_draft=draft,
                                  history_path=str(self.history) + ".json", trigger_opening=True)
        self.assertTrue(created["ok"], created)
        self.assertTrue(created["opening_submitted"])
        self.assertEqual(1, len(submitted))
        session = created["session"]
        sid = session["session_id"]
        self.assertEqual("dialogue", session["mode"])
        self.assertEqual("", session["realm_space_id"])
        self.assertEqual("钟表店", session["scene_context"]["scene_name"])
        thread, node = active_dialogue_beat(session)
        self.assertEqual(2, thread["max_nodes"])
        self.assertEqual("玻璃上落下一滴雨", node["situation"])
        self.assertEqual("留还是走", node["dilemma_frame"])
        self.assertEqual("顾客", session["ledgers"]["character:秋"]["story_role"]["local_role"])
        self.assertEqual("", node["participant_choices"]["character:秋"]["inner_reason"])
        self.assertTrue(node["participant_choices"]["character:秋"]["inner_hidden"])
        self.assertEqual(sid, self.runtime.get_bound_dialogue_session(str(self.history / "active.json"))["session_id"])
        self.assertEqual(canonical_history_path(self.history), canonical_history_path(str(self.history) + ".json"))

        saved = register.action("save_dialogue_preset", session_id=sid)["preset"]
        exported = register.action("export_dialogue_preset", preset_id=saved["preset_id"])["json"]
        self.assertNotIn("linked_history_path", exported)
        imported = register.action("import_dialogue_preset", json=exported)["preset"]
        self.assertEqual("自己的旧表", imported["custom_draft"]["character_roles"]["秋"]["relationship_to_incident"])
        self.assertEqual(2, len(register.action("list_dialogue_presets")["dialogue_presets"]))
        self.assertTrue(register.action("delete_dialogue_preset", preset_id=saved["preset_id"])["ok"])
        self.assertEqual([imported], register.action("list_dialogue_presets")["dialogue_presets"])
        self.assertTrue(register.action("unbind_dialogue_session", session_id=sid)["ok"])
        self.assertIsNone(self.runtime.get_bound_dialogue_session(str(self.history)))
        self.assertTrue(register.action("bind_dialogue_session", session_id=sid, history_path=str(self.history))["ok"])
        self.assertTrue(register.action("detach_fate_session", session_id=sid)["ok"])
        self.assertEqual({}, self.runtime.list_chat_bindings())

    def test_hook_placement_private_views_native_guard_and_director_whisper(self):
        self.create()
        with patch("application.runtime.context.try_get_app_runtime", return_value=None):
            single, init = self.injector("秋")
        self.assertEqual("host-chat", single.session_id)
        self.assertEqual(str(self.history / "active.json"), single.history_file)
        init.report.assert_called_once()
        self.runtime.inject_director_whisper(self.sid, "听听窗外雨声", target="秋")
        self.runtime.inject_director_whisper(self.sid, "看看遗落的车票", target="澪")
        base = [{"role": "system", "content": "人物设定"}, {"role": "system", "content": "【此刻】心理状态"},
                user("还在等车吗？", "a")]
        projected = self.before(single, base)
        self.assertEqual(base[:2], projected[:2])
        self.assertEqual(base[-1], projected[-1])
        block = projected[2]["content"]
        self.assertTrue(block.startswith(SCRIPT_HEADER))
        self.assertIn("秋独有的信封秘密", block)
        self.assertIn("听听窗外雨声", block)
        self.assertNotIn("澪独有的钥匙秘密", block)
        self.assertNotIn("看看遗落的车票", block)
        loop = projected + [{"role": "assistant", "tool_calls": [{"id": "t1"}]}, {"role": "tool", "content": "结果"}]
        rerun = self.before(single, loop)
        self.assertEqual(1, sum(str(row.get("content")).startswith(SCRIPT_HEADER) for row in rerun))
        self.assertEqual("system", rerun[2]["role"])
        multi, _ = self.injector("秋", "澪")
        block = self.before(multi, base)[2]["content"]
        autumn_view, mio_view = block.split("【澪的视角与心事】")
        self.assertIn("别人", autumn_view)
        self.assertIn("秋独有的信封秘密", autumn_view)
        self.assertNotIn("澪独有的钥匙秘密", autumn_view)
        self.assertIn("澪独有的钥匙秘密", mio_view)
        self.assertNotIn("秋独有的信封秘密", mio_view)

        native = {"role": "system", "content": "【当前剧本进度】"}
        self.assertEqual([*base, native], self.before(multi, [*base, native]))
        self.added(multi, [*base, assistant("原生有效但不应记录")])
        self.assertEqual(0, self.session()["turn_count"])
        self.before(multi, base)
        self.history.mkdir(parents=True)
        binding_file = self.history / "story-prompt-binding.json"
        binding_file.write_text("{}", encoding="utf-8")
        self.assertEqual(base, self.before(multi, base))
        binding_file.unlink()
        self.runtime.toggle_dialogue_pause(self.sid, True)
        self.assertEqual(base, self.before(multi, projected))
        self.added(multi, [*base, assistant("暂停不应记录")])
        self.assertEqual(0, self.session()["turn_count"])
        self.runtime.toggle_dialogue_pause(self.sid, False)
        self.assertTrue(self.before(multi, base)[2]["content"].startswith(SCRIPT_HEADER))
        self.settings.update({"enabled": False})
        self.assertEqual(base, self.before(multi, base))

    def test_message_added_pacing_and_multichapter_afterglow(self):
        session = self.create(max_nodes=2)
        single, _ = self.injector("秋")
        first_user = user("你在等信吗？", "u1")
        self.before(single, [first_user])
        self.added(single, [first_user, assistant("工具对话", tool_calls=[{"id": "test"}])])
        self.added(single, [first_user, {"role": "assistant", "content": "not dialogue"}])
        self.added(single, [first_user, assistant("未在场的人", speaker="澪")])
        self.assertEqual(0, self.session()["turn_count"])

        first = [first_user, assistant("我只捡到了一张车票。")]
        self.added(single, first)
        self.added(single, first)
        current = self.session()
        thread, node = active_dialogue_beat(current)
        self.assertEqual(1, current["turn_count"])
        self.assertEqual(1, node["turns_in_node"])
        self.assertEqual(1, node["seq"])
        self.assertEqual(2, len(node["recent_dialogue_lines"]))
        self.assertIn("车票", current["ledgers"]["character:秋"]["experienced_events"][0]["summary"])
        self.assertIn("车票", current["ledgers"]["player"]["experienced_events"][0]["summary"])
        self.assertEqual([], current["ledgers"]["character:澪"]["experienced_events"])
        self.assertNotIn("不应写进记事", json.dumps(node, ensure_ascii=False))
        self.assertNotIn("turn_checkpoints", redact_fate_session_for_ui(current))
        self.assertEqual(thread["thread_id"], session["threads"][0]["thread_id"])
        self.runtime.tick_all()
        self.assertEqual(current["revision"], self.session()["revision"])

        history = first + [user("那张车票还能用吗？", "u2"), assistant("也许该等他回来。")]
        self.added(single, history)
        thread, node = active_dialogue_beat(self.session())
        self.assertEqual(2, node["seq"])
        self.assertTrue(node["participant_choices"]["character:秋"]["dialogue_intent"])
        self.assertEqual("", thread["nodes"][0]["outcome_summary"])  # 澪没有听到这些台词
        history += [user("换个场景吧", "u3"), assistant("可以到外面聊。")]
        multi, _ = self.injector("秋", "澪")
        self.added(multi, history)
        closed = self.session()["threads"][0]
        self.assertEqual("afterglow", closed["phase"])
        self.assertEqual("resolved", closed["status"])
        self.assertIn("到外面", closed["chapter_chronicle"]["epilogue"])
        self.assertIn("到外面", closed["chapter_chronicle"]["character_epilogues"]["character:秋"])
        ui_closed = redact_fate_session_for_ui(self.session())["threads"][0]
        self.assertTrue(ui_closed["nodes"][0]["participant_choices"]["character:秋"]["inner_reason"])
        new_chapter = self.runtime.pluck_thread(self.sid, use_llm=False)
        self.assertEqual(2, len(new_chapter["session"]["threads"]))
        self.assertEqual(1, len(new_chapter["session"]["chronicle_archive"]))
        self.assertEqual("秋独有的信封秘密", new_chapter["session"]["ledgers"]["character:秋"]["story_role"]["relationship_to_incident"])
        self.assertEqual(2, active_dialogue_beat(self.runtime.tick_session(
            self.sid, force_evaluate=True, sync_models=True,
        ))[1]["seq"])

    def test_model_uses_only_valid_line_ids_and_rejects_fabricated_quotes(self):
        self.create(characters=("秋",), max_nodes=3)
        with patch.object(self.runtime, "advance_dialogue_beat", side_effect=lambda *_args, **_kw: self.session()):
            for text, speech in (("问一问", "车站关门了。"), ("信在哪里？", "我没有拿那封信。")):
                self.runtime.ingest_host_dialogue_turn(
                    self.sid, user_text=text, assistant_utterances=[{"character_name": "秋", "speech": speech}],
                    present_characters=["秋"], sync_models=True,
                )
        session = self.session()
        _, node = active_dialogue_beat(session)
        ids = [row["line_id"] for row in node["recent_dialogue_lines"]]

        bad = evaluate_and_weave_dialogue_beat(session, llm_fn=lambda *_: {
            "action": "resolve", "line_ids": ["invented-id"],
            "revealed_facts": [{"line_ids": [ids[-1]], "quote": "我承认偷了信。"}],
        })
        self.assertEqual("continue_scene", bad["action"])
        self.assertEqual([], bad["revealed_facts"])
        self.assertNotIn("chapter_chronicle", bad)

        def grounded(_system, context):
            self.assertEqual(ids, [row["line_id"] for row in json.loads(context)["transcript"]])
            return {"action": "next_node", "line_ids": ids[-2:],
                    "revealed_facts": [{"line_ids": [ids[-1]], "quote": "拿那封信"}],
                    "player_stance": {"line_ids": [ids[-2]], "quote": "信在哪里？"},
                    "next_node": {"scene_name": "车站外", "situation": "门口仍能交谈", "dilemma_frame": "要不要继续问？"}}

        result = evaluate_and_weave_dialogue_beat(session, llm_fn=grounded)
        self.assertEqual("next_node", result["action"])
        self.assertIn("没有拿", result["revealed_facts"][0])
        self.assertEqual("车站外", result["next_node"]["scene_name"])
        self.assertIn("信在哪里", result["player_stance_note"])

    def test_model_opening_and_choice_keep_private_stakes_and_indirect_speech(self):
        contexts = []

        def model(_system, context):
            row = json.loads(context)
            contexts.append(row)
            if "characters" in row:
                return {"scene_name": "天台", "public_incident_fact": "天台的门开着。",
                        "node1": {"situation": "两个人靠在栏杆旁。"},
                        "character_roles": {"秋": {"local_role": "值日生",
                                                   "relationship_to_incident": "唯有秋知道的来意"}}}
            return {"stance": "confide", "choice_summary": "等气氛平静下来",
                    "inner_reason": "仍在斟酌怎么开口", "speech_seed": "「我必须告诉你答案！」",
                    "dialogue_intent": {"kind": "confide", "hint": "「我必须告诉你答案！」"},
                    "arc_delta": {"promises": ["捏造的承诺"], "aspirations": ["愿意多听一会"]}}

        runtime = self.new_runtime(llm_fn=model)
        session = runtime.start_dialogue_story({"title": "放学后", "characters": ["秋", "澪"], "use_llm": True})
        thread, node = active_dialogue_beat(session)
        self.assertEqual("天台", node["scene_name"])
        self.assertEqual("天台的门开着。", thread["public_incident_fact"])
        self.assertEqual(3, len(contexts))
        night, other = contexts[1:]
        self.assertIn("唯有秋知道", night["story_role"]["relationship_to_incident"])
        self.assertNotIn("唯有秋知道", json.dumps(other, ensure_ascii=False))
        choice = node["participant_choices"]["character:秋"]
        self.assertEqual("confide", choice["dialogue_intent"]["kind"])
        self.assertNotIn("我必须告诉你答案", json.dumps(choice, ensure_ascii=False))
        self.assertEqual([], session["ledgers"]["character:秋"]["arc_state"]["promises"])

    def test_revert_and_fork_restore_matching_checkpoint(self):
        self.create(characters=("秋",), max_nodes=3)
        injector, _ = self.injector("秋")
        first = [user("我们在哪？", "u1"), assistant("在月台。")]
        second = [*first, user("我看见信了。", "u2"), assistant("那封信在我的包里。")]
        self.before(injector, first[:1])
        self.added(injector, first)
        self.added(injector, second)
        self.assertEqual(2, self.session()["turn_count"])
        self.assertEqual(2, active_dialogue_beat(self.session())[1]["seq"])

        projected = self.before(injector, second[:-1])
        self.assertEqual(1, self.session()["turn_count"])
        self.assertNotIn("那封信在我的包里", projected[0]["content"])
        alternate = [*first, user("我看见信了。", "u2-fork"), assistant("我没有带那封信。")]
        self.added(injector, alternate)
        self.assertEqual(2, self.session()["turn_count"])
        self.before(injector, alternate)
        events = self.session()["ledgers"]["character:秋"]["experienced_events"]
        self.assertIn("我没有带那封信", events[-1]["summary"])
        self.before(injector, second)
        events = self.session()["ledgers"]["character:秋"]["experienced_events"]
        self.assertIn("那封信在我的包里", events[-1]["summary"])
        self.assertNotIn("我没有带那封信", events[-1]["summary"])
        self.assertEqual(2, self.session()["turn_count"])
        self.assertEqual(3, len(self.session()["turn_checkpoints"]))

    def test_async_model_turn_commits_before_model_and_cannot_overwrite_rollback(self):
        self.create(characters=("秋",), max_nodes=3)
        injector, _ = self.injector("秋", sync_models=False)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def model(_system, raw):
            context = json.loads(raw)
            if "transcript" in context:
                started.set()
                release.wait(5)
                return {"action": "next_node", "line_ids": [context["transcript"][-1]["line_id"]]}
            return None

        self.settings.update({"use_llm_compiler": True})
        self.runtime._llm_fn = model
        first = [user("还在等人？", "x1"), assistant("是的。")]
        self.before(injector, first[:1])
        self.added(injector, first)
        self.added(injector, [*first, user("谁？", "x2"), assistant("我的朋友。")])
        self.assertTrue(started.wait(3))
        self.assertEqual(2, self.session()["turn_count"])
        self.assertEqual(1, active_dialogue_beat(self.session())[1]["seq"])
        self.before(injector, first)
        release.set()
        self.assertTrue(self.runtime.wait_for_idle())
        self.assertEqual(1, self.session()["turn_count"])
        self.assertEqual(1, active_dialogue_beat(self.session())[1]["seq"])

    def test_compaction_uses_the_host_journal_and_checkpoints_are_bounded(self):
        self.create(characters=("秋",), max_nodes=20)
        injector, _ = self.injector("秋")
        history = []
        for turn in range(1, 27):
            history += [user(f"问题 {turn}", str(turn)), assistant(f"回答 {turn}")]
            self.added(injector, history)
        self.assertEqual(26, self.session()["turn_count"])
        self.assertEqual(24, len(self.session()["turn_checkpoints"]))
        self.assertEqual(3, self.session()["turn_checkpoints"][0]["user_turn_count"])

        self.history.mkdir(parents=True)
        pending = user("下一轮", "27")
        (self.history / "active.json").write_text(json.dumps(history), encoding="utf-8")
        (self.history / "active.json.tmp").write_text(json.dumps(pending) + ",\n", encoding="utf-8")
        summary = user("【历史对话总结】历史被压缩")
        self.before(injector, [summary, *history[-2:], pending])
        self.assertEqual(26, self.session()["turn_count"])
        self.added(injector, [summary, *history[-2:], pending, assistant("我听见了。")])
        self.assertEqual(27, self.session()["turn_count"])
        self.assertEqual(27, self.session()["turn_checkpoints"][-1]["user_turn_count"])

    def test_process_roles_hot_reload_lazy_history_and_bridge_writeback(self):
        def start_plugin(owner, launch):
            register = _Register()
            plugin = NanamiStoryPlugin()
            self.addCleanup(plugin.shutdown)
            values = {"SHINSEKAI_MEMORY_SERVICE_OWNER": owner}
            if launch:
                values["SHINSEKAI_CHAT_LAUNCH_CONFIG"] = "{}"
            with patch.dict(os.environ, values):
                if not launch:
                    os.environ.pop("SHINSEKAI_CHAT_LAUNCH_CONFIG", None)
                plugin.initialize(register, self.root, SimpleNamespace(project_data_dir=self.root / ("plugin-" + owner)))
            return plugin, register

        chat_plugin, register = start_plugin("0", True)
        self.assertEqual(35, chat_plugin.priority)
        self.assertIsNone(chat_plugin._poll_thread)
        self.assertEqual({"before", "added", "init"}, set(register.hooks))
        self.assertIsInstance(register.dialog_handler, StoryMouthDialogHandler)
        conflicting, _ = start_plugin("1", True)
        self.assertIsNone(conflicting._poll_thread)
        owner, _ = start_plugin("1", False)
        self.assertTrue(owner._poll_thread.is_alive())
        self.assertEqual("nanami-story-fate-weaver", owner._poll_thread.name)

        chat = self.new_runtime()
        self.assertEqual([], chat.restore_sessions(restore_world=False))
        initial = self.create(characters=("秋",))
        self.assertEqual(self.sid, chat.list_sessions()[0]["session_id"])
        newer = self.runtime.inject_director_whisper(self.sid, "看一眼远处的灯")
        self.assertGreater(newer["revision"], initial["revision"])
        self.assertEqual(newer["revision"], chat.get_session(self.sid)["revision"])
        register = _Register()
        story_page.register_pages(register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
                                  settings_store=self.settings)
        requests = []

        def bridge(request, *, timeout):
            self.assertLess(timeout, 1)
            self.assertEqual("bridge-secret", request.get_header("X-shinsekai-bridge-token"))
            endpoint, action = request.full_url.rsplit("/", 1)
            self.assertEqual("http://127.0.0.1:12345/api/plugins/community.nanami.story/ui/story_lobby/actions", endpoint)
            requests.append(action)
            response = register.action(action, **json.loads(request.data)["values"])
            return io.BytesIO(json.dumps({"result": response}).encode())

        manager = SimpleNamespace(llm_manager=SimpleNamespace(_history_file=str(self.history / "active.json")))
        with patch.dict(os.environ, {"SHINSEKAI_CHAT_LAUNCH_CONFIG": "{}",
                                    "SHINSEKAI_MEMORY_SERVICE_URL": "http://127.0.0.1:12345/api/memory",
                                    "SHINSEKAI_MEMORY_SERVICE_TOKEN": "bridge-secret"}), patch(
            "plugins.nanami_story.dialogue_injector.urlopen", side_effect=bridge,
        ), patch("application.runtime.context.try_get_app_runtime", return_value=None) as app_runtime:
            injector, init = self.injector("秋", runtime=chat)
            init.report.assert_not_called()
            app_runtime.return_value = manager
            first = [user("看到了吗？", "r1"), assistant("看到了。")]
            self.assertIn("看一眼远处的灯", self.before(injector, first[:1])[0]["content"])
            with patch.object(chat, "ingest_host_dialogue_turn", side_effect=AssertionError("local write")):
                self.added(injector, first)
            self.assertEqual(1, self.session()["turn_count"])
        self.assertEqual(["sync_dialogue_checkpoint", "ingest_dialogue_turn"], requests)

        with patch.dict(os.environ, {"SHINSEKAI_MEMORY_SERVICE_URL": "http://127.0.0.1:12345/api/memory"}), patch(
            "plugins.nanami_story.dialogue_injector.urlopen", side_effect=OSError("Bridge unavailable"),
        ):
            self.added(injector, [*first, user("过去看看吧。", "r2"), assistant("我也想过去。")])
        self.assertEqual(2, self.session()["turn_count"])
        register.action("sync_dialogue_checkpoint", session_id=self.sid, history_path=str(self.history),
                        user_history_prefixes=["0" * 64], expected_history_cursor={})
        self.assertEqual(2, self.session()["turn_count"])

    def test_dialogue_observer_mode_prompt_ledger_choices_and_revert(self):
        session = self.create(characters=("秋", "澪"), experience_mode="observer", observer_interval_sec=12)
        self.assertEqual("observer", session["experience_mode"])
        self.assertEqual("", session["player_actor_id"])
        self.assertNotIn("player", session["ledgers"])
        self.assertEqual("observer", session["dialogue_preset"]["experience_mode"])
        self.assertEqual(12.0, session["dialogue_preset"]["observer_interval_sec"])

        preset = self.runtime.dialogue_presets.save(session["dialogue_preset"])
        loaded = self.runtime.dialogue_presets.list()[0]
        self.assertEqual(preset["preset_id"], loaded["preset_id"])
        self.assertEqual("observer", loaded["experience_mode"])
        self.assertEqual(12.0, loaded["observer_interval_sec"])

        injector, _ = self.injector("秋", "澪")
        injected = self.before(injector, [user("（剧目开场，在场角色自然展开互动）", "o1")])
        prompt = injected[0]["content"]
        self.assertIn("【观剧模式】", prompt)
        self.assertIn("第四面墙外静默观剧", prompt)
        self.assertIn("严禁向「你」或观众搭话", prompt)
        self.assertIn("[舞台此刻]旧站月台", injected[-1]["content"])
        self.assertIn("候车室只剩一盏灯", injected[-1]["content"])

        first_turn = [
            user("（剧目开场，在场角色自然展开互动）", "o1"),
            assistant("车已经晚点了十分钟，你一直看表是怕误事？", speaker="秋"),
        ]
        self.added(injector, first_turn)
        after_t1 = self.session()
        self.assertEqual(1, after_t1["turn_count"])
        node1 = after_t1["threads"][0]["nodes"][0]
        self.assertTrue(all(row["actor_id"] != "player" for row in node1["recent_dialogue_lines"]))
        self.assertFalse(any("剧目开场" in ev["summary"] for ev in after_t1["ledgers"]["character:秋"]["experienced_events"]))

        second_turn = [
            *first_turn,
            user("（剧情继续）", "o2"),
            assistant("我只是确认值班表，雨停了你就能走。", speaker="澪"),
        ]
        self.added(injector, second_turn)
        after_t2 = self.session()
        self.assertEqual(2, after_t2["turn_count"])
        self.assertEqual(2, len(after_t2["threads"][0]["nodes"]))
        self.assertIsNone(after_t2["threads"][0]["nodes"][0].get("player_stance"))

        # Revert back to turn 1 retains observer mode without player ledger.
        self.before(injector, [*first_turn, user("（剧情继续）", "o2-alt")])
        reverted = self.session()
        self.assertEqual(1, reverted["turn_count"])
        self.assertEqual("observer", reverted["experience_mode"])
        self.assertNotIn("player", reverted["ledgers"])

        # Solo observer cast switches prompt to monologue framing and supports live mode toggle.
        solo = self.runtime.start_dialogue_story({
            "title": "独角夜谈", "characters": ["秋"], "experience_mode": "observer", "use_llm": False,
        })
        solo_prompt = self.runtime._sessions[solo["session_id"]]
        self.assertIn("独角戏观剧", _project_dialogue(solo_prompt, ["秋"]))

        co = self.runtime.set_dialogue_experience_mode(self.sid, "co_play")
        self.assertEqual("co_play", co["experience_mode"])
        self.assertEqual("player", co["player_actor_id"])
        self.assertIn("player", co["ledgers"])
        back = self.runtime.set_dialogue_experience_mode(self.sid, "observer")
        self.assertEqual("observer", back["experience_mode"])
        self.assertNotIn("player", back["ledgers"])

    def test_observer_stimulus_uses_spoken_lines_and_preserves_history(self):
        self.create(experience_mode="observer", observer_autoplay=False)
        self.settings.update({"use_llm_compiler": True})
        model = Mock(return_value={"candidates": ["灯还亮着，进去看看。"]})
        mouth = DialogueMouthRewriter(self.persona, self.settings.get, llm_fn=model)
        injector, _ = self.injector("秋", "澪", mouth_rewriter=mouth)
        first = [user("（剧目开场）", "host-1"), assistant("先进去察看灯的状况。")]
        self.history.mkdir(parents=True, exist_ok=True)
        history_path = self.history / "active.json"
        history_path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
        saved = history_path.read_bytes()
        with patch.object(self.runtime, "advance_dialogue_beat", return_value={}):
            self.added(injector, first)
        self.assertEqual(1, model.call_count)
        _, node = active_dialogue_beat(self.session())
        self.assertEqual("灯还亮着，进去看看。", node["recent_dialogue_lines"][-1]["speech"])
        cursor = self.session()["dialogue_history_cursor"]
        meta = '[本地时间 2026-11-26 22:35:00]\n[当前显示背景]\n{"scene":"旧站月台"}\n[/当前显示背景]'
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,fake"}}
        second_user = user([{"type": "text", "text": meta}, image, {"type": "text", "text": "（剧情继续）"}], "host-2")
        rows = [*first, second_user]
        prefixes = _user_prefixes(rows, completed_only=False)
        prompt_rows = self.before(injector, copy.deepcopy(rows))
        self.assertIn("灯还亮着，进去看看。", prompt_rows[-2]["content"])
        self.assertNotIn("先进去察看灯的状况", prompt_rows[-2]["content"])
        self.assertNotIn("你亲耳听到的近事", prompt_rows[0]["content"])
        self.assertIn("【此刻】心理卡用于角色对台上同伴", prompt_rows[0]["content"])
        self.assertIn("复述对方刚说过的词", prompt_rows[0]["content"])
        self.assertEqual(meta + "\n[舞台上一拍]秋：灯还亮着，进去看看。\n（剧情继续）",
                         _user_text(prompt_rows[-1]["content"]))
        self.assertEqual(image, prompt_rows[-1]["content"][1])
        self.assertEqual("（剧情继续）", second_user["content"][-1]["text"])
        self.assertEqual(prefixes, _user_prefixes(prompt_rows, completed_only=False))
        self.assertEqual(prompt_rows, self.before(injector, copy.deepcopy(prompt_rows)))
        self.assertEqual(prompt_rows, self.before(injector, copy.deepcopy(rows)))
        self.assertEqual(cursor, self.session()["dialogue_history_cursor"])
        self.assertEqual(saved, history_path.read_bytes())
        self.assertEqual(1, model.call_count)

        actual_user = user(meta + "\n我想走了。", "host-3")
        self.assertEqual(actual_user, self.before(injector, [*first, actual_user])[-1])
        for empty in ("", meta):
            original_cue = [{**user(empty, "another"), "display_content": ""}]
            opening = self.before(injector, original_cue)
            self.assertIn("[舞台此刻]旧站月台", opening[-1]["content"])
            self.assertTrue(opening[-1]["content"].endswith("（剧目开场）"))
            self.assertEqual(_user_prefixes(original_cue, completed_only=False),
                             _user_prefixes(opening, completed_only=False))

    def test_dialogue_prompt_shares_only_latest_revealed_facts_and_deduplicates_hints(self):
        session = self.create(experience_mode="observer")
        _, node = active_dialogue_beat(session)
        node["revealed_facts"] = ["旧事实", "事实二", "事实三", "事实四", "事实五"]
        node["participant_choices"]["character:秋"]["dialogue_intent"]["hint"] = "打听信封的去向。"
        node["participant_choices"]["character:秋"]["speech_seed"] = " 打听信封的去向 "
        session["ledgers"]["character:秋"]["experienced_events"].append({"summary": "冗长的旧对白"})
        prompt = _project_dialogue(session, ["秋", "澪"])
        self.assertIn("已揭晓事实：事实二 / 事实三 / 事实四 / 事实五", prompt)
        self.assertEqual(1, prompt.count("已揭晓事实"))
        self.assertEqual(1, prompt.count("打听信封的去向"))
        self.assertNotIn("旧事实", prompt)
        self.assertNotIn("冗长的旧对白", prompt)

    def test_dialogue_observer_step_and_autoplay_lifecycle(self):
        submitted = []
        adapter = HostChatAdapter(submitted.append)
        register = _Register()
        story_page.register_pages(
            register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
            settings_store=self.settings, host_chat_adapter=adapter,
        )
        self.create(characters=("秋", "澪"), experience_mode="observer", observer_interval_sec=5, observer_autoplay=False)

        # Manual observer step dispatches opening cue when turn_count == 0, then continue cue later.
        step1 = register.action("step_dialogue_observer", session_id=self.sid, history_path=str(self.history))
        self.assertTrue(step1["ok"])
        self.assertEqual(1, len(submitted))
        self.assertIn("剧目开场", submitted[0])

        injector, _ = self.injector("秋", "澪")
        t1 = [user(submitted[0], "s1"), assistant("雨好像要停了。", speaker="秋")]
        self.added(injector, t1)
        self.assertEqual(1, self.session()["turn_count"])

        # Turn on autoplay at 5s interval with trigger_now=True.
        res = register.action(
            "toggle_dialogue_autoplay", session_id=self.sid, enabled=True,
            observer_interval_sec=5, trigger_now=True, history_path=str(self.history),
        )
        self.assertTrue(res["session"]["observer_autoplay"])
        self.assertEqual(2, len(submitted))
        self.assertEqual("（剧情继续）", submitted[-1])

        # Does not double-fire before model reply arrives, even if time advances < timeout.
        t0 = self.runtime._observer_autoplay_state[self.sid]["last_dispatched_at"]
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=t0 + 20.0))
        self.assertEqual(2, len(submitted))

        # Reply arrives -> pending clears; waits for interval before dispatching again.
        t2 = [*t1, user(submitted[-1], "s2"), assistant("嗯，快结束了。", speaker="澪")]
        self.added(injector, t2)
        last_turn_at = self.runtime._observer_autoplay_state[self.sid]["last_turn_at"]
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=last_turn_at + 2.0))
        self.assertEqual([self.sid], self.runtime.tick_observer_autoplay(adapter, now=last_turn_at + 6.0))
        self.assertEqual(3, len(submitted))

        # Switching active chat automatically halts observer autoplay.
        register.action("report_active_chat", history_path=str(self.root / "history" / "other"))
        halted = self.session()
        self.assertFalse(halted["observer_autoplay"])
        self.assertIn("切换对话", halted["observer_status_note"])

        # Re-enable and hit pause / afterglow / restore reset.
        register.action("toggle_dialogue_autoplay", session_id=self.sid, enabled=True, observer_interval_sec=4,
                        history_path=str(self.history))
        self.assertTrue(self.session()["observer_autoplay"])
        restarted = self.new_runtime()
        restarted.restore_sessions(restore_world=True)
        self.assertFalse(restarted.get_session(self.sid)["observer_autoplay"])

        # Reaching thread resolution (afterglow) stops autoplay cleanly.
        self.runtime.set_dialogue_observer_autoplay(self.sid, enabled=True, interval_sec=4, history_path=str(self.history))
        t3 = [*t2, user("（剧情继续）", "s3"), assistant("那我们各自保重。", speaker="秋")]
        self.added(injector, t3)
        self.runtime.advance_dialogue_beat(self.sid, force_evaluate=True, use_llm=False, sync_models=True)
        self.assertEqual("active", self.session()["threads"][0]["status"])
        self.assertTrue(self.session()["observer_autoplay"])
        self.runtime.advance_dialogue_beat(self.sid, force_evaluate=True, use_llm=False, sync_models=True)
        self.assertEqual("resolved", self.session()["threads"][0]["status"])
        self.assertFalse(self.session()["observer_autoplay"])
        self.assertIn("落幕", self.session()["observer_status_note"])

    def test_init_chat_recovers_history_from_bridge_snapshot_and_triggers_observer_opening(self):
        submitted = []
        adapter = HostChatAdapter(submitted.append)
        register = _Register()
        story_page.register_pages(
            register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
            settings_store=self.settings, host_chat_adapter=adapter,
        )
        self.create(characters=("秋", "澪"), experience_mode="observer", observer_autoplay=False)

        calls = []

        def fake_bridge(request, *, timeout):
            self.assertLess(timeout, 1)
            self.assertEqual("bridge-secret", request.get_header("X-shinsekai-bridge-token"))
            url = request.full_url
            if url.endswith("/api/chat/snapshot"):
                calls.append(("GET", "/api/chat/snapshot"))
                return io.BytesIO(json.dumps({
                    "historyPath": str(self.history),
                    "sessionId": "bridge-chat-sid",
                }).encode("utf-8"))
            endpoint, action = url.rsplit("/", 1)
            self.assertEqual("http://127.0.0.1:12345/api/plugins/community.nanami.story/ui/story_lobby/actions", endpoint)
            values = json.loads(request.data)["values"]
            calls.append(("POST", action, values))
            response = register.action(action, **values)
            return io.BytesIO(json.dumps({"result": response}).encode("utf-8"))

        env_without_launch_config = {k: v for k, v in os.environ.items() if k != "SHINSEKAI_CHAT_LAUNCH_CONFIG"}
        env_without_launch_config.update({
            "SHINSEKAI_MEMORY_SERVICE_OWNER": "0",
            "SHINSEKAI_MEMORY_SERVICE_URL": "http://127.0.0.1:12345/api/memory",
            "SHINSEKAI_MEMORY_SERVICE_TOKEN": "bridge-secret",
        })
        injector = DialogueInjector(self.runtime, self.settings.get, sync_models=True)
        init = SimpleNamespace(character_names=("秋", "澪"), session_id="", report=Mock())
        with patch.dict(os.environ, env_without_launch_config, clear=True), patch(
            "application.runtime.context.try_get_app_runtime", return_value=None,
        ), patch("sys.argv", ["main.py"]), patch(
            "plugins.nanami_story.dialogue_injector.urlopen", side_effect=fake_bridge,
        ), patch.object(injector, "_start_ready_reporter") as watcher:
            injector.init_chat(init)

        self.assertEqual(str(self.history), injector.history_file)
        self.assertEqual("bridge-chat-sid", injector.session_id)
        init.report.assert_called_once()
        self.assertIn("希望" if False else "末班列车", init.report.call_args.kwargs["message"])
        watcher.assert_called_once_with(str(self.history))
        self.assertEqual(("GET", "/api/chat/snapshot"), calls[0])
        self.assertEqual(("POST", "report_active_chat", {"history_path": canonical_history_path(self.history)}), calls[1])

        # Ready signal triggers exactly one opening cue via tick_observer_autoplay when observer turn_count == 0.
        register.action("report_active_chat", history_path=str(self.history), ready=True)
        self.assertEqual([self.sid], self.runtime.tick_observer_autoplay(adapter, now=100.0))
        self.assertEqual(1, len(submitted))
        self.assertIn("剧目开场", submitted[0])
        # Second tick must not double-fire while waiting for reply, and another ready ping must not duplicate it.
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=105.0))
        register.action("report_active_chat", history_path=str(self.history), ready=True)
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=106.0))
        self.assertEqual(1, len(submitted))

        # Guard cases: paused, co_play, or turn_count > 0 must never auto-dispatch an opening cue.
        register.action("toggle_dialogue_pause", session_id=self.sid, paused=True)
        register.action("report_active_chat", history_path=str(self.history), ready=True)
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=110.0))
        register.action("toggle_dialogue_pause", session_id=self.sid, paused=False)

        self.runtime.set_dialogue_experience_mode(self.sid, "co_play")
        register.action("report_active_chat", history_path=str(self.history), ready=True)
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=120.0))
        self.runtime.set_dialogue_experience_mode(self.sid, "observer")

        t1 = [user(submitted[0], "u1"), assistant("我们开始吧。", speaker="秋")]
        self.added(injector, t1)
        register.action("report_active_chat", history_path=str(self.history), ready=True)
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=130.0))
        self.assertEqual(1, len(submitted))

    def test_observer_autoplay_preference_persists_across_restart_and_advance_cue(self):
        preset = {"experience_mode": "observer"}
        self.assertTrue(normalize_dialogue_preset(preset)["observer_autoplay"])
        self.assertTrue(normalize_dialogue_preset({**preset, "observer_autoplay": None})["observer_autoplay"])
        self.assertFalse(normalize_dialogue_preset({**preset, "observer_autoplay": False})["observer_autoplay"])
        self.assertFalse(normalize_dialogue_preset({"experience_mode": "co_play", "observer_autoplay": True})[
            "observer_autoplay"])

        created = self.create(experience_mode="observer", observer_autoplay=True, observer_interval_sec=5, max_nodes=8)
        self.assertTrue(created["observer_autoplay"])
        self.assertTrue(created["observer_autoplay_preferred"])
        self.assertTrue(created["dialogue_preset"]["observer_autoplay"])
        injector, _ = self.injector("秋", "澪")
        first_turn = [user("（剧目开场）", "a1"), assistant("广播刚停。")]
        self.added(injector, first_turn)
        self.assertEqual(1, self.session()["turn_count"])

        self.runtime = self.new_runtime()
        self.runtime.restore_sessions()
        self.assertFalse(self.session()["observer_autoplay"])
        self.assertTrue(self.session()["observer_autoplay_preferred"])
        self.assertTrue(self.session()["dialogue_preset"]["observer_autoplay"])

        submitted = []
        adapter = HostChatAdapter(submitted.append)
        register = _Register()
        story_page.register_pages(
            register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
            settings_store=self.settings, host_chat_adapter=adapter,
        )
        history_path = str(self.history)
        self.runtime.report_active_chat(history_path, ready=True)
        self.assertTrue(self.session()["observer_autoplay"])
        self.assertEqual([self.sid], self.runtime.tick_observer_autoplay(adapter))
        self.assertEqual(["（剧情继续）"], submitted)

        pending = dict(self.runtime._observer_autoplay_state[self.sid])
        self.runtime.report_active_chat(history_path, ready=True)
        self.assertEqual(pending, self.runtime._observer_autoplay_state[self.sid])
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter))

        injector, _ = self.injector("秋", "澪")
        self.added(injector, [*first_turn, user(submitted[-1], "a2"), assistant("去月台看看。", speaker="澪")])
        last_turn_at = self.runtime._observer_autoplay_state[self.sid]["last_turn_at"]
        closed_adapter = HostChatAdapter(Mock(return_value=False))
        for attempt in range(1, OBSERVER_MAX_SUBMIT_FAILURES + 1):
            self.assertEqual([], self.runtime.tick_observer_autoplay(closed_adapter, now=last_turn_at + 6 * attempt))
        self.assertFalse(self.session()["observer_autoplay"])
        self.assertTrue(self.session()["observer_autoplay_preferred"])
        self.assertIn("重新打开对话后将自动继续", self.session()["observer_status_note"])

        self.runtime.report_active_chat(history_path, ready=True)
        self.assertTrue(self.session()["observer_autoplay"])
        self.assertEqual("", self.session()["observer_status_note"])
        self.assertEqual([self.sid], self.runtime.tick_observer_autoplay(adapter))
        self.assertEqual(["（剧情继续）"] * 2, submitted)

        stopped = register.action("toggle_dialogue_autoplay", session_id=self.sid, enabled=False, history_path=history_path)
        self.assertTrue(stopped["ok"])
        self.assertFalse(stopped["session"]["observer_autoplay_preferred"])
        self.assertFalse(stopped["session"]["dialogue_preset"]["observer_autoplay"])
        self.runtime.restore_sessions()
        self.runtime.report_active_chat(history_path, ready=True)
        self.assertFalse(self.session()["observer_autoplay"])
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter))
        self.assertEqual(2, len(submitted))

        previous_seq = active_dialogue_beat(self.session())[1]["seq"]
        advanced = register.action(
            "advance_dialogue_beat", session_id=self.sid, history_path=history_path,
            trigger_observer_cue=True, use_llm=False,
        )
        self.assertTrue(advanced["ok"])
        self.assertTrue(advanced["cue_submitted"])
        self.assertEqual(previous_seq + 1, active_dialogue_beat(self.session())[1]["seq"])
        self.assertEqual(["（剧情继续）"] * 3, submitted)
        self.assertEqual(self.session()["turn_count"], self.runtime._observer_autoplay_state[self.sid]["pending_turn"])

        with patch.object(adapter, "submit_text", return_value=False):
            refused = register.action(
                "advance_dialogue_beat", session_id=self.sid, history_path=history_path,
                trigger_observer_cue=True, use_llm=False,
            )
        self.assertTrue(refused["ok"])
        self.assertFalse(refused["cue_submitted"])
        self.assertIn("后台剧情已推进", refused["cue_error"])
        self.assertEqual(previous_seq + 2, active_dialogue_beat(self.session())[1]["seq"])

    def test_observer_autoplay_unlimited_and_explicit_cap(self):
        self.assertEqual(0, OBSERVER_DEFAULT_MAX_AUTO_TURNS)
        self.create(experience_mode="observer", observer_autoplay=True, max_nodes=8)
        injector, _ = self.injector("秋", "澪")
        submitted = []
        rows = []
        adapter = HostChatAdapter(submitted.append)
        for cap, count in ((None, 30), (3, 3)):
            with self.subTest(cap=cap), patch.object(self.runtime, "advance_dialogue_beat", return_value={}):
                self.runtime.set_dialogue_observer_autoplay(self.sid, enabled=True, interval_sec=5, max_auto_turns=cap)
                tracker = self.runtime._observer_autoplay_state[self.sid]
                self.assertEqual(cap or 0, tracker["max_auto_turns"])
                for _ in range(count):
                    self.assertEqual([self.sid], self.runtime.tick_observer_autoplay(
                        adapter, now=tracker["last_turn_at"] + 6,
                    ))
                    rows.extend([user(submitted[-1], f"turn-{len(submitted)}"), assistant("灯还亮着。")])
                    self.added(injector, rows)
                    self.assertIsNone(tracker["pending_turn"])
                self.assertEqual(count, tracker["auto_turns"])
                if cap is None:
                    self.assertTrue(self.session()["observer_autoplay"])
                else:
                    self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=tracker["last_turn_at"] + 6))
                    self.assertFalse(self.session()["observer_autoplay"])
                    self.assertIn("已连续自动续演 3 轮", self.session()["observer_status_note"])
        self.assertEqual(33, self.session()["turn_count"])
        self.runtime.report_active_chat(str(self.history), ready=True)
        self.assertEqual("", self.session()["observer_status_note"])
        self.assertEqual(0, self.runtime._observer_autoplay_state[self.sid]["max_auto_turns"])

    def test_observer_autoplay_retries_at_interval_and_resets_on_turn_ingest(self):
        self.create(experience_mode="observer", observer_autoplay=False)
        self.runtime.set_dialogue_observer_autoplay(self.sid, enabled=True, interval_sec=5)
        injector, _ = self.injector("秋", "澪")
        submit = Mock(side_effect=[False, False, True, False, False, False])
        adapter = HostChatAdapter(submit)
        tracker = self.runtime._observer_autoplay_state[self.sid]
        start = tracker["last_turn_at"]
        for failure in (1, 2):
            now = start + 6 * failure
            self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=now))
            self.assertTrue(self.session()["observer_autoplay"])
            self.assertEqual(failure, tracker["submit_failures"])
            self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=now + 2))
            self.assertEqual(failure, submit.call_count)
        self.assertEqual([self.sid], self.runtime.tick_observer_autoplay(adapter, now=start + 18))
        self.assertEqual(0, tracker["submit_failures"])
        self.assertEqual(180.0, OBSERVER_PENDING_TIMEOUT_SEC)
        self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=start + 18 + 150))
        self.assertTrue(self.session()["observer_autoplay"])
        tracker["submit_failures"] = 2
        self.added(injector, [user("（剧目开场）", "retry"), assistant("钟停了。")])
        self.assertEqual(0, tracker["submit_failures"])
        for _ in range(OBSERVER_MAX_SUBMIT_FAILURES):
            self.assertEqual([], self.runtime.tick_observer_autoplay(adapter, now=tracker["last_turn_at"] + 6))
        self.assertFalse(self.session()["observer_autoplay"])
        self.assertIn("当前对话连续多次未响应，自动观剧已暂停", self.session()["observer_status_note"])

    def test_mouth_settings_default_and_persistence(self):
        self.assertTrue(StorySettings().dialogue_mouth_decoupling)
        self.assertTrue(StorySettings.from_mapping({"enabled": True}).dialogue_mouth_decoupling)
        register = _Register()
        story_page.register_pages(register, code_dir=Path(story_page.__file__).parent, fate_runtime=self.runtime,
                                  settings_store=self.settings)
        self.assertTrue(register.action("get_lobby_state")["settings"]["dialogue_mouth_decoupling"])
        result = register.action("set_dialogue_mouth", dialogue_mouth_decoupling=False)
        self.assertFalse(result["settings"]["dialogue_mouth_decoupling"])
        self.assertFalse(SettingsStore(self.settings.path).get().dialogue_mouth_decoupling)
        self.assertFalse(register.action("get_lobby_state")["settings"]["dialogue_mouth_decoupling"])
        self.assertFalse(register.action("set_dialogue_mouth", dialogue_mouth_decoupling="off")["ok"])
        register.pages["story_settings"].save_values({"dialogue_mouth_decoupling": True})
        self.assertTrue(register.pages["story_settings"].load_values()["dialogue_mouth_decoupling"])

    def test_story_html_launch_dialogue_binds_real_snapshot_path(self):
        html_path = Path(story_page.__file__).parent / "web" / "story.html"
        html = html_path.read_text(encoding="utf-8")
        self.assertNotIn("nanami_story_", html)
        self.assertIn("waitForChatTask", html)
        self.assertIn("snapshot?.historyPath", html)
        self.assertIn('"bind_dialogue_session"', html)
        self.assertIn("openRebindModal", html)


if __name__ == "__main__":
    unittest.main()
