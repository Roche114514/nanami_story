"""只读人格适配器：仅读取宿主角色表与 nanami_persona 性格底色，绝不写入任何人格文件。"""

from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _read_json_value(path: Path) -> Any:
    try:
        return json.loads(_read_text(path))
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    raw = _read_json_value(path)
    return raw if isinstance(raw, dict) else None


def _corpus_pairs(path: Path, limit: int) -> list[tuple[str, str]]:
    buckets: dict[str, deque[tuple[str, str]]] = {}
    if limit <= 0:
        return []
    for line in _read_text(path).splitlines():
        fields = line.strip().split("\t", 2)
        if line.lstrip().startswith("#") or len(fields) != 3:
            continue
        bucket, prompt, reply = (field.strip() for field in fields)
        if prompt and reply:
            buckets.setdefault(bucket, deque()).append((prompt[:160], reply[:160]))
    preferred = ("casual", "curious", "repair_afterglow")
    ordered = [buckets.pop(key) for key in preferred if key in buckets] + list(buckets.values())
    pairs: list[tuple[str, str]] = []
    while len(pairs) < limit and any(ordered):
        for rows in ordered:
            if rows and len(pairs) < limit:
                pair = rows.popleft()
                if pair not in pairs:
                    pairs.append(pair)
    return pairs


def _off_voice_words(path: Path) -> list[str]:
    raw = _read_json_value(path)
    if isinstance(raw, Mapping):
        groups = [raw.get(key) for key in ("ban", "words", "phrases", "items", "patterns")]
    else:
        groups = [raw]
    return list(dict.fromkeys(word.strip() for group in groups if isinstance(group, list)
                              for word in group if isinstance(word, str) and word.strip()))


def _mind_hint(state_root: Path | None) -> str:
    if state_root is None:
        return ""
    raw = _read_json(state_root / "state" / "mind.json") or {}

    def updated(entry: tuple[int, Mapping[str, Any]]) -> tuple[float, int]:
        index, row = entry
        try:
            return float(row.get("updatedAt") or row.get("lastHerAt") or 0), index
        except (TypeError, ValueError, OverflowError):
            return 0.0, index

    _, row = max(((i, value) for i, value in enumerate(raw.values()) if isinstance(value, Mapping)),
                 key=updated, default=(0, {}))
    mood = row.get("mood")
    if mood is None and isinstance(row.get("moodV"), (int, float)):
        mood = f"{row['moodV']:.2f}"
        if isinstance(row.get("moodA"), (int, float)):
            mood += f"/{row['moodA']:.2f}"
    attitude = row.get("attitude")
    stance = attitude.get("attitudeStance") if isinstance(attitude, Mapping) else None
    return "，".join(
        f"{label} {value:.2f}" if isinstance(value, float) else f"{label} {str(value).strip()[:48]}"
        for label, value in (
            ("精力", row.get("energy")), ("情绪", mood), ("倾向", row.get("want") or row.get("agenda")),
            ("态度", row.get("stance") or stance),
        ) if isinstance(value, (str, int, float)) and str(value).strip()
    )


