package mina.context;

import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.world.rule.GameRule;
import net.minecraft.world.rule.GameRules;

import java.util.LinkedHashMap;
import java.util.Map;

public final class WorldSnapshotProvider {
    private final int serverRuleSummaryLimit;

    public WorldSnapshotProvider(int serverRuleSummaryLimit) {
        this.serverRuleSummaryLimit = serverRuleSummaryLimit;
    }

    public Map<String, Object> collectWorld(ServerPlayerEntity player) {
        var world = player.getEntityWorld();
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("dimension", world.getRegistryKey().getValue().toString());
        snapshot.put("time_of_day", world.getTimeOfDay());
        snapshot.put("is_day", world.isDay());
        snapshot.put("is_raining", world.isRaining());
        snapshot.put("is_thundering", world.isThundering());
        snapshot.put("weather", world.isThundering() ? "thunder" : world.isRaining() ? "rain" : "clear");
        return snapshot;
    }

    public Map<String, Object> collectRuleReferences(GameRules gameRules) {
        Map<String, Object> summary = new LinkedHashMap<>();
        int remaining = serverRuleSummaryLimit;
        for (GameRule<?> rule : gameRules.streamRules().toList()) {
            if (remaining-- <= 0) {
                break;
            }
            summary.put(Registries.GAME_RULE.getId(rule).toString(), gameRules.getRuleValueName(rule));
        }
        return summary;
    }
}
