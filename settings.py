"""故事引擎（nanami_story）运行时配置管理（纯标准库 JSON，原子写盘）。"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    """线程安全故事引擎配置读写器。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._cached: StorySettings | None = None

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> StorySettings:
        with self._lock:
            if self._cached is not None:
                return self._cached
            self._cached = self._load_locked()
            return self._cached

    def reload(self) -> StorySettings:
        with self._lock:
            self._cached = self._load_locked()
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
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp")
            tmp.write_text(
                json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("nanami_story: 保存设置失败: %s", self._path, exc_info=True)
