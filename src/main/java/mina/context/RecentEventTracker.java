package mina.context;

import net.minecraft.entity.Entity;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.ItemEntity;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.TntEntity;
import net.minecraft.entity.damage.DamageSource;
import net.minecraft.entity.mob.HostileEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;

public final class RecentEventTracker {
    private final int capacity;
    private final int entityScanIntervalTicks;
    private final int longDangerThresholdTicks;
    private final Map<UUID, PlayerPulseState> playerStates = new java.util.HashMap<>();
    private final Deque<TrackedEvent> events = new ArrayDeque<>();
    private long currentTick = 0L;

    public RecentEventTracker(int capacity, int entityScanIntervalTicks, int longDangerThresholdTicks) {
        this.capacity = Math.max(1, capacity);
        this.entityScanIntervalTicks = Math.max(1, entityScanIntervalTicks);
        this.longDangerThresholdTicks = Math.max(20, longDangerThresholdTicks);
    }

    public synchronized void recordPlayerEvent(String kind, ServerPlayerEntity player, Map<String, Object> payload) {
        record(kind, player, payload, importanceForKind(kind));
    }

    public synchronized void onPlayerJoin(ServerPlayerEntity player) {
        PlayerPulseState state = playerState(player);
        state.lastKnownDimension = player.getEntityWorld().getRegistryKey().getValue().toString();
        record("player_joined", player, Map.of(), "medium");
    }

    public synchronized void onPlayerLeave(ServerPlayerEntity player) {
        record("player_left", player, Map.of(), "medium");
        playerStates.remove(player.getUuid());
    }

    public synchronized void onPlayerRespawn(ServerPlayerEntity oldPlayer, ServerPlayerEntity newPlayer, boolean alive) {
        PlayerPulseState state = playerState(newPlayer);
        state.lastRespawnTick = currentTick;
        state.lastKnownDimension = newPlayer.getEntityWorld().getRegistryKey().getValue().toString();
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("alive", alive);
        payload.put("old_dimension", oldPlayer.getEntityWorld().getRegistryKey().getValue().toString());
        payload.put("new_dimension", newPlayer.getEntityWorld().getRegistryKey().getValue().toString());
        record("player_respawned", newPlayer, payload, "high");
    }

    public synchronized void onPlayerAfterDamage(
            ServerPlayerEntity player,
            DamageSource source,
            float baseDamageTaken,
            float damageTaken,
            boolean blocked
    ) {
        PlayerPulseState state = playerState(player);
        state.lastDamageTick = currentTick;
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("damage_source", source.getName());
        payload.put("base_damage", baseDamageTaken);
        payload.put("damage_taken", damageTaken);
        payload.put("blocked", blocked);
        payload.put("health_after", player.getHealth());
        record("player_hurt", player, payload, damageTaken >= 6.0F ? "high" : "medium");
    }

    public synchronized void onPlayerDeath(ServerPlayerEntity player, DamageSource source) {
        PlayerPulseState state = playerState(player);
        state.lastDeathTick = currentTick;
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("damage_source", source.getName());
        payload.put("health_after", player.getHealth());
        record("player_died", player, payload, "high");
    }

    public synchronized void onPlayerChangeWorld(ServerPlayerEntity player, ServerWorld origin, ServerWorld destination) {
        PlayerPulseState state = playerState(player);
        state.lastDimensionChangeTick = currentTick;
        state.lastKnownDimension = destination.getRegistryKey().getValue().toString();
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("from_dimension", origin.getRegistryKey().getValue().toString());
        payload.put("to_dimension", destination.getRegistryKey().getValue().toString());
        record("player_changed_dimension", player, payload, "high");
    }

