package mina.context;

import net.minecraft.server.network.ServerPlayerEntity;

import java.time.Instant;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

public final class RecentEventBuffer {
    private final int capacity;
    private final Deque<Map<String, Object>> events = new ArrayDeque<>();

    public RecentEventBuffer(int capacity) {
        this.capacity = Math.max(1, capacity);
    }

    public synchronized void record(String kind, Map<String, Object> payload) {
        Map<String, Object> event = new LinkedHashMap<>();
        event.put("ts", Instant.now().toString());
        event.put("kind", kind);
        event.put("payload", payload);
        events.addLast(event);
        while (events.size() > capacity) {
            events.removeFirst();
        }
    }

    public synchronized void recordPlayerEvent(String kind, ServerPlayerEntity player, Map<String, Object> payload) {
        Map<String, Object> eventPayload = new LinkedHashMap<>();
        eventPayload.put("player_uuid", player.getUuidAsString());
        eventPayload.put("player_name", player.getName().getString());
        eventPayload.put("dimension", player.getEntityWorld().getRegistryKey().getValue().toString());
        eventPayload.putAll(payload);
        record(kind, eventPayload);
    }

    public synchronized List<Map<String, Object>> snapshot() {
        return new ArrayList<>(events);
    }

    public synchronized List<Map<String, Object>> snapshotForPlayer(String playerUuid) {
        List<Map<String, Object>> filtered = new ArrayList<>();
        for (Map<String, Object> event : events) {
            Object payload = event.get("payload");
            if (!(payload instanceof Map<?, ?> payloadMap)) {
                continue;
            }
            Object eventPlayerUuid = payloadMap.get("player_uuid");
            if (eventPlayerUuid == null || Objects.equals(String.valueOf(eventPlayerUuid), playerUuid)) {
                filtered.add(event);
            }
        }
        return filtered;
    }
}
