package mina.context;

import net.minecraft.server.network.ServerPlayerEntity;

import java.util.LinkedHashMap;
import java.util.Map;

public final class WorldStateProvider {
    public Map<String, Object> collectWorld(ServerPlayerEntity player, String locationBucket) {
        var world = player.getEntityWorld();
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("dimension", world.getRegistryKey().getValue().toString());
        snapshot.put("time_of_day", world.getTimeOfDay());
        snapshot.put("is_day", world.isDay());
        snapshot.put("is_raining", world.isRaining());
        snapshot.put("is_thundering", world.isThundering());
        snapshot.put("weather", world.isThundering() ? "thunder" : world.isRaining() ? "rain" : "clear");
        snapshot.put("time_phase", timePhase(world.getTimeOfDay()));
        snapshot.put(
                "biome",
                world.getBiome(player.getBlockPos())
                        .getKey()
                        .map(key -> key.getValue().toString())
                        .orElse("unknown")
        );
        snapshot.put("moon_phase", (int) Math.floorMod(world.getTimeOfDay() / 24_000L, 8L));
        snapshot.put("location_bucket", locationBucket);
        return snapshot;
    }

    public static String timePhase(long timeOfDay) {
        long dayTime = Math.floorMod(timeOfDay, 24_000L);
        if (dayTime < 1_000L) {
            return "dawn";
        }
        if (dayTime < 12_000L) {
            return "day";
        }
        if (dayTime < 13_000L) {
            return "dusk";
        }
        return "night";
    }
}
