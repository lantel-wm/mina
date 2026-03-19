package mina.policy;

import mina.config.MinaConfig;
import net.minecraft.server.network.ServerPlayerEntity;

public final class PermissionResolver {
    private final MinaConfig config;

    public PermissionResolver(MinaConfig config) {
        this.config = config;
    }

    public PlayerRole resolveRole(ServerPlayerEntity player) {
        PlayerRole override = config.roleOverrides().get(player.getUuid());
        if (override != null) {
            return override;
        }

        if (config.enableExperimentalCapabilities() && player.getEntityWorld().getServer().getPlayerManager().isOperator(player.getPlayerConfigEntry())) {
            return PlayerRole.EXPERIMENTAL;
        }

        if (player.getEntityWorld().getServer().getPlayerManager().isOperator(player.getPlayerConfigEntry())) {
            return PlayerRole.ADMIN;
        }

        return PlayerRole.READ_ONLY;
    }

    public boolean allowsRisk(PlayerRole role, RiskClass riskClass) {
        return switch (role) {
            case CONVERSATION -> riskClass == RiskClass.REPLY_ONLY;
            case READ_ONLY -> riskClass == RiskClass.REPLY_ONLY || riskClass == RiskClass.READ_ONLY;
            case LOW_RISK -> riskClass != RiskClass.ADMIN_MUTATION && riskClass != RiskClass.EXPERIMENTAL_PRIVILEGED;
            case ADMIN -> riskClass != RiskClass.EXPERIMENTAL_PRIVILEGED;
            case EXPERIMENTAL -> true;
        };
    }
}
