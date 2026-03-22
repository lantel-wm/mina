package mina.capability;

import carpet.CarpetServer;
import carpet.CarpetSettings;
import carpet.helpers.HopperCounter;
import carpet.logging.LoggerRegistry;
import carpet.patches.EntityPlayerMPFake;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.DyeColor;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class DefaultCarpetObservationBackend implements CarpetObservationBackend {
    private final boolean available;

    public DefaultCarpetObservationBackend(boolean available) {
        this.available = available;
    }

    @Override
    public boolean isAvailable() {
        return available;
    }

    @Override
    public Map<String, Object> readRules(ServerPlayerEntity player) {
        if (!available) {
            return Map.of("carpet_loaded", false);
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("carpet_loaded", true);
        payload.put("hopper_counters", CarpetSettings.hopperCounters);
        payload.put("command_log", CarpetSettings.commandLog);
        payload.put("command_script", CarpetSettings.commandScript);
        payload.put("command_script_ace", CarpetSettings.commandScriptACE);
        payload.put("scripts_autoload", CarpetSettings.scriptsAutoload);
        payload.put("scripts_debugging", CarpetSettings.scriptsDebugging);
        payload.put("command_player", CarpetSettings.commandPlayer);
        payload.put("allow_listing_fake_players", CarpetSettings.allowListingFakePlayers);
        return payload;
    }

    @Override
    public Map<String, Object> readObservability(ServerPlayerEntity player) {
        if (!available) {
            return Map.of("carpet_loaded", false);
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("carpet_loaded", true);
        payload.put("logger_names", new ArrayList<>(LoggerRegistry.getLoggerNames()));
        payload.put("script_server_running", CarpetServer.scriptServer != null);
        payload.put("hopper_counters_enabled", CarpetSettings.hopperCounters);

        List<Map<String, Object>> counters = new ArrayList<>();
        for (DyeColor color : DyeColor.values()) {
            HopperCounter counter = HopperCounter.getCounter(color);
            if (counter == null || counter.getTotalItems() <= 0) {
                continue;
            }
            counters.add(Map.of(
                    "color", color.getId(),
                    "total_items", counter.getTotalItems()
            ));
        }
        payload.put("active_hopper_counters", counters);
        return payload;
    }

    @Override
    public Map<String, Object> readFakePlayers(ServerPlayerEntity player) {
        if (!available) {
            return Map.of("carpet_loaded", false);
        }

        List<Map<String, Object>> fakePlayers = new ArrayList<>();
        for (ServerPlayerEntity onlinePlayer : player.getEntityWorld().getServer().getPlayerManager().getPlayerList()) {
            if (!(onlinePlayer instanceof EntityPlayerMPFake fakePlayer)) {
                continue;
            }
            fakePlayers.add(Map.of(
                    "name", fakePlayer.getName().getString(),
                    "uuid", fakePlayer.getUuidAsString(),
                    "dimension", fakePlayer.getEntityWorld().getRegistryKey().getValue().toString(),
                    "position", mina.context.GameContextCollector.positionMap(fakePlayer),
                    "shadow", fakePlayer.isAShadow
            ));
        }
        return Map.of(
                "carpet_loaded", true,
                "count", fakePlayers.size(),
                "fake_players", fakePlayers
        );
    }

    @Override
    public Map<String, Object> ambientSnapshot(ServerPlayerEntity player) {
        if (!available) {
            return Map.of("carpet_loaded", false);
        }
        Map<String, Object> rules = readRules(player);
        Map<String, Object> observability = readObservability(player);
        Map<String, Object> fakePlayers = readFakePlayers(player);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("carpet_loaded", true);
        payload.put("script_server_running", observability.get("script_server_running"));
        payload.put("hopper_counters_enabled", rules.get("hopper_counters"));
        payload.put("logger_count", ((List<?>) observability.getOrDefault("logger_names", List.of())).size());
        payload.put("fake_player_count", fakePlayers.get("count"));
        return payload;
    }
}
