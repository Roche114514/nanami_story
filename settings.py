"""故事引擎（nanami_story）运行时配置管理（纯标准库 JSON，原子写盘）。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .fate_schema import VALID_THEME_COMPASSES, clip_text, normalize_max_nodes

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"
_VALID_MODES = ("co_play", "observer")


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return max(low, min(high, parsed))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(frozen=True)
class StorySettings:
    """`nanami_story` 插件级配置。"""

    enabled: bool = True
    poll_interval_sec: float = 1.5
    use_llm_compiler: bool = True
    default_experience_mode: str = "co_play"
    persona_data_dir: str = ""
    dialogue_mouth_decoupling: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "StorySettings":
        src = raw if isinstance(raw, Mapping) else {}
        mode = str(src.get("default_experience_mode") or "co_play").strip().lower()
        if mode == "blind_play" or mode not in _VALID_MODES:
            mode = "co_play"
        return cls(
            enabled=_as_bool(src.get("enabled"), True),
            poll_interval_sec=_clamp_float(src.get("poll_interval_sec"), 0.5, 30.0, 1.5),
            use_llm_compiler=_as_bool(src.get("use_llm_compiler"), True),
            default_experience_mode=mode,
            persona_data_dir=str(src.get("persona_data_dir") or "").strip(),
            dialogue_mouth_decoupling=_as_bool(src.get("dialogue_mouth_decoupling"), True),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    """线程安全故事引擎配置读写器。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._cached: StorySettings | None = None
        self._mtime_ns: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> StorySettings:
        with self._lock:
            if self._cached is not None and self._mtime_ns == file_mtime_ns(self._path):
                return self._cached
            return self.reload()

    def reload(self) -> StorySettings:
        with self._lock:
            self._cached = self._load_locked()
            self._mtime_ns = file_mtime_ns(self._path)
            return self._cached

    def update(self, incoming: Mapping[str, Any] | None) -> StorySettings:
        with self._lock:
            current = self._load_locked().to_dict()
            if isinstance(incoming, Mapping):
                for key in current:
                    if key in incoming:
                        current[key] = incoming[key]
            updated = StorySettings.from_mapping(current)
            self._save_locked(updated)
            self._cached = updated
            self._mtime_ns = file_mtime_ns(self._path)
            return updated

    def _load_locked(self) -> StorySettings:
        if not self._path.is_file():
            settings = StorySettings()
            self._save_locked(settings)
            return settings
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("nanami_story: 读取设置失败，使用默认配置: %s", self._path, exc_info=True)
            return StorySettings()
        return StorySettings.from_mapping(raw if isinstance(raw, Mapping) else {})

    def _save_locked(self, settings: StorySettings) -> None:
        try:
            atomic_write_json(self._path, settings.to_dict())
        except OSError:
            logger.warning("nanami_story: 保存设置失败: %s", self._path, exc_info=True)


def file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def dialogue_write_lock(data_root: Path):
    """Serialize Bridge writes with the headless/unavailable-Bridge fallback."""
    lock_path = data_root / "dialogue.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def canonical_history_path(history_path: str | Path) -> str:
    clean = str(history_path or "").strip()
    if not clean:
        return ""
    candidate = Path(clean).expanduser()
    try:
        from core.chat_history.storage import chat_history_session_dir
    except ImportError:
        if candidate.suffix.lower() == ".json":
            candidate = (candidate.parent if candidate.name in {"active.json", "branches.json"}
                         else candidate.with_suffix(""))
    else:
        candidate = chat_history_session_dir(candidate)
    return os.path.normcase(str(candidate.resolve()))


def history_has_native_story(history_path: str | Path) -> bool:
    path = canonical_history_path(history_path)
    return bool(path and any((Path(path) / name).is_file()
                             for name in ("story-prompt-binding.json", "story-v2.json")))


