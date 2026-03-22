from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


SnapshotReader = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class SemanticBridgeProxySpec:
    capability_id: str
    bridge_target_id: str
    description: str
    args_schema: dict[str, Any]
    result_schema: dict[str, Any]
    domain: str
    freshness_hint: str
    snapshot_reader: SnapshotReader


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    return None


def _none(_: dict[str, Any], __: dict[str, Any]) -> dict[str, Any] | None:
    return None


def _player_snapshot(snapshot: dict[str, Any], _: dict[str, Any]) -> dict[str, Any] | None:
    return _mapping(snapshot.get("player"))


def _scene_snapshot(snapshot: dict[str, Any], _: dict[str, Any]) -> dict[str, Any] | None:
    scene = _mapping(snapshot.get("scene"))
    if not scene:
        return None
    risk_state = _mapping(snapshot.get("risk_state"))
    if risk_state and "risk_state" not in scene:
        scene["risk_state"] = risk_state
    return scene


def _inventory_snapshot(snapshot: dict[str, Any], _: dict[str, Any]) -> dict[str, Any] | None:
    player = _mapping(snapshot.get("player"))
    if not player:
        return None
    inventory = _mapping(player.get("inventory_brief"))
    if not inventory:
        return None
    hands = _mapping(player.get("hands"))
    if hands:
        inventory["hands"] = hands
    shortages = inventory.get("shortages")
    if "summary" not in inventory and isinstance(shortages, dict):
        missing = [
            name.removeprefix("needs_").replace("_", " ")
            for name, needed in shortages.items()
            if bool(needed)
        ]
        inventory["summary"] = (
            "Inventory brief is available; no obvious shortage detected."
            if not missing
            else "Inventory brief suggests missing " + ", ".join(missing) + "."
        )
    return inventory


def _social_snapshot(snapshot: dict[str, Any], _: dict[str, Any]) -> dict[str, Any] | None:
    return _mapping(snapshot.get("social"))


def _technical_snapshot(snapshot: dict[str, Any], _: dict[str, Any]) -> dict[str, Any] | None:
    return _mapping(snapshot.get("technical"))


SEMANTIC_BRIDGE_PROXY_SPECS: tuple[SemanticBridgeProxySpec, ...] = (
    SemanticBridgeProxySpec(
        capability_id="observe.player",
        bridge_target_id="world.player_state.read",
        description="Observe Mina's structured player-state view from the ambient snapshot when possible, otherwise refresh it live.",
        args_schema={},
        result_schema={"player_state": "object"},
        domain="world",
        freshness_hint="ambient",
        snapshot_reader=_player_snapshot,
    ),
    SemanticBridgeProxySpec(
        capability_id="observe.scene",
        bridge_target_id="world.scene.read",
        description="Observe the current scene, including threats, hazards, safe spots, and risk state.",
        args_schema={},
        result_schema={"scene": "object"},
        domain="world",
        freshness_hint="ambient",
        snapshot_reader=_scene_snapshot,
    ),
    SemanticBridgeProxySpec(
        capability_id="observe.inventory",
        bridge_target_id="world.inventory.read",
        description="Observe a compact inventory brief, including armor coverage, readiness, and shortages.",
        args_schema={},
        result_schema={"inventory": "object"},
        domain="world",
        freshness_hint="ambient",
        snapshot_reader=_inventory_snapshot,
    ),
    SemanticBridgeProxySpec(
        capability_id="observe.poi",
        bridge_target_id="world.poi.read",
        description="Observe nearby structures, biomes, or points of interest with a live locate-style refresh.",
        args_schema={"kind": "string", "query": "string", "radius": "integer"},
        result_schema={"poi": "object"},
        domain="world",
        freshness_hint="live",
        snapshot_reader=_none,
    ),
    SemanticBridgeProxySpec(
        capability_id="observe.social",
        bridge_target_id="world.social.read",
        description="Observe nearby players, companions, and whether the player is currently alone.",
        args_schema={},
        result_schema={"social": "object"},
        domain="world",
        freshness_hint="ambient",
        snapshot_reader=_social_snapshot,
    ),
    SemanticBridgeProxySpec(
        capability_id="observe.technical",
        bridge_target_id="carpet.observability.read",
        description="Observe structured technical-state signals such as logger visibility, script-server state, and hopper counters.",
        args_schema={},
        result_schema={"technical": "object"},
        domain="technical",
        freshness_hint="ambient",
        snapshot_reader=_technical_snapshot,
    ),
)
