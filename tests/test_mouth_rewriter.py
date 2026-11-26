"""Tests for story voice references, isolated realization and live media dispatch."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _shinsekai  # noqa: F401
from core.chat_history.identity import DialogIdentities
from sdk.messages import LLMDialogMessage

from plugins.nanami_persona.plugin import NanamiDialogHandler
from plugins.nanami_story.adapters.llm_adapter import ask_llm_json
from plugins.nanami_story.adapters.persona_adapter import PersonaAdapter
from plugins.nanami_story.mouth_rewriter import (
    DialogueMouthRewriter, StoryMouthDialogHandler, _run_guard_bridge, score_mouth_candidate,
)
from plugins.nanami_story.settings import StorySettings

MODULE = "plugins.nanami_story.mouth_rewriter"
HANDLERS = "application.chat.presentation.handlers.dialog_media.get_dialog_media_handlers"
PLUGIN_HANDLERS = "plugin_system.host.get_plugin_dialog_media_handlers"


class PersonaMouthContextTest(unittest.TestCase):
    def test_read_only_voice_sources_use_own_corpus_then_bound_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "data/nanami"
            profile = base / "profiles/akino"
            state = profile / "runtime/state"
            toolkit = root / "plugins/nanami_persona/toolkit"
            entries = {
                base / "pointers.yaml": "pointers:\n- id: akino\n  character_name: 秋\n  profile_dir: profiles/akino\n",
                root / "data/config/characters.yaml": "- name: 秋\n  setting: 宿主声线\n- name: 澪\n  setting: 低声而爽快\n",
                profile / "soul.md": "沉着而爽快" * 500,
                profile / "corpus.tsv": (
                    "# source\nannoyed\t怒问\t怒答\ncasual\t闲问\t闲答\n"
                    "curious\t奇问\t奇答\nrepair_afterglow\t和问\t和答\n"
                ),
                base / "corpus/acg_corpus.tsv": "casual\t通用问\t通用答\n",
                profile / "off-voice.json": json.dumps({"ban": ["换个话题"], "patterns": ["^模板"]}, ensure_ascii=False),
                state / "mind.json": json.dumps({
                    "recent": {"updatedAt": 17, "energy": 0.75, "moodV": 0.4, "moodA": 0.3,
                               "agenda": "want_company", "attitude": {"attitudeStance": "warm"}},
                    "stale": {"updatedAt": 10, "energy": 0.35},
                }),
                toolkit.parent / "guard_bridge.mjs": "// mock bridge",
                toolkit.parent / "soul/chiaki-soul.md": "慢慢说话，常用游戏比喻。",
                toolkit / "state/off-voice.json": '["离声词"]',
            }
            for path, value in entries.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
            snapshot = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            adapter = PersonaAdapter(root)
            own = adapter.read_mouth_context(" 秋 ", corpus_limit=4)
            self.assertEqual("秋", own["name"])
            self.assertTrue(own["persona_bound"])
            self.assertEqual(1800, len(own["soul_text"]))
            self.assertEqual([("闲问", "闲答"), ("奇问", "奇答"), ("和问", "和答"), ("怒问", "怒答")],
                             own["corpus_pairs"])
            self.assertEqual(["换个话题", "^模板"], own["off_voice_words"])
            self.assertEqual(toolkit, own["toolkit_dir"])
            self.assertEqual(toolkit.parent / "guard_bridge.mjs", own["guard_mjs"])
            self.assertEqual("精力 0.75，情绪 0.40/0.30，倾向 want_company，态度 warm", own["mind_hint"])
            profile_data = adapter.read_character_profile("秋")
            self.assertEqual(profile_data["traits_summary"], own["traits_summary"])
            self.assertEqual(profile_data["style_tags"], own["style_tags"])
            unbound = adapter.read_mouth_context("澪")
            self.assertFalse(unbound["persona_bound"])
            self.assertEqual("低声而爽快", unbound["soul_text"])
            self.assertEqual([], unbound["corpus_pairs"])
            self.assertEqual("", unbound["mind_hint"])
            chiaki = adapter.read_mouth_context("七海千秋")
            self.assertEqual("慢慢说话，常用游戏比喻。", chiaki["soul_text"])
            self.assertEqual(["离声词"], chiaki["off_voice_words"])
            self.assertEqual(snapshot, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
            (profile / "corpus.tsv").unlink()
            self.assertEqual([("通用问", "通用答")], adapter.read_mouth_context("秋")["corpus_pairs"])


class DialogueMouthRewriterTest(unittest.TestCase):
    def setUp(self):
        self.persona = Mock()
        self.persona.read_mouth_context.return_value = {
            "soul_text": "说短句，语气平静。", "corpus_pairs": [("你找谁？", "我在等玛丽。")],
            "off_voice_words": ["模板短语"], "mind_hint": "精力 0.70",
            "guard_mjs": Path("guard_bridge.mjs"), "toolkit_dir": Path("toolkit"),
        }
        self.model = Mock()
        self.guard = Mock(side_effect=lambda _bridge, _toolkit, lines: lines)
        self.mouth = DialogueMouthRewriter(self.persona, StorySettings, llm_fn=self.model, guard_fn=self.guard)

    def test_score_penalizes_echoes_leaks_directions_and_repetition(self):
        heard = "第一章就到这儿，最后一封信在抽屉里。"
        for line in ("第一章……嗯，信我拿着。", "第一章啊，信我拿着。", "封信在抽屉里。"):
            with self.subTest(line=line):
                self.assertLess(score_mouth_candidate(line, heard_line=heard), score_mouth_candidate(line) - 2)
        natural = score_mouth_candidate("我去把信拿来。")
        for line in ("作为AI，我去把信拿来。", "（轻声）我去把信拿来。", "(点头)我去把信拿来。",
                     "应一声，说：我去把信拿来。", "旧信？ / 我去把信拿来。",
                     "我不想聊这个，换个话题。", "反复拖长台词" * 10):
            with self.subTest(line=line):
                self.assertLess(score_mouth_candidate(line), natural)
        self.assertLess(score_mouth_candidate("我并不想用模板短语。", off_voice_words=("模板短语",)), natural)
        self.assertLess(score_mouth_candidate("别替我盖章。", off_voice_words=("^别替.{0,2}盖章$",)), natural)
        for repeated in ("我去把信拿来。", "我去找找抽屉。", "你留在这儿，我去把信拿来。"):
            self.assertLess(score_mouth_candidate(repeated, recent_replies=("我去把信拿来。",)),
                            score_mouth_candidate(repeated))

    def test_lean_prompt_generation_selection_and_guard_order(self):
        self.model.side_effect = [
            {"candidates": ["钥匙就在那扇门后面。"]},
            {"candidates": ["门后面……嗯，我去拿。", "作为AI我可以去拿。", "模板短语，晚点再拿。",
                            "「你在这儿等我，我去拿。」"]},
            {"candidates": ["我马上回来。"]},
        ]
        self.assertEqual("钥匙就在那扇门后面。", self.mouth.rewrite_utterance("chat", "秋", "我已知钥匙的所在。"))
        self.guard.side_effect = lambda _bridge, _toolkit, lines: [lines[0].rstrip("。")]
        self.assertEqual("你在这儿等我，我去拿", self.mouth.rewrite_utterance(
            "chat", "澪", "我要去取钥匙。", scene_hint="旧车站", listener_hint="秋",
        ))
        call = self.model.call_args
        prompt = json.loads(call.args[1])
        self.assertEqual({"timeout_sec": 7.0, "max_tokens": 360, "temperature": 0.78}, call.kwargs)
        self.assertIn("不照搬事件、人名或台词", call.args[0])
        self.assertIn("钥匙就在那扇门后面。", prompt.values())
        self.assertIn("我要去取钥匙。", prompt.values())
        self.assertIn("旧车站", prompt.values())
        self.assertNotIn("我已知钥匙的所在。", prompt.values())
        self.assertEqual([Path("guard_bridge.mjs"), Path("toolkit"), ["你在这儿等我，我去拿。"]],
                         list(self.guard.call_args.args))
        self.mouth.rewrite_utterance("chat", "澪", "等我一会儿。")
        prompt = json.loads(self.model.call_args.args[1])
        self.assertEqual(["你在这儿等我，我去拿"], prompt["自己近几句（避开重复开头和措辞）"])
        self.assertFalse(any("刚听到" in field for field in prompt))

    def test_invalid_llm_and_guard_fail_open_and_skip_control_lines(self):
        draft = "「（低头）应一声，说：等雨停了再走。」"
        for response in (None, {}, {"candidates": "不可用"}, {"candidates": [None, "（叹气）", "……"]}):
            self.model.return_value = response
            self.assertEqual("等雨停了再走。", self.mouth.rewrite_utterance("chat", "秋", draft))
        self.model.side_effect = TimeoutError
        self.guard.side_effect = FileNotFoundError
        self.assertEqual("等雨停了再走。", self.mouth.rewrite_utterance("chat", "秋", draft))
        self.guard.side_effect = None
        self.guard.return_value = ["（只剩动作）"]
        self.assertEqual(draft, self.mouth.rewrite_utterance("chat", "秋", draft))
        count = self.model.call_count
        self.assertEqual("……", self.mouth.rewrite_utterance("chat", "秋", " …… "))
        self.assertEqual("", self.mouth.rewrite_utterance("chat", "秋", " "))
        self.assertEqual("一声钟响", self.mouth.rewrite_utterance("chat", "旁白", "一声钟响"))
        self.assertEqual(draft, self.mouth.rewrite_utterance("chat", "秋", draft, enabled=False))
        self.assertEqual(count, self.model.call_count)
        self.assertEqual(6, len(self.mouth._recent_by_speaker["chat"]["秋"]))
        self.assertEqual("秋", self.mouth._last_spoken["chat"][0])

    def test_guard_bridge_uses_utf8_json_and_fails_open(self):
        bridge, toolkit = Path("guard_bridge.mjs"), Path("toolkit")
        with patch(f"{MODULE}.subprocess.run") as run:
            run.return_value.stdout = '{"ok":true,"texts":["守卫后的句子"]}'
            self.assertEqual(["守卫后的句子"], _run_guard_bridge(bridge, toolkit, ["草稿"]))
            self.assertEqual(["node", str(bridge), str(toolkit)], run.call_args.args[0])
            self.assertEqual({"texts": ["草稿"]}, json.loads(run.call_args.kwargs["input"]))
            self.assertEqual(2.5, run.call_args.kwargs["timeout"])
            self.assertEqual(0x08000000 if os.name == "nt" else 0, run.call_args.kwargs["creationflags"])
            run.return_value.stdout = '{"ok":false,"texts":["被拒绝"]}'
            self.assertEqual(["草稿"], _run_guard_bridge(bridge, toolkit, ["草稿"]))
            for failure in (FileNotFoundError, subprocess.TimeoutExpired("node", 2.5)):
                run.side_effect = failure
                self.assertEqual(["草稿"], _run_guard_bridge(bridge, toolkit, ["草稿"]))
            self.assertEqual(["草稿"], _run_guard_bridge(None, toolkit, ["草稿"]))

    def test_copy_and_patch_recent_history_in_supported_json_shapes(self):
        self.model.return_value = {"candidates": ["我们换条路走。"]}
        self.mouth.rewrite_utterance("chat", "秋", "建议绕路。")
        line = {"name": "秋", "text": "建议绕路。", "vibe": "沉着"}
        wrapper = {"dialog": [line], "other": True}
        oldest = {"role": "assistant", "content": json.dumps(wrapper, ensure_ascii=False)}
        raw_rows = [
            {"role": "assistant", "content": json.dumps(wrapper, ensure_ascii=False)},
            {"role": "assistant", "content": copy.deepcopy(wrapper)},
            {"role": "assistant", "content": [dict(line)]},
            {"role": "assistant", "content": dict(line)},
        ]
        messages = [oldest, *[copy.deepcopy(r) for r in raw_rows], *raw_rows]
        self.mouth.apply_rewrites_to_messages("chat", messages)
        self.assertIn("建议绕路。", oldest["content"])
        patched = {**line, "text": "我们换条路走。"}
        expected = {"dialog": [patched], "other": True}
        self.assertEqual(expected, json.loads(raw_rows[0]["content"]))
        self.assertEqual(expected, raw_rows[1]["content"])
        self.assertEqual([patched], raw_rows[2]["content"])
        self.assertEqual(patched, raw_rows[3]["content"])
        utterances = [{"character_name": "秋", "speech": "建议绕路。"}, {"character_name": "澪", "speech": "建议绕路。"}]
        actual = self.mouth.apply_rewrites_to_utterances("chat", utterances)
        self.assertEqual(["我们换条路走。", "建议绕路。"], [row["speech"] for row in actual])
        self.assertEqual("建议绕路。", utterances[0]["speech"])
        self.assertEqual(utterances, self.mouth.apply_rewrites_to_utterances("other chat", utterances))

    def test_turn_completed_before_or_after_media_reuses_durable_lines(self):
        row = {"character_name": "秋", "speech": "去那里。", "sprite": "4", "translate": "Go there."}
        message = {"role": "assistant", "content": {"dialog": [row, row]}}
        ids = DialogIdentities("turn-1")
        first = ids.next(LLMDialogMessage(**row).model_dump(by_alias=True))
        second = ids.next(LLMDialogMessage(**row).model_dump(by_alias=True))
        self.model.side_effect = [{"candidates": ["跟我来。"]}, {"candidates": ["往里走。"]}]
        self.mouth.rewrite_utterance("chat", "秋", row["speech"], dialog_id=first)
        self.mouth.complete_turn("chat", message, "turn-1", ["秋"])
        self.assertEqual("往里走。", self.mouth.rewrite_utterance("chat", "秋", row["speech"], dialog_id=second))
        spoken = self.mouth.apply_rewrites_to_utterances(
            "chat", [{"character_name": "秋", "speech": row["speech"]}] * 2, message=message, turn_id="turn-1",
        )
        self.assertEqual(["跟我来。", "往里走。"], [item["speech"] for item in spoken])
        self.assertEqual(2, self.model.call_count)

    def test_llm_adapter_forwards_default_and_mouth_temperature(self):
        adapter = SimpleNamespace(chat=Mock(side_effect=[TypeError, '{"ok":true}', '{"ok":true}']))
        self.assertEqual({"ok": True}, ask_llm_json("system", "user", adapter_provider=lambda: adapter))
        self.assertEqual({"ok": True}, ask_llm_json("system", "user", adapter_provider=lambda: adapter, temperature=0.78))
        self.assertEqual([0.45, 0.45, 0.78], [call.kwargs["temperature"] for call in adapter.chat.call_args_list])


class StoryMouthDialogHandlerTest(unittest.TestCase):
    def setUp(self):
        self.settings = Mock(return_value=StorySettings())
        self.injector = Mock()
        self.injector.is_native_story.return_value = False
        self.injector.active_dialogue_session.return_value = ({"paused": False}, "chat", ["Akino"])
        self.rewriter = Mock()
        self.rewriter.rewrite_utterance.return_value = "自然说出的台词。"
        self.handler = StoryMouthDialogHandler(
            injector=self.injector, rewriter=self.rewriter, settings_provider=self.settings,
        )

    def test_cast_controls_replay_native_pause_and_settings(self):
        with patch.object(self.handler, "_build_downstream", return_value=[Mock()]) as build:
            for msg in (
                LLMDialogMessage(name="陌生人", text="路过。"),
                LLMDialogMessage(name="NARR", text="舞台灯亮了。"),
                LLMDialogMessage(name="旁白", text="舞台灯亮了。"),
                LLMDialogMessage(name="CG", text="画面"),
                LLMDialogMessage(name="Akino", text=" "),
                LLMDialogMessage(name="Akino", text="历史回放。", _presentation_replay=True),
            ):
                self.assertFalse(self.handler.can_handle(msg))
            live = LLMDialogMessage(name=" AKINO ", text="原稿。")
            for setting in (StorySettings(enabled=False), StorySettings(use_llm_compiler=False),
                            StorySettings(dialogue_mouth_decoupling=False)):
                self.settings.return_value = setting
                self.assertFalse(self.handler.can_handle(live))
            self.settings.return_value = StorySettings()
            self.injector.is_native_story.return_value = True
            self.assertFalse(self.handler.can_handle(live))
            self.injector.is_native_story.return_value = False
            self.injector.active_dialogue_session.return_value = ({"paused": True}, "chat", ["Akino"])
            self.assertFalse(self.handler.can_handle(live))
            self.injector.active_dialogue_session.return_value = (None, "chat", [])
            self.assertFalse(self.handler.can_handle(live))
            build.assert_not_called()
            self.injector.active_dialogue_session.return_value = ({"paused": False}, "chat", ["Akino"])
            self.assertTrue(self.handler.can_handle(live))
            self.handler.pre_process(live)
            self.assertEqual("自然说出的台词。", live.text)
            self.assertFalse(self.handler.can_handle(live))

    def test_rewrites_before_real_nanami_handler_without_recursion(self):
        pointer = SimpleNamespace(pointer_id="test")
        plugin = Mock()
        plugin._primary = pointer
        plugin._runtime_for_name.return_value = pointer
        plugin.guard_text.return_value = "人格守卫之后。"
        plugin.should_drop_bubble.return_value = False
        persona = NanamiDialogHandler(plugin=plugin)
        media = Mock()
        with patch(PLUGIN_HANDLERS, return_value=[self.handler, persona]), patch(HANDLERS, return_value=[media]):
            msg = LLMDialogMessage(name="Akino", text="原稿。", turn_id=12, dialog_id="live")
            self.assertTrue(self.handler.can_handle(msg))
            self.handler.pre_process(msg)
            self.handler.handle(msg)
            self.handler.post_process(msg)
        self.rewriter.rewrite_utterance.assert_called_once()
        plugin.guard_text.assert_called_once_with("自然说出的台词。")
        media.handle.assert_called_once_with(msg)
        self.assertEqual("人格守卫之后。", msg.text)

    def test_failed_chain_leaves_host_to_dispatch(self):
        with patch.object(self.handler, "_build_downstream", side_effect=RuntimeError), self.assertLogs(MODULE):
            self.assertFalse(self.handler.can_handle(LLMDialogMessage(name="Akino", text="原稿。")))
        self.assertFalse(self.handler.can_handle(LLMDialogMessage(name="Akino", text="新的台词。")))
        self.rewriter.rewrite_utterance.assert_not_called()
