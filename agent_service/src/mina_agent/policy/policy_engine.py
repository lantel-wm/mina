from __future__ import annotations

from dataclasses import dataclass


ROLE_ORDER = {
    "conversation": 0,
    "read_only": 1,
    "low_risk": 2,
    "admin": 3,
    "experimental": 4,
}

RISK_MIN_ROLE = {
    "reply_only": "conversation",
    "read_only": "read_only",
    "world_low_risk": "low_risk",
    "admin_mutation": "admin",
    "experimental_privileged": "experimental",
}


@dataclass(slots=True)
class PolicyContext:
    role: str
    carpet_loaded: bool
    experimental_enabled: bool
    dynamic_scripting_enabled: bool


class PolicyEngine:
    def role_allows_risk(self, role: str, risk_class: str) -> bool:
        required_role = RISK_MIN_ROLE.get(risk_class, "experimental")
        return ROLE_ORDER.get(role, -1) >= ROLE_ORDER[required_role]

    def descriptor_visible(self, context: PolicyContext, visibility_predicate: str) -> bool:
        predicate = visibility_predicate.strip().lower()
        if predicate == "always":
            return True
        if predicate == "carpet_loaded":
            return context.carpet_loaded
        if predicate == "read_only_plus":
            return ROLE_ORDER.get(context.role, -1) >= ROLE_ORDER["read_only"]
        if predicate == "experimental_only":
            return context.experimental_enabled and context.role == "experimental"
        if predicate == "dynamic_scripting_enabled":
            return context.dynamic_scripting_enabled and context.role == "experimental"
        return False
