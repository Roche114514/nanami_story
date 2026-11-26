"""Read the host's built-in background catalog without changing host state."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

try:
    from sdk.asset_tags import tag_contents
except ImportError:
    def tag_contents(block: str, count: int) -> list[str]:
        return [re.split(r"[:：]", line, maxsplit=1)[-1].strip()
                for line in block.splitlines() if line.strip()][:count]


BACKGROUND_SCENE_KEYS = ("background_group", "background_scene_index", "background_scene_path")
_NUMBERED_TAG = re.compile(r"^[^:：]*?(\d+)\s*[:：]")
_KEYWORDS = re.compile(r"[\u3400-\u9fff]+|[^\W_]+")


def resolve_catalog_scene(
    scenes: Sequence[Mapping[str, Any]], *, scene_index: int = 0, scene_name: str = "",
) -> Mapping[str, Any] | None:
    # Accept integer strings from JSON clients without coercing booleans or fractions.
    try:
        index = int(str(scene_index))
    except ValueError:
        index = 0
    if index > 0:
        match = next((scene for scene in scenes if scene["index"] == index), None)
        if match is not None:
            return match
    name = str(scene_name or "").strip().casefold()
    if not name:
        return None
    for scene in scenes:
        if name == scene["label"].casefold():
            return scene
    return next((scene for scene in scenes if name in scene["label"].casefold()), None)


def match_catalog_scene(
    scenes: Sequence[Mapping[str, Any]], text: str,
) -> Mapping[str, Any] | None:
    if not scenes:
        return None
    text = str(text or "").casefold()
    words = set(_KEYWORDS.findall(text))

    def score(scene: Mapping[str, Any]) -> int:
        label = scene["label"].casefold()
        if label in text:
            return 100 + len(label)
        overlap = set()
        for word in _KEYWORDS.findall(label):
            if "\u3400" <= word[0] <= "\u9fff":
                overlap.update(word[i:i + 2] for i in range(len(word) - 1) if word[i:i + 2] in text)
            elif len(word) > 1 and word in words:
                overlap.add(word)
        return len(overlap)

    return max(scenes, key=score)


def background_scene_fields(background_name: str, scene: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "background_group": background_name,
        "background_scene_index": scene["index"],
        "background_scene_path": scene["path"],
        "scene_name": scene["label"],
    }


def _assets(rows: Any, tags: Any, *, scenes: bool = False) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    block = str(tags or "")
    lines = [line for line in block.splitlines() if line.strip()]
    labels = {}
    for position, (line, label) in enumerate(zip(lines, tag_contents(block, len(lines))), 1):
        numbered = _NUMBERED_TAG.match(line)
        index = int(numbered[1]) if numbered else position
        labels[index] = label
    assets = []
    for index, row in enumerate(rows, 1):
        path = row.get("path") if isinstance(row, Mapping) else row
        if isinstance(path, str) and path.strip():
            label = labels.get(index) or (f"场景 {index}" if scenes else "")
            assets.append({"index": index, "label": label, "path": path.strip()})
    return assets


def _path_key(path: str) -> str:
    return posixpath.normpath(path.strip().replace("\\", "/")).casefold()


class BackgroundAdapter:
    def __init__(self, project_root: Path | str | None = None) -> None:
        self._path = Path(project_root or Path.cwd()) / "data" / "config" / "background.yaml"
        self._mtime_ns: int | None = None
        self._backgrounds: list[dict[str, Any]] = []
        self._lock = Lock()

    def _read_backgrounds(self) -> list[dict[str, Any]]:
        if yaml is None:
            return []
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, yaml.YAMLError):
            return []
        if not isinstance(raw, list):
            return []
        backgrounds = []
        for group in raw:
            if not isinstance(group, Mapping):
                continue
            name = str(group.get("name") or "").strip()
            if not name or name in {"透明场景", "透明背景"}:
                continue
            scenes = _assets(group.get("sprites"), group.get("bg_tags"), scenes=True)
            bgms = _assets(group.get("bgm_list"), group.get("bgm_tags"))
            backgrounds.append({
                "name": name, "sprite_prefix": str(group.get("sprite_prefix") or ""),
                "scene_count": len(scenes), "bgm_count": len(bgms), "scenes": scenes, "bgms": bgms,
            })
        return backgrounds

    def list_backgrounds(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                stamp = self._path.stat().st_mtime_ns
            except OSError:
                stamp = None
            if stamp != self._mtime_ns:
                self._backgrounds = self._read_backgrounds() if stamp is not None else []
                self._mtime_ns = stamp
            return deepcopy(self._backgrounds)

    def get_background(self, name: str) -> dict[str, Any] | None:
        name = str(name or "").strip()
        backgrounds = self.list_backgrounds()
        return next((group for group in backgrounds if group["name"] == name), None) or next(
            (group for group in backgrounds if group["name"].casefold() == name.casefold()), None,
        )

    def resolve_scene(
        self, background_name: str, *, scene_index: int = 0, scene_name: str = "",
    ) -> dict[str, Any] | None:
        group = self.get_background(background_name)
        scene = resolve_catalog_scene(group["scenes"], scene_index=scene_index, scene_name=scene_name) if group else None
        return dict(scene) if scene is not None else None

    def find_scene_by_path(self, path: str) -> dict[str, Any] | None:
        if not path or not path.strip():
            return None
        target = _path_key(path)
        for group in self.list_backgrounds():
            for scene in group["scenes"]:
                if _path_key(scene["path"]) == target:
                    return {"background_name": group["name"], **scene}
        return None

    def match_scene_for_text(self, background_name: str, text: str) -> dict[str, Any] | None:
        group = self.get_background(background_name)
        scene = match_catalog_scene(group["scenes"], text) if group else None
        return dict(scene) if scene is not None else None
