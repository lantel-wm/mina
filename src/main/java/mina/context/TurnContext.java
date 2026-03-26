package mina.context;

import mina.bridge.AppServerModels;
import mina.capability.CapabilityDefinition;
import mina.policy.PlayerRole;

import java.util.List;
import java.util.Map;

public record TurnContext(
        String sessionRef,
        PlayerRole role,
        AppServerModels.PlayerPayload playerPayload,
        AppServerModels.ServerEnvPayload serverEnvPayload,
        Map<String, Object> scopedSnapshot,
        List<CapabilityDefinition> visibleCapabilities,
        String stateFingerprint
) {
}
