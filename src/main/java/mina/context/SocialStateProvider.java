package mina.context;

import net.minecraft.server.network.ServerPlayerEntity;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class SocialStateProvider {
    private final int nearbyRadius;

    public SocialStateProvider(int nearbyRadius) {
        this.nearbyRadius = Math.max(8, nearbyRadius);
    }

    public Map<String, Object> collect(ServerPlayerEntity player, RecentEventTracker recentEventTracker) {
        List<ServerPlayerEntity> nearby = player.getEntityWorld().getPlayers(other ->
                !other.getUuid().equals(player.getUuid())
                        && other.squaredDistanceTo(player) <= (double) nearbyRadius * nearbyRadius
        );
        nearby.sort(Comparator.comparingDouble(other -> other.squaredDistanceTo(player)));

        List<Map<String, Object>> nearbyPlayers = new ArrayList<>();
        List<Map<String, Object>> companions = new ArrayList<>();
        for (ServerPlayerEntity other : nearby) {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("name", other.getName().getString());
            payload.put("uuid", other.getUuidAsString());
            payload.put("distance", Math.sqrt(other.squaredDistanceTo(player)));
            payload.put("position", GameContextCollector.positionMap(other));
            nearbyPlayers.add(payload);
            if (other.squaredDistanceTo(player) <= 16.0 * 16.0) {
                companions.add(payload);
            }
        }

        Map<String, Object> damageState = recentEventTracker.recentDamageState(player);
        boolean isAlone = nearbyPlayers.isEmpty();
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("nearby_players", nearbyPlayers);
        payload.put("companions", companions);
        payload.put("is_alone", isAlone);
        payload.put("alone_duration_ticks", damageState.get("alone_duration_ticks"));
        payload.put(
                "party_summary",
                isAlone
                        ? "The player is currently moving alone."
                        : "The player currently has %d nearby companions.".formatted(nearbyPlayers.size())
        );
        return payload;
    }
}