def dialogue_character_names(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    names = {}
    for item in raw:
        value = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(value, str) and value.strip():
            clean = value.strip()
            names.setdefault(clean.casefold(), clean)
    return list(names.values())[:32]


class ChatBindingsStore:
    def __init__(self, data_root: Path) -> None:
        self._path = Path(data_root) / "chat_bindings.json"
        self._mtime_ns: int | None = None
        self._bindings: dict[str, Any] = {}

    def reload_if_changed(self) -> dict[str, Any]:
        stamp = file_mtime_ns(self._path)
        if stamp != self._mtime_ns:
            self._bindings = read_json_object(self._path) or {}
            self._mtime_ns = stamp
        return self._bindings

    def replace(self, bindings: dict[str, Any]) -> None:
        atomic_write_json(self._path, bindings)
        self._bindings = bindings
        self._mtime_ns = file_mtime_ns(self._path)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_dialogue_background(raw: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(raw.get("background_mode") or "").strip().lower()
    return {
        "background_mode": "shinsekai" if mode == "shinsekai" else "custom",
        "opening_background": clip_text(raw.get("opening_background"), 160, ""),
        "opening_scene_index": min(200, _nonnegative_int(raw.get("opening_scene_index"))),
        "allow_scene_transitions": bool(raw.get("allow_scene_transitions", True)),
    }


def normalize_dialogue_preset(raw: Mapping[str, Any]) -> dict[str, Any]:
    theme = str(raw.get("theme_compass") or "auto").strip().lower()
    draft = raw.get("custom_draft")
    clean_draft: dict[str, Any] = {}
    if isinstance(draft, Mapping):
        clean_draft.update({
            key: clip_text(draft[key], size)
            for key, size in (("premise", 240), ("public_incident_fact", 260), ("scene_name", 80),
                              ("atmosphere", 160), ("background_group", 160), ("background_scene_path", 320))
            if key in draft
        })
        if "background_scene_index" in draft:
            clean_draft["background_scene_index"] = _nonnegative_int(draft["background_scene_index"])
        node = draft.get("node1")
        if isinstance(node, Mapping):
            clean_draft["node1"] = {key: clip_text(node[key], size) for key, size in
                                   (("situation", 320), ("dilemma_frame", 240)) if key in node}
        roles = draft.get("character_roles")
        if isinstance(roles, Mapping):
            clean_draft["character_roles"] = {
                name: {key: clip_text(role[key], size) for key, size in (("local_role", 36),
                       ("relationship_to_incident", 120)) if key in role}
                for name in list(roles)[:32] if isinstance(name, str) and isinstance((role := roles[name]), Mapping)
            }
    exp_mode = str(raw.get("experience_mode") or "co_play").strip().lower()
    observer_autoplay = raw.get("observer_autoplay")
    observer_interval = _clamp_float(raw.get("observer_interval_sec"), 3.0, 60.0, 8.0)
    return {
        "title": clip_text(raw.get("title"), 56, "未命名对话剧本"),
        "theme_compass": theme if theme in VALID_THEME_COMPASSES else "auto",
        "outline": str(raw.get("outline") or raw.get("custom_script") or "").strip()[:12000],
        "max_nodes": normalize_max_nodes(raw.get("max_nodes"), minimum=1),
        "characters": dialogue_character_names(raw.get("characters")),
        **normalize_dialogue_background(raw),
        "custom_draft": clean_draft,
        "use_narration": bool(raw.get("use_narration", False)),
        "experience_mode": "observer" if exp_mode == "observer" else "co_play",
        "observer_autoplay": exp_mode == "observer" and (observer_autoplay is None or bool(observer_autoplay)),
        "observer_interval_sec": observer_interval,
    }


class DialoguePresetStore:
    def __init__(self, data_root: Path) -> None:
        self._dir = Path(data_root) / "dialogue_presets"

    def _path(self, preset_id: str) -> Path:
        if not preset_id or not all(c.isascii() and (c.isalnum() or c in "_-") for c in preset_id):
            raise ValueError("无效的剧本预设 ID。")
        return self._dir / f"{preset_id}.json"

    def list(self) -> list[dict[str, Any]]:
        rows = [row for path in self._dir.glob("*.json") if (row := read_json_object(path))]
        return sorted(rows, key=lambda row: row.get("updated_at", 0), reverse=True)

    def save(self, raw: Mapping[str, Any], *, preset_id: str = "") -> dict[str, Any]:
        preset_id = preset_id or f"dialogue_{uuid.uuid4().hex[:12]}"
        preset = {**normalize_dialogue_preset(raw), "preset_id": preset_id, "updated_at": time.time()}
        atomic_write_json(self._path(preset_id), preset)
        return preset

    def delete(self, preset_id: str) -> None:
        self._path(preset_id).unlink(missing_ok=True)

    def export_json(self, preset_id: str) -> str:
        preset = read_json_object(self._path(preset_id))
        if preset is None:
            raise ValueError("未找到剧本预设。")
        return json.dumps(normalize_dialogue_preset(preset), ensure_ascii=False, indent=2)

    def import_json(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, Mapping):
            raise ValueError("剧本预设须为 JSON 对象。")
        return self.save(raw)
