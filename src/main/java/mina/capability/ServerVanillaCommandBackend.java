package mina.capability;

import com.mojang.datafixers.util.Pair;
import net.minecraft.entity.Entity;
import net.minecraft.nbt.NbtCompound;
import net.minecraft.registry.RegistryKeys;
import net.minecraft.registry.RegistryWrapper;
import net.minecraft.registry.entry.RegistryEntry;
import net.minecraft.registry.tag.TagKey;
import net.minecraft.scoreboard.ReadableScoreboardScore;
import net.minecraft.scoreboard.ScoreHolder;
import net.minecraft.scoreboard.ScoreboardObjective;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.world.biome.Biome;
import net.minecraft.world.poi.PointOfInterestStorage;
import net.minecraft.world.poi.PointOfInterestType;
import net.minecraft.world.poi.PointOfInterestTypes;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class ServerVanillaCommandBackend implements VanillaCommandBackend {
    @Override
    public List<Entity> selector(ServerPlayerEntity player, Map<String, Object> selectorSpec) {
        var world = player.getEntityWorld();
        double radius = number(selectorSpec.get("radius"), 24.0);
        String entityType = string(selectorSpec.get("entity_type"));
        String tag = string(selectorSpec.get("tag"));
        Box box = player.getBoundingBox().expand(radius);
        List<Entity> matches = new ArrayList<>(world.getOtherEntities(
                player,
                box,
                entity -> entity != null
                        && entity.isAlive()
                        && entity.squaredDistanceTo(player) <= radius * radius
                        && (entityType == null || entityType.equals(net.minecraft.registry.Registries.ENTITY_TYPE.getId(entity.getType()).toString()))
                        && (tag == null || entity.getCommandTags().contains(tag))
        ));
        matches.sort(java.util.Comparator.comparingDouble(entity -> entity.squaredDistanceTo(player)));
        return matches;
    }

    @Override
    public Map<String, Object> executeProbe(ServerPlayerEntity player, Map<String, Object> probeSpec) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("executing_as", player.getName().getString());
        payload.put("dimension", player.getEntityWorld().getRegistryKey().getValue().toString());
        payload.put("position", mina.context.GameContextCollector.positionMap(player));
        payload.put("probe", probeSpec);
        payload.put("supported", true);
        return payload;
    }

    @Override
    public Map<String, Object> readBlockData(ServerPlayerEntity player, BlockPos blockPos) {
        var world = player.getEntityWorld();
        var blockEntity = world.getBlockEntity(blockPos);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("block_pos", mina.context.GameContextCollector.blockPosMap(blockPos));
        if (blockEntity == null) {
            payload.put("found", false);
            return payload;
        }
        RegistryWrapper.WrapperLookup lookup = world.getRegistryManager();
        NbtCompound nbt = blockEntity.createNbtWithIdentifyingData(lookup);
        payload.put("found", true);
        payload.put("nbt", nbt.toString());
        payload.put("keys", new ArrayList<>(nbt.getKeys()));
        return payload;
    }

    @Override
    public Map<String, Object> readEntityData(ServerPlayerEntity player, Entity entity) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("entity_id", net.minecraft.registry.Registries.ENTITY_TYPE.getId(entity.getType()).toString());
        payload.put("name", entity.getName().getString());
        payload.put("tags", new ArrayList<>(entity.getCommandTags()));
        payload.put("position", mina.context.GameContextCollector.vectorMap(new net.minecraft.util.math.Vec3d(entity.getX(), entity.getY(), entity.getZ())));
        payload.put("supported", true);
        return payload;
    }

    @Override
    public Map<String, Object> readScore(ServerPlayerEntity player, String holderName, String objectiveName) {
        var scoreboard = player.getEntityWorld().getServer().getScoreboard();
        ScoreboardObjective objective = scoreboard.getNullableObjective(objectiveName);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("holder", holderName);
        payload.put("objective", objectiveName);
        if (objective == null) {
            payload.put("found", false);
            return payload;
        }
        ReadableScoreboardScore score = scoreboard.getScore(ScoreHolder.fromName(holderName), objective);
        payload.put("found", score != null);
        if (score != null) {
            payload.put("score", score.getScore());
            payload.put("locked", score.isLocked());
        }
        return payload;
    }

    @Override
    public Map<String, Object> readTags(ServerPlayerEntity player, Entity entity) {
        return Map.of(
                "entity_id", net.minecraft.registry.Registries.ENTITY_TYPE.getId(entity.getType()).toString(),
                "tags", new ArrayList<>(entity.getCommandTags())
        );
    }

    @Override
    public Map<String, Object> locate(ServerPlayerEntity player, Map<String, Object> arguments) {
        var world = player.getEntityWorld();
        String kind = string(arguments.get("kind"));
        String query = string(arguments.get("query"));
        int radius = intValue(arguments.get("radius"), 128);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("kind", kind == null ? "mixed" : kind);
        payload.put("query", query);
        payload.put("radius", radius);

        if (kind == null || "structure".equals(kind)) {
            String structureId = query == null || query.isBlank() ? "minecraft:village" : query;
            TagKey<net.minecraft.world.gen.structure.Structure> structureTag =
                    TagKey.of(RegistryKeys.STRUCTURE, Identifier.of(structureId));
            BlockPos structurePos = world.locateStructure(structureTag, player.getBlockPos(), radius, false);
            payload.put("structure", structurePos == null ? Map.of("found", false) : Map.of(
                    "found", true,
                    "pos", mina.context.GameContextCollector.blockPosMap(structurePos),
                    "tag", structureId
            ));
        }

        if (kind == null || "biome".equals(kind)) {
            String biomeQuery = query == null || query.isBlank() ? "forest" : query.toLowerCase(Locale.ROOT);
            Pair<BlockPos, RegistryEntry<Biome>> result = world.locateBiome(
                    entry -> entry.getKey().map(key -> key.getValue().toString().toLowerCase(Locale.ROOT).contains(biomeQuery)).orElse(false),
                    player.getBlockPos(),
                    radius,
                    32,
                    64
            );
            payload.put("biome", result == null ? Map.of("found", false) : Map.of(
                    "found", true,
                    "pos", mina.context.GameContextCollector.blockPosMap(result.getFirst()),
                    "biome", result.getSecond().getKey().map(key -> key.getValue().toString()).orElse("unknown")
            ));
        }

        if (kind == null || "poi".equals(kind)) {
            PointOfInterestStorage storage = world.getPointOfInterestStorage();
            var poi = storage.getNearestTypeAndPosition(
                    entry -> matchesPoiQuery(query, entry),
                    player.getBlockPos(),
                    radius,
                    PointOfInterestStorage.OccupationStatus.ANY
            );
            payload.put("poi", poi.isEmpty() ? Map.of("found", false) : Map.of(
                    "found", true,
                    "type", poi.get().getFirst().getKey().map(key -> key.getValue().toString()).orElse("unknown"),
                    "pos", mina.context.GameContextCollector.blockPosMap(poi.get().getSecond())
            ));
        }

        return payload;
    }

    private boolean matchesPoiQuery(String query, RegistryEntry<PointOfInterestType> entry) {
        if (query == null || query.isBlank()) {
            return entry.matchesKey(PointOfInterestTypes.HOME)
                    || entry.matchesKey(PointOfInterestTypes.MEETING)
                    || entry.matchesKey(PointOfInterestTypes.LODESTONE)
                    || entry.matchesKey(PointOfInterestTypes.NETHER_PORTAL);
        }
        String normalized = query.toLowerCase(Locale.ROOT);
        return entry.getKey()
                .map(key -> key.getValue().toString().toLowerCase(Locale.ROOT).contains(normalized))
                .orElse(false);
    }

    private double number(Object value, double fallback) {
        return value instanceof Number number ? number.doubleValue() : fallback;
    }

    private int intValue(Object value, int fallback) {
        return value instanceof Number number ? number.intValue() : fallback;
    }

    private String string(Object value) {
        return value instanceof String string && !string.isBlank() ? string.trim() : null;
    }
}