def _clamp_f(val: Any, lo: float, hi: float, default: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return round(max(lo, min(hi, num)), 3)


def _parse_characters_yaml_fallback(raw_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if cur and cur.get("name"):
                rows.append(cur)
            cur = {}
            stripped = stripped[2:].strip()
        if cur is None or ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        k = key.strip()
        v = val.strip().strip("'\"")
        if k in ("name", "setting", "avatar", "desc"):
            cur[k] = v
    if cur and cur.get("name"):
        rows.append(cur)
    return rows


def _parse_pointers_yaml(text: str) -> list[dict[str, Any]]:
    parsed: Any = None
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(text)
    except Exception:
        parsed = None

    entries: list[dict[str, Any]] = []
    if isinstance(parsed, Mapping) and isinstance(parsed.get("pointers"), list):
        for item in parsed["pointers"]:
            if isinstance(item, Mapping):
                entries.append({
                    "id": str(item.get("id") or "").strip(),
                    "enabled": bool(item.get("enabled", True)),
                    "character_name": str(item.get("character_name") or "").strip(),
                    "profile_dir": str(item.get("profile_dir") or "").strip(),
                    "state_root": str(item.get("state_root") or "").strip(),
                })
        return entries

    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- "):
            if cur:
                entries.append(cur)
            cur = {"id": "", "enabled": True, "character_name": "", "profile_dir": "", "state_root": ""}
            s = s[2:].strip()
        if cur is None:
            continue
        if s.startswith("id:"):
            cur["id"] = s.split(":", 1)[1].strip().strip("'\"")
        elif s.startswith("enabled:"):
            cur["enabled"] = s.split(":", 1)[1].strip().lower() not in ("false", "0", "no")
        elif s.startswith("character_name:"):
            cur["character_name"] = s.split(":", 1)[1].strip().strip("'\"")
        elif s.startswith("profile_dir:"):
            cur["profile_dir"] = s.split(":", 1)[1].strip().strip("'\"")
        elif s.startswith("state_root:"):
            cur["state_root"] = s.split(":", 1)[1].strip().strip("'\"")
    if cur:
        entries.append(cur)
    return entries


class PersonaAdapter:
    """只读读取宿主角色目录及 `nanami_persona` 性格与灵魂设置。"""

    def __init__(self, project_root: Path, *, custom_persona_dir: str = "") -> None:
        self._project_root = Path(project_root)
        self._custom_persona_dir = str(custom_persona_dir or "").strip()

    def _base_persona_dir(self, custom_persona_dir: str = "") -> Path | None:
        cands: list[Path] = []
        eff_custom = str(custom_persona_dir or self._custom_persona_dir).strip()
        if eff_custom:
            cands.append(Path(eff_custom).expanduser())
        cands.extend([self._project_root / "data" / "nanami", Path("data/nanami")])
        return next((p for p in cands if p.is_dir()), None)

    def _read_host_characters(self) -> list[dict[str, str]]:
        cands = [
            self._project_root / "data" / "config" / "characters.yaml",
            Path("data/config/characters.yaml"),
        ]
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for path in cands:
            if not path.is_file():
                continue
            try:
                raw_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed: Any = None
            try:
                import yaml  # type: ignore

                parsed = yaml.safe_load(raw_text)
            except Exception:
                parsed = _parse_characters_yaml_fallback(raw_text)
            if isinstance(parsed, list):
                for row in parsed:
                    if not isinstance(row, Mapping):
                        continue
                    name = str(row.get("name") or "").strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    items.append({
                        "name": name,
                        "setting": str(row.get("setting") or "").strip()[:1800],
                        "desc": str(row.get("desc") or "").strip()[:200],
                    })
            if items:
                break
        return items

    def _resolve_persona_roots(
        self,
        base_dir: Path | None,
        character_name: str,
        *,
        exact: bool = False,
    ) -> tuple[Path | None, Path | None]:
        if base_dir is None:
            return None, None
        target = str(character_name or "").strip()
        p_file = base_dir / "pointers.yaml"
        if not p_file.is_file():
            if target in ("", "七海千秋"):
                prof = base_dir / "profiles" / "nanami_chiaki"
                return base_dir, (prof if prof.is_dir() else None)
            return None, None
        try:
            entries = _parse_pointers_yaml(p_file.read_text(encoding="utf-8"))
        except OSError:
            return None, None

        matches = [r for r in entries if r.get("character_name") == target or r.get("id") == target]
        if not matches and target and not exact:
            matches = [
                r for r in entries
                if r.get("character_name") and (target in r["character_name"] or r["character_name"] in target)
            ]
        if not matches:
            return None, None

        def _entry_state_root(row: Mapping[str, Any]) -> Path:
            raw_sr = str(row.get("state_root") or "").strip()
            if raw_sr:
                cand = Path(raw_sr).expanduser()
                if not cand.is_absolute():
                    cand = (base_dir / cand).resolve()
                return cand
            pid = str(row.get("id") or "").strip()
            if not pid or pid == "nanami_chiaki":
                return base_dir
            return (base_dir / "profiles" / pid / "runtime").resolve()

        best = max(matches, key=lambda r: 1 if r.get("enabled", True) else 0)
        state_root = _entry_state_root(best)
        raw_prof = str(best.get("profile_dir") or "").strip()
        prof_dir: Path | None = None
        if raw_prof:
            p_cand = Path(raw_prof).expanduser()
            if not p_cand.is_absolute():
                p_cand = (base_dir / p_cand).resolve()
            if p_cand.is_dir():
                prof_dir = p_cand
        if prof_dir is None and state_root.name == "runtime" and state_root.parent.is_dir():
            prof_dir = state_root.parent
        elif prof_dir is None and best.get("id") == "nanami_chiaki" and (base_dir / "profiles" / "nanami_chiaki").is_dir():
            prof_dir = base_dir / "profiles" / "nanami_chiaki"
        return state_root, prof_dir

    def list_available_characters(self, *, custom_persona_dir: str = "") -> list[dict[str, Any]]:
        base_dir = self._base_persona_dir(custom_persona_dir)
        host_rows = self._read_host_characters()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        for row in host_rows:
            name = row["name"]
            seen.add(name)
            sr, pd = self._resolve_persona_roots(base_dir, name)
            bound = bool((sr and sr.is_dir()) or (pd and pd.is_dir()))
            out.append({
                "name": name,
                "persona_bound": bound,
                "desc": row.get("desc") or "",
                "has_setting": bool(row.get("setting")),
            })

        if base_dir is not None and (base_dir / "pointers.yaml").is_file():
            try:
                for entry in _parse_pointers_yaml((base_dir / "pointers.yaml").read_text(encoding="utf-8")):
                    cname = str(entry.get("character_name") or "").strip()
                    if cname and cname not in seen:
                        seen.add(cname)
                        out.append({
                            "name": cname,
                            "persona_bound": True,
                            "desc": "人格模型绑定角色",
                            "has_setting": True,
                        })
            except OSError:
                pass
        return out

    def read_character_profile(self, name: str, *, custom_persona_dir: str = "") -> dict[str, Any]:
        clean = str(name or "").strip()
        base_dir = self._base_persona_dir(custom_persona_dir)
        state_root, prof_dir = self._resolve_persona_roots(base_dir, clean)
        return self._character_profile(clean, state_root, prof_dir)

    def _character_profile(
        self, clean: str, state_root: Path | None, prof_dir: Path | None, *, mouth: bool = False,
    ) -> dict[str, Any]:
        host_entry = next((row for row in self._read_host_characters() if row["name"] == clean), {})
        soul_cands = [root / "soul.md" for root in (prof_dir, state_root) if root is not None]
        if mouth and clean == "七海千秋":
            soul_cands.append(self._project_root / "plugins/nanami_persona/soul/chiaki-soul.md")
        if not mouth and state_root is not None:
            soul_cands.append(state_root.parent / "soul.md")
        soul_text = next((value for path in soul_cands if (value := _read_text(path).strip())), "")
        setting = host_entry.get("setting", "")

        defaults: dict[str, float] = {
            "effortfulControl": 0.72,
            "arousalBaseline": 0.38,
            "domainEfficacy.game": 0.65,
            "domainEfficacy.nonGame": 0.45,
            "needProfile.safety": 0.52,
            "needProfile.relatedness": 0.62,
            "needProfile.competence": 0.65,
            "socialEfficacy": 0.45,
            "surgency": 0.40,
            "attachment.avoidance": 0.45,
            "social.affiliation": 0.55,
            "social.prosocial": 0.68,
            "social.normCompliance": 0.70,
        }
        merged = dict(defaults)
        has_traits_file = False
        if state_root is not None and state_root.is_dir():
            for fname, wrapper in (("traits.json", "traits"), ("social_traits.json", "socialTraits")):
                raw = _read_json(state_root / "state" / fname)
                if not raw:
                    continue
                has_traits_file = True
                src = raw.get(wrapper, raw.get("traits", raw))
                if not isinstance(src, Mapping):
                    continue
                for k in defaults:
                    if k in src:
                        val = src[k]
                    else:
                        parent, _, child = k.partition(".")
                        branch = src.get(parent)
                        val = branch.get(child) if child and isinstance(branch, Mapping) else None
                    if val is not None:
                        merged[k] = _clamp_f(val, 0.0, 1.0, merged[k])

        style_tags: list[str] = []
        if merged["effortfulControl"] >= 0.72:
            style_tags.append("沉着克制、先思后言")
        if merged["socialEfficacy"] <= 0.38 or merged["surgency"] <= 0.35:
            style_tags.append("内敛慢热、不喜夸张表演")
        elif merged["surgency"] >= 0.62:
            style_tags.append("直爽主动、乐于开口")
        if merged["social.normCompliance"] >= 0.68 and merged["social.prosocial"] >= 0.68:
            style_tags.append("守序体贴、不愿连累他人")
        elif merged["social.normCompliance"] < 0.42:
            style_tags.append("行事不拘常理")
        if merged["domainEfficacy.game"] >= 0.75:
            style_tags.append("擅长机制推演与逻辑解谜")
        if merged["attachment.avoidance"] >= 0.52:
            style_tags.append("对陌生人保有安全距离")
        if not style_tags:
            style_tags.append("温和务实、按直觉观察细节")

        return {
            "name": clean or "无名角色",
            "persona_bound": bool((state_root and state_root.is_dir()) or (prof_dir and prof_dir.is_dir())),
            "has_traits_file": has_traits_file,
            "soul_text": (soul_text or setting)[:1800] if mouth else soul_text[:700] or setting[:500],
            "setting_summary": setting[:500],
            "traits_summary": {
                "effortful_control": merged["effortfulControl"],
                "social_efficacy": merged["socialEfficacy"],
                "surgency": merged["surgency"],
                "norm_compliance": merged["social.normCompliance"],
                "prosocial": merged["social.prosocial"],
                "avoidance": merged["attachment.avoidance"],
                "safety_need": merged["needProfile.safety"],
                "relatedness_need": merged["needProfile.relatedness"],
                "affiliation": merged["social.affiliation"],
                "competence_need": merged["needProfile.competence"],
                "domain_efficacy_game": merged["domainEfficacy.game"],
            },
            "style_tags": style_tags,
        }

    def read_mouth_context(
        self, name: str, *, custom_persona_dir: str = "", corpus_limit: int = 8,
    ) -> dict[str, Any]:
        """Read the speaker's voice references and a compact mind-state snapshot."""
        clean = str(name or "").strip()
        base_dir = self._base_persona_dir(custom_persona_dir)
        state_root, prof_dir = self._resolve_persona_roots(base_dir, clean, exact=True) if clean else (None, None)
        profile = self._character_profile(clean, state_root, prof_dir, mouth=True)
        persona_dir = self._project_root / "plugins" / "nanami_persona"
        toolkit = persona_dir / "toolkit"
        guard = persona_dir / "guard_bridge.mjs"
        pairs = _corpus_pairs(prof_dir / "corpus.tsv", corpus_limit) if prof_dir is not None else []
        if not pairs and profile["persona_bound"] and base_dir is not None:
            pairs = _corpus_pairs(base_dir / "corpus" / "acg_corpus.tsv", corpus_limit)
        words = _off_voice_words(prof_dir / "off-voice.json") if prof_dir is not None else []
        if not words and clean == "七海千秋":
            words = _off_voice_words(toolkit / "state" / "off-voice.json")
        return {
            "name": clean,
            "persona_bound": profile["persona_bound"],
            "soul_text": profile["soul_text"],
            "corpus_pairs": pairs,
            "off_voice_words": words,
            "guard_mjs": guard if guard.is_file() else None,
            "toolkit_dir": toolkit if toolkit.is_dir() else None,
            "mind_hint": _mind_hint(state_root),
            "traits_summary": profile["traits_summary"],
            "style_tags": profile["style_tags"],
        }

    def read_character_relations(
        self,
        name: str,
        *,
        custom_persona_dir: str = "",
    ) -> dict[str, dict[str, Any]]:
        """只读加载 `state/relations.json`，返回精简版角色对各人的态度关系表。"""
        clean = str(name or "").strip()
        if not clean:
            return {}
        base_dir = self._base_persona_dir(custom_persona_dir)
        state_root, _ = self._resolve_persona_roots(base_dir, clean)
        if state_root is None or not state_root.is_dir():
            return {}
        raw = _read_json(state_root / "state" / "relations.json")
        if not raw:
            return {}
        targets = raw.get("targets") if isinstance(raw.get("targets"), Mapping) else raw
        if not isinstance(targets, Mapping):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for tkey, row in targets.items():
            if not isinstance(row, Mapping):
                continue
            key_str = str(tkey or "").strip()
            tname = str(row.get("target_name") or "").strip()
            if not tname:
                tname = key_str.split(":", 1)[1] if ":" in key_str else key_str
            if not tname:
                continue
            out[tname] = {
                "rel_stage": str(row.get("rel_stage") or "stranger").strip().lower(),
                "trust": _clamp_f(row.get("trust"), 0.0, 1.0, 0.55),
                "warmth": _clamp_f(row.get("warmth"), 0.0, 1.0, 0.45),
                "attitude_stance": str(row.get("attitude_stance") or "neutral").strip().lower(),
                "call_name": str(row.get("call_name") or "").strip()[:24],
            }
        return out


__all__ = ["PersonaAdapter", "os"]
