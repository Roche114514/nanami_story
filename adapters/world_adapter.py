"""世界层适配器：仅通过 WorldBridge 注册表与通用协议 dispatch 交互，零直接代码导入。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class WorldAdapter:
    """面向 `nanami_world` `WorldBridge` 的解耦协议适配器。"""

    def __init__(
        self,
        bridge: Any = None,
        *,
        bridge_provider: Callable[[], Any] | None = None,
        bridge_key: str = "default",
    ) -> None:
        self._bridge = bridge
        self._bridge_provider = bridge_provider
        self._bridge_key = str(bridge_key or "default").strip() or "default"

    def resolve_bridge(self) -> Any:
        if self._bridge is not None:
            return self._bridge
        if self._bridge_provider is not None:
            try:
                provided = self._bridge_provider()
                if provided is not None:
                    return provided
            except Exception:
                return None
        mod = sys.modules.get("plugins.nanami_world.external_bridge")
        if mod is None:
            return None
        getter = getattr(mod, "get_world_bridge", None)
        if callable(getter):
            return getter(self._bridge_key)
        return None

    def available(self) -> bool:
        return self.resolve_bridge() is not None

    def dispatch(self, op: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        bridge = self.resolve_bridge()
        if bridge is None:
            raise RuntimeError("文字世界桥接器（WorldBridge）尚未就绪。")
        return dict(bridge.dispatch(op, dict(args or {})))

    def get_capabilities(self) -> dict[str, Any]:
        return self.dispatch("get_capabilities")

    def create_world(self, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("create_world", kwargs)

    def set_world_flags(self, space_id: str, **flags: Any) -> dict[str, Any]:
        return self.dispatch("set_world_flags", {"space_id": space_id, **flags})

    def delete_world(self, space_id: str, *, owned_actor_ids: list[str] | None = None) -> dict[str, Any]:
        return self.dispatch("delete_world", {"space_id": space_id, "owned_actor_ids": owned_actor_ids or []})

    def switch_world(self, space_id: str) -> dict[str, Any]:
        return self.dispatch("switch_world", {"space_id": space_id})

    def ensure_player(self, space_id: str, *, name: str = "你", x: int | None = None, y: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"space_id": space_id, "name": name}
        if x is not None and y is not None:
            payload["x"] = x
            payload["y"] = y
        return self.dispatch("ensure_player", payload)

    def upsert_actor(self, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("upsert_actor", kwargs)

    def remove_actor(self, actor_id: str) -> dict[str, Any]:
        return self.dispatch("remove_actor", {"actor_id": actor_id})

    def set_actor_status(self, actor_id: str, status: str, *, status_note: str = "") -> dict[str, Any]:
        return self.dispatch(
            "set_actor_status",
            {"actor_id": actor_id, "status": status, "status_note": status_note},
        )

    def set_actor_relation(
        self,
        space_id: str,
        subject_character: str,
        target_character: str,
        patch: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.dispatch(
            "set_actor_relation",
            {
                "space_id": space_id,
                "subject_character": subject_character,
                "target_character": target_character,
                "patch": dict(patch),
            },
        )

    def get_actor_relations(
        self,
        space_id: str,
        subject_character: str,
        *,
        known_actors: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        args: dict[str, Any] = {
            "space_id": space_id,
            "subject_character": subject_character,
        }
        if known_actors is not None:
            args["known_actors"] = [str(x) for x in known_actors if str(x).strip()]
        res = self.dispatch("get_actor_relations", args)
        rels = res.get("relations")
        return dict(rels) if isinstance(rels, Mapping) else {}

    def seed_initial_relations(
        self,
        space_id: str,
        subjects: Sequence[str],
        *,
        source_space_id: str = "auto",
        include_user: bool = True,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        res = self.dispatch(
            "seed_initial_relations",
            {
                "space_id": space_id,
                "subjects": [str(s) for s in subjects if str(s).strip()],
                "source_space_id": str(source_space_id or "auto").strip() or "auto",
                "include_user": bool(include_user),
            },
        )
        seeded = res.get("seeded")
        return dict(seeded) if isinstance(seeded, Mapping) else {}

    def place_entity(self, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("place_entity", kwargs)

    def synthesize_entity(self, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("synthesize_entity", kwargs)

    def synthesize_actor_sprite(self, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("synthesize_actor_sprite", kwargs)

    def set_private_brief(
        self,
        actor_id: str,
        brief: str,
        *,
        cover_goal: str = "",
        space_id: str = "",
        night_active: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "actor_id": actor_id,
            "brief": brief,
            "cover_goal": cover_goal,
            "space_id": space_id,
        }
        if night_active is not None:
            payload["night_active"] = bool(night_active)
        return self.dispatch("set_private_brief", payload)

    def clear_private_brief(self, actor_id: str, *, space_id: str = "") -> dict[str, Any]:
        return self.dispatch("clear_private_brief", {"actor_id": actor_id, "space_id": space_id})

    def set_covert_directive(
        self,
        actor_id: str,
        directive: Mapping[str, Any],
        *,
        space_id: str = "",
    ) -> dict[str, Any]:
        return self.dispatch(
            "set_covert_directive",
            {"actor_id": actor_id, "directive": dict(directive), "space_id": space_id},
        )

    def clear_covert_directive(
        self,
        actor_id: str,
        *,
        directive_id: str = "",
        space_id: str = "",
    ) -> dict[str, Any]:
        return self.dispatch(
            "clear_covert_directive",
            {"actor_id": actor_id, "directive_id": directive_id, "space_id": space_id},
        )

    def dispatch_npc_to_player(
        self,
        actor_id: str,
        *,
        player_id: str = "",
        reason: str = "",
        space_id: str = "",
        preempt: bool = True,
    ) -> dict[str, Any]:
        return self.dispatch(
            "dispatch_npc_to_player",
            {
                "actor_id": actor_id,
                "player_id": player_id,
                "reason": reason,
                "space_id": space_id,
                "preempt": preempt,
            },
        )

    def resume_resting_group(self, space_id: str, actor_ids: list[str]) -> dict[str, Any]:
        return self.dispatch("resume_resting_group", {"space_id": space_id, "actor_ids": actor_ids})

    def set_background_roster(self, space_id: str, actor_ids: list[str]) -> dict[str, Any]:
        return self.dispatch("set_background_roster", {"space_id": space_id, "actor_ids": actor_ids})

    def gather_actors(self, space_id: str, actor_ids: list[str]) -> dict[str, Any]:
        return self.dispatch("gather_actors", {"space_id": space_id, "actor_ids": actor_ids})

    def set_dialogue_focus(self, space_id: str, scenes: list[dict[str, Any]]) -> dict[str, Any]:
        return self.dispatch("set_dialogue_focus", {"space_id": space_id, "scenes": scenes})

    def set_overlay(self, space_id: str, overlay: Mapping[str, Any] | None) -> dict[str, Any]:
        if overlay is None:
            return self.dispatch("set_overlay", {"space_id": space_id, "clear": True})
        return self.dispatch("set_overlay", {"space_id": space_id, "overlay": dict(overlay)})

    def register_overlay_handler(self, space_id: str, handler: Callable[[dict[str, Any]], Any]) -> bool:
        bridge = self.resolve_bridge()
        if bridge is None or not hasattr(bridge, "register_overlay_handler"):
            return False
        bridge.register_overlay_handler(space_id, handler)
        return True

    def unregister_overlay_handler(self, space_id: str) -> None:
        bridge = self.resolve_bridge()
        if bridge is not None and hasattr(bridge, "unregister_overlay_handler"):
            bridge.unregister_overlay_handler(space_id)

    def poll_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        realm_id: str = "",
        kinds: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "after_seq": int(after_seq),
            "limit": int(limit),
            "realm_id": realm_id,
        }
        if kinds is not None:
            args["kinds"] = list(kinds)
        return self.dispatch("poll_events", args)

    def query_world_state(self, space_id: str = "") -> dict[str, Any]:
        return self.dispatch("query_world_state", {"space_id": space_id, "compact": True})

    def list_realms(self) -> dict[str, Any]:
        return self.dispatch("list_realms")

    def propose_actor_commitment(
        self,
        actor_id: str,
        commitment: Mapping[str, Any],
        *,
        space_id: str = "",
        preempt: bool = False,
    ) -> dict[str, Any]:
        return self.dispatch(
            "propose_actor_commitment",
            {
                "actor_id": actor_id,
                "commitment": dict(commitment),
                "space_id": space_id,
                "preempt": bool(preempt),
            },
        )

    def emit_ambient_cue(
        self,
        space_id: str,
        text: str,
        *,
        zone_id: str = "",
        audible_adjacent_zones: bool = True,
        cue_kind: str = "sound",
    ) -> dict[str, Any]:
        return self.dispatch(
            "emit_ambient_cue",
            {
                "space_id": space_id,
                "text": text,
                "zone_id": zone_id,
                "audible_adjacent_zones": bool(audible_adjacent_zones),
                "cue_kind": cue_kind,
            },
        )

    def set_world_environment(
        self,
        space_id: str,
        *,
        weather_mode: str = "",
        intensity: float | None = None,
        entity_updates: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"space_id": space_id}
        if weather_mode:
            payload["weather_mode"] = weather_mode
        if intensity is not None:
            payload["intensity"] = float(intensity)
        if entity_updates:
            payload["entity_updates"] = [dict(row) for row in entity_updates]
        return self.dispatch("set_world_environment", payload)

    def list_blueprints(self) -> dict[str, dict[str, Any]]:
        res = self.dispatch("list_blueprints")
        bps = res.get("blueprints")
        return dict(bps) if isinstance(bps, Mapping) else {}
