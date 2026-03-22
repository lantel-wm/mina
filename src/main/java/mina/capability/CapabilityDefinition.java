package mina.capability;

import mina.policy.PlayerRole;
import mina.policy.RiskClass;
import net.minecraft.server.network.ServerPlayerEntity;

import java.util.Map;
import java.util.function.BiPredicate;

public record CapabilityDefinition(
        String id,
        String kind,
        String description,
        RiskClass riskClass,
        String executionMode,
        boolean requiresConfirmation,
        Map<String, Object> argsSchema,
        Map<String, Object> resultSchema,
        String domain,
        boolean preferred,
        String semanticLevel,
        String freshnessHint,
        BiPredicate<ServerPlayerEntity, PlayerRole> visibility,
        CapabilityExecutor executor
) {
    public CapabilityDefinition(
            String id,
            String kind,
            String description,
            RiskClass riskClass,
            String executionMode,
            boolean requiresConfirmation,
            Map<String, Object> argsSchema,
            Map<String, Object> resultSchema,
            BiPredicate<ServerPlayerEntity, PlayerRole> visibility,
            CapabilityExecutor executor
    ) {
        this(
                id,
                kind,
                description,
                riskClass,
                executionMode,
                requiresConfirmation,
                argsSchema,
                resultSchema,
                "general",
                false,
                "raw",
                "live",
                visibility,
                executor
        );
    }

    public boolean isVisible(ServerPlayerEntity player, PlayerRole role) {
        return visibility.test(player, role);
    }

    @FunctionalInterface
    public interface CapabilityExecutor {
        CapabilityResult execute(ServerPlayerEntity player, Map<String, Object> arguments) throws Exception;
    }
}
