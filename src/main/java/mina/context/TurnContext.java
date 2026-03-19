package mina.context;

import mina.bridge.BridgeModels;
import mina.capability.CapabilityDefinition;
import mina.policy.PlayerRole;

import java.util.List;
import java.util.Map;

public record TurnContext(
        String sessionRef,
        PlayerRole role,
        BridgeModels.PlayerPayload playerPayload,
        BridgeModels.ServerEnvPayload serverEnvPayload,
        Map<String, Object> scopedSnapshot,
        List<CapabilityDefinition> visibleCapabilities,
        String stateFingerprint
) {
}