    public synchronized void onPlayerKilledEntity(ServerPlayerEntity player, LivingEntity killedEntity, DamageSource source) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("entity_id", Registries.ENTITY_TYPE.getId(killedEntity.getType()).toString());
        payload.put("entity_name", killedEntity.getName().getString());
        payload.put("damage_source", source.getName());
        record(
                "player_killed_important_enemy",
                player,
                payload,
                killedEntity instanceof HostileEntity ? "medium" : "low"
        );
    }

    public synchronized void onPlayerEquipmentChange(
            ServerPlayerEntity player,
            EquipmentSlot slot,
            ItemStack previousStack,
            ItemStack currentStack
    ) {
        PlayerPulseState state = playerState(player);
        state.lastEquipmentChangeTick = currentTick;
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("slot", slot.asString());
        payload.put("previous", GameContextCollector.stackMap(previousStack));
        payload.put("current", GameContextCollector.stackMap(currentStack));
        record("player_equipment_changed", player, payload, "low");
    }

    public synchronized void onEntityLoad(Entity entity, ServerWorld world) {
        if (!(entity instanceof TntEntity) && !(entity instanceof ItemEntity) && !(entity instanceof HostileEntity)) {
            return;
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("entity_id", Registries.ENTITY_TYPE.getId(entity.getType()).toString());
        payload.put("entity_name", entity.getName().getString());
        payload.put("dimension", world.getRegistryKey().getValue().toString());
        payload.put("position", GameContextCollector.vectorMap(new Vec3d(entity.getX(), entity.getY(), entity.getZ())));
        recordGlobal("entity_loaded", payload, entity instanceof TntEntity ? "medium" : "low");
    }

    public synchronized void onEntityUnload(Entity entity, ServerWorld world) {
        if (!(entity instanceof TntEntity) && !(entity instanceof HostileEntity)) {
            return;
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("entity_id", Registries.ENTITY_TYPE.getId(entity.getType()).toString());
        payload.put("entity_name", entity.getName().getString());
        payload.put("dimension", world.getRegistryKey().getValue().toString());
        recordGlobal("entity_unloaded", payload, "low");
    }

    public synchronized void onServerTick(MinecraftServer server) {
        currentTick++;
        if (currentTick % entityScanIntervalTicks != 0) {
            return;
        }

        for (ServerPlayerEntity player : server.getPlayerManager().getPlayerList()) {
            PlayerPulseState state = playerState(player);
            boolean inDanger = isInImmediateDanger(player);
            if (inDanger) {
                if (state.dangerStartTick < 0) {
                    state.dangerStartTick = currentTick;
                }
                if (currentTick - state.dangerStartTick >= longDangerThresholdTicks
                        && currentTick - state.lastDangerAlertTick >= longDangerThresholdTicks) {
                    state.lastDangerAlertTick = currentTick;
                    Map<String, Object> payload = new LinkedHashMap<>();
                    payload.put("danger_duration_ticks", currentTick - state.dangerStartTick);
                    record("player_in_danger_too_long", player, payload, "high");
                }
            } else {
                state.dangerStartTick = -1L;
            }

            boolean alone = isAlone(player);
            if (alone) {
                if (state.aloneStartTick < 0) {
                    state.aloneStartTick = currentTick;
                }
            } else {
                state.aloneStartTick = -1L;
            }
        }
    }

    public synchronized List<Map<String, Object>> collect(ServerPlayerEntity player) {
        List<Map<String, Object>> filtered = new ArrayList<>();
        String playerUuid = player.getUuidAsString();
        for (TrackedEvent event : events) {
            if (event.playerUuid != null && !Objects.equals(event.playerUuid, playerUuid)) {
                continue;
            }
            filtered.add(event.asPayload(currentTick));
        }
        return filtered;
    }

    public synchronized Map<String, Object> recentDamageState(ServerPlayerEntity player) {
        PlayerPulseState state = playerState(player);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("recently_hurt", state.lastDamageTick >= 0 && currentTick - state.lastDamageTick <= 100);
        payload.put("recently_died", state.lastDeathTick >= 0 && currentTick - state.lastDeathTick <= 200);
        payload.put("recently_respawned", state.lastRespawnTick >= 0 && currentTick - state.lastRespawnTick <= 200);
        payload.put("danger_duration_ticks", state.dangerStartTick < 0 ? 0 : currentTick - state.dangerStartTick);
        payload.put("alone_duration_ticks", state.aloneStartTick < 0 ? 0 : currentTick - state.aloneStartTick);
        payload.put("long_in_danger", state.dangerStartTick >= 0 && currentTick - state.dangerStartTick >= longDangerThresholdTicks);
        payload.put("long_alone", state.aloneStartTick >= 0 && currentTick - state.aloneStartTick >= longDangerThresholdTicks);
        return payload;
    }

    public synchronized void recordTurnEvent(String kind, ServerPlayerEntity player, String message) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("message", message);
        record(kind, player, payload, importanceForKind(kind));
    }

    public static String importanceForKind(String kind) {
        String normalized = String.valueOf(kind).trim().toLowerCase(Locale.ROOT);
        if (normalized.contains("died")
                || normalized.contains("respawn")
                || normalized.contains("changed_dimension")
                || normalized.contains("danger_too_long")) {
            return "high";
        }
        if (normalized.contains("hurt")
                || normalized.contains("killed")
                || normalized.contains("joined")
                || normalized.contains("left")) {
            return "medium";
        }
        return "low";
    }

    public static String stalenessForAge(Duration age) {
        long seconds = Math.max(0L, age.getSeconds());
        if (seconds <= 15L) {
            return "fresh";
        }
        if (seconds <= 120L) {
            return "recent";
        }
        return "stale";
    }

    private void record(String kind, ServerPlayerEntity player, Map<String, Object> payload, String importance) {
        Map<String, Object> eventPayload = new LinkedHashMap<>();
        eventPayload.put("player_uuid", player.getUuidAsString());
        eventPayload.put("player_name", player.getName().getString());
        eventPayload.put("dimension", player.getEntityWorld().getRegistryKey().getValue().toString());
        eventPayload.putAll(payload);
        addEvent(new TrackedEvent(kind, importance, Instant.now(), currentTick, player.getUuidAsString(), eventPayload));
    }

    private void recordGlobal(String kind, Map<String, Object> payload, String importance) {
        addEvent(new TrackedEvent(kind, importance, Instant.now(), currentTick, null, payload));
    }

    private void addEvent(TrackedEvent event) {
        events.addLast(event);
        while (events.size() > capacity) {
            events.removeFirst();
        }
    }

    private PlayerPulseState playerState(ServerPlayerEntity player) {
        return playerStates.computeIfAbsent(player.getUuid(), ignored -> new PlayerPulseState());
    }

    private boolean isInImmediateDanger(ServerPlayerEntity player) {
        if (player.getHealth() <= 8.0F || player.isOnFire() || player.isInLava()) {
            return true;
        }
        if (player.isSubmergedInWater() && player.getAir() < 60) {
            return true;
        }

        Box box = player.getBoundingBox().expand(12.0);
        return !player.getEntityWorld().getOtherEntities(
                player,
                box,
                entity -> entity.isAlive()
                        && (entity instanceof HostileEntity || entity instanceof TntEntity)
                        && entity.squaredDistanceTo(player) <= 144.0
        ).isEmpty();
    }

    private boolean isAlone(ServerPlayerEntity player) {
        return player.getEntityWorld().getPlayers(other ->
                !other.getUuid().equals(player.getUuid())
                        && other.squaredDistanceTo(player) <= (32.0 * 32.0)
        ).isEmpty();
    }

    private record TrackedEvent(
            String kind,
            String importance,
            Instant timestamp,
            long tick,
            String playerUuid,
            Map<String, Object> payload
    ) {
        private Map<String, Object> asPayload(long currentTick) {
            Map<String, Object> event = new LinkedHashMap<>();
            event.put("kind", kind);
            event.put("ts", timestamp.toString());
            event.put("importance", importance);
            event.put("staleness", stalenessForAge(Duration.between(timestamp, Instant.now())));
            event.put("age_ticks", Math.max(0L, currentTick - tick));
            event.put("payload", payload);
            return event;
        }
    }

    private static final class PlayerPulseState {
        private long lastDamageTick = -1L;
        private long lastDeathTick = -1L;
        private long lastRespawnTick = -1L;
        private long lastDimensionChangeTick = -1L;
        private long lastEquipmentChangeTick = -1L;
        private long dangerStartTick = -1L;
        private long aloneStartTick = -1L;
        private long lastDangerAlertTick = -1L;
        private String lastKnownDimension = "minecraft:overworld";
    }
}
