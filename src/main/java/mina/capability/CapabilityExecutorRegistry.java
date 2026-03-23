package mina.capability;

import carpet.utils.BlockInfo;
import carpet.utils.SpawnReporter;
import mina.config.MinaConfig;
import mina.context.GameContextCollector;
import mina.integration.carpet.CarpetCapabilitySupport;
import mina.policy.PlayerRole;
import mina.policy.RiskClass;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.block.BlockState;
import net.minecraft.entity.Entity;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.mob.HostileEntity;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.text.Text;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;
import net.minecraft.world.rule.GameRule;
import net.minecraft.world.rule.GameRules;

import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class CapabilityExecutorRegistry {
    private final MinaConfig config;
    private final DirectWorldReader directWorldReader;
    private final VanillaCommandBackend vanillaCommandBackend;
    private final CarpetObservationBackend carpetObservationBackend;
    private final Map<String, CapabilityDefinition> capabilities;
    private final boolean carpetAvailable;

    public CapabilityExecutorRegistry(
            MinaConfig config,
            DirectWorldReader directWorldReader,
            VanillaCommandBackend vanillaCommandBackend,
            CarpetObservationBackend carpetObservationBackend
    ) {
        this.config = config;
        this.directWorldReader = directWorldReader;
        this.vanillaCommandBackend = vanillaCommandBackend;
        this.carpetObservationBackend = carpetObservationBackend;
        this.carpetAvailable = FabricLoader.getInstance().isModLoaded("carpet");
        this.capabilities = buildCapabilities();
    }

    public boolean isCarpetAvailable() {
        return carpetAvailable;
    }

    public Map<String, Object> ambientTechnicalSnapshot(ServerPlayerEntity player) {
        return directWorldReader.readAmbientTechnicalState(player);
    }

    public List<CapabilityDefinition> visibleCapabilities(ServerPlayerEntity player, PlayerRole role) {
        List<CapabilityDefinition> visible = new ArrayList<>();
        for (CapabilityDefinition definition : capabilities.values()) {
            if (definition.isVisible(player, role)) {
                visible.add(definition);
            }
        }
        return List.copyOf(visible);
    }

    public CapabilityDefinition definition(String capabilityId) {
        return capabilities.get(capabilityId);
    }

    public CapabilityResult execute(String capabilityId, ServerPlayerEntity player, Map<String, Object> arguments) throws Exception {
        CapabilityDefinition definition = definition(capabilityId);
        if (definition == null) {
            throw new IllegalArgumentException("Unknown capability: " + capabilityId);
        }
        return definition.executor().execute(player, arguments == null ? Map.of() : arguments);
    }

    private Map<String, CapabilityDefinition> buildCapabilities() {
        Map<String, CapabilityDefinition> map = new LinkedHashMap<>();

        register(map, new CapabilityDefinition(
                "world.player_state.read",
                "tool",
                "Read Mina's structured player-state view, including survival pressure, movement flags, hands, experience, and inventory shortages.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("player", "object"),
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readPlayerState(player), "Read structured player state.")
        ));
        register(map, new CapabilityDefinition(
                "world.scene.read",
                "tool",
                "Read Mina's structured scene summary, including location kind, biome, threats, hazards, safe spots, and current risk.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("scene", "object"),
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readScene(player), "Read structured scene state.")
        ));
        register(map, new CapabilityDefinition(
                "world.threats.read",
                "tool",
                "Read nearby entity threats with Mina's hostile, explosive, and direction-aware threat summary.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("threats", "object"),
                "world",
                true,
                "semantic",
                "live",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readThreats(player), "Read nearby threat summary.")
        ));
        register(map, new CapabilityDefinition(
                "world.environment.read",
                "tool",
                "Read Mina's environment summary, including location kind, biome, terrain safety, nearby hazards, and safe spots.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("environment", "object"),
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readEnvironment(player), "Read environment summary.")
        ));
        register(map, new CapabilityDefinition(
                "world.interactables.read",
                "tool",
                "Read nearby containers, workstations, shelter markers, and other important utility blocks around the player.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("interactables", "object"),
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readInteractables(player), "Read nearby interactables.")
        ));
        register(map, new CapabilityDefinition(
                "world.inventory.read",
                "tool",
                "Read Mina's inventory brief, including armor, shortages, and readiness for exploration.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("inventory", "object"),
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readInventory(player), "Read inventory brief.")
        ));
        register(map, new CapabilityDefinition(
                "world.poi.read",
                "tool",
                "Locate nearby structures, biomes, or points of interest in a typed, structured format.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(
                        "kind", "string",
                        "query", "string",
                        "radius", "integer"
                ),
                Map.of("poi", "object"),
                "world",
                true,
                "semantic",
                "live",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readPoi(player, arguments), "Located nearby points of interest.")
        ));
        register(map, new CapabilityDefinition(
                "world.social.read",
                "tool",
                "Read nearby players and companionship context, including whether the player is currently alone.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("social", "object"),
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readSocial(player), "Read nearby social context.")
        ));
        register(map, new CapabilityDefinition(
                "world.events.read",
                "tool",
                "Read recent important continuity events, including damage, death, respawn, dimension changes, and nearby notable events.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("recent_events", "array<object>"),
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(directWorldReader.readEvents(player), "Read recent world events.")
        ));
        register(map, new CapabilityDefinition(
                "carpet.rules.read",
                "tool",
                "Read a structured summary of important Carpet and Scarpet rule toggles.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("rules", "object"),
                "technical",
                true,
                "diagnostic",
                "live",
                (player, role) -> carpetAvailable,
                (player, arguments) -> new CapabilityResult(carpetObservationBackend.readRules(player), "Read Carpet rules.")
        ));
        register(map, new CapabilityDefinition(
                "carpet.observability.read",
                "tool",
                "Read structured Carpet observability state, including loggers, script-server visibility, and hopper counters.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("technical", "object"),
                "technical",
                true,
                "diagnostic",
                "live",
                (player, role) -> carpetAvailable,
                (player, arguments) -> new CapabilityResult(carpetObservationBackend.readObservability(player), "Read Carpet observability state.")
        ));
        register(map, new CapabilityDefinition(
                "carpet.fake_player.read",
                "tool",
                "Read the current fake-player roster and their basic in-world status.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of("fake_players", "array<object>"),
                "technical",
                true,
                "diagnostic",
                "live",
                (player, role) -> carpetAvailable,
                (player, arguments) -> new CapabilityResult(carpetObservationBackend.readFakePlayers(player), "Read Carpet fake players.")
        ));

        register(map, new CapabilityDefinition(
                "game.player_snapshot.read",
                "tool",
                "Read the current player's server-side snapshot, including health, hunger, hands, position, and a small inventory summary.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of(),
                "world",
                false,
                "raw",
                "live",
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(collectPlayerSnapshot(player), "Read current player snapshot.")
        ));
        register(map, new CapabilityDefinition(
                "game.nearby_entities.read",
                "tool",
                "List nearby entities around the player within a radius, optionally filtered by a category such as monster, hostile, living, player, or a specific entity id.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(
                        "radius", "number",
                        "entity_type", "string",
                        "limit", "integer"
                ),
                Map.of(
                        "radius", "number",
                        "filter", "string",
                        "count", "integer",
                        "entities", "array<object>",
                        "summary", "string"
                ),
                "world",
                false,
                "raw",
                "live",
                (player, role) -> true,
                this::executeNearbyEntitiesRead
        ));
        register(map, new CapabilityDefinition(
                "game.target_block.read",
                "tool",
                "Inspect the block the player is currently targeting, or a supplied block position if present.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                blockPosArgSchema("Inspect an explicit block position instead of the live target."),
                Map.of(
                        "target_found", "boolean",
                        "pos", "object{x,y,z}",
                        "block_id", "string",
                        "block_name", "string",
                        "is_air", "boolean",
                        "luminance", "integer"
                ),
                "world",
                false,
                "raw",
                "live",
                (player, role) -> true,
                this::executeTargetBlockRead
        ));
        register(map, new CapabilityDefinition(
                "server.rules.read",
                "tool",
                "Read a summary of current gamerules plus server.properties when available.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of(),
                "server",
                false,
                "diagnostic",
                "live",
                (player, role) -> true,
                (player, arguments) -> executeServerRulesRead(player)
        ));
        register(map, new CapabilityDefinition(
                "carpet.block_info.read",
                "tool",
                "Read Carpet block diagnostics for the targeted block or a supplied block position.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                blockPosArgSchema("Inspect Carpet diagnostics for an explicit block position."),
                Map.of(
                        "pos", "object{x,y,z}",
                        "lines", "array<string>",
                        "summary", "string"
                ),
                "technical",
                true,
                "diagnostic",
                "live",
                (player, role) -> carpetAvailable,
                this::executeCarpetBlockInfo
        ));
        register(map, new CapabilityDefinition(
                "carpet.distance.measure",
                "tool",
                "Measure distance between two positions using structured metrics instead of raw Carpet commands.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(
                        "from", "object{x,y,z}",
                        "to", "object{x,y,z}"
                ),
                Map.of(),
                "technical",
                true,
                "raw",
                "live",
                (player, role) -> carpetAvailable,
                this::executeDistanceMeasure
        ));
        register(map, new CapabilityDefinition(
                "carpet.mobcaps.read",
                "tool",
                "Read Carpet's current mobcap report for the player's dimension.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of(),
                "technical",
                false,
                "diagnostic",
                "live",
                (player, role) -> carpetAvailable,
                (player, arguments) -> executeMobcapsRead(player)
        ));
        return Map.copyOf(map);
    }

    private void register(Map<String, CapabilityDefinition> map, CapabilityDefinition definition) {
        map.put(definition.id(), definition);
    }

    private Map<String, Object> blockPosArgSchema(String description) {
        return Map.of(
                "block_pos",
                Map.of(
                        "type", "object",
                        "required", false,
                        "description", description,
                        "fields",
                        Map.of(
                                "x", "integer",
                                "y", "integer",
                                "z", "integer"
                        )
                )
        );
    }

    private CapabilityResult executeTargetBlockRead(ServerPlayerEntity player, Map<String, Object> arguments) {
        BlockPos blockPos = CarpetCapabilitySupport.parseBlockPos(arguments.get("block_pos"));
        if (blockPos == null) {
            BlockHitResult hitResult = CarpetCapabilitySupport.raycast(player, config.targetReachBlocks());
            if (hitResult == null || hitResult.getType() != HitResult.Type.BLOCK) {
                return new CapabilityResult(
                        Map.of("target_found", false),
                        "No target block found in reach."
                );
            }
            blockPos = hitResult.getBlockPos();
        }

        ServerWorld world = player.getEntityWorld();
        BlockState blockState = world.getBlockState(blockPos);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("target_found", true);
        payload.put("pos", GameContextCollector.blockPosMap(blockPos));
        payload.put("block_id", Registries.BLOCK.getId(blockState.getBlock()).toString());
        payload.put("block_name", blockState.getBlock().getName().getString());
        payload.put("is_air", blockState.isAir());
        payload.put("luminance", blockState.getLuminance());
        payload.put("block_data", vanillaCommandBackend.readBlockData(player, blockPos));
        return new CapabilityResult(payload, "Read targeted block state.");
    }

    private CapabilityResult executeServerRulesRead(ServerPlayerEntity player) {
        Map<String, Object> payload = new LinkedHashMap<>();
        ServerWorld world = player.getEntityWorld();
        GameRules gameRules = world.getGameRules();
        Map<String, Object> gamerules = new LinkedHashMap<>();
        for (GameRule<?> rule : gameRules.streamRules().toList()) {
            gamerules.put(Registries.GAME_RULE.getId(rule).toString(), gameRules.getRuleValueName(rule));
        }
        payload.put("gamerules", gamerules);

        var serverPropertiesPath = world.getServer().getRunDirectory().resolve("server.properties");
        if (Files.exists(serverPropertiesPath)) {
            payload.put("server_properties_path", serverPropertiesPath.toString());
            payload.put("server_properties", CarpetCapabilitySupport.readPropertiesFile(serverPropertiesPath));
        }

        return new CapabilityResult(payload, "Read gamerules and server properties.");
    }

    private CapabilityResult executeCarpetBlockInfo(ServerPlayerEntity player, Map<String, Object> arguments) {
        BlockPos blockPos = CarpetCapabilitySupport.parseBlockPos(arguments.get("block_pos"));
        if (blockPos == null) {
            BlockHitResult hitResult = CarpetCapabilitySupport.raycast(player, config.targetReachBlocks());
            if (hitResult == null || hitResult.getType() != HitResult.Type.BLOCK) {
                return new CapabilityResult(
                        Map.of("target_found", false),
                        "Carpet block info unavailable because no target block was found."
                );
            }
            blockPos = hitResult.getBlockPos();
        }

        List<Text> lines = BlockInfo.blockInfo(blockPos, player.getEntityWorld());
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("pos", GameContextCollector.blockPosMap(blockPos));
        payload.put("lines", CarpetCapabilitySupport.textLines(lines));
        payload.put("summary", CarpetCapabilitySupport.joinLines(lines));
        return new CapabilityResult(payload, "Read Carpet block info.");
    }

    private CapabilityResult executeDistanceMeasure(ServerPlayerEntity player, Map<String, Object> arguments) {
        Vec3d from = CarpetCapabilitySupport.parseVec(arguments.get("from"));
        Vec3d to = CarpetCapabilitySupport.parseVec(arguments.get("to"));

        if (from == null) {
            from = new Vec3d(player.getX(), player.getY(), player.getZ());
        }

        if (to == null) {
            BlockHitResult hitResult = CarpetCapabilitySupport.raycast(player, config.targetReachBlocks());
            if (hitResult != null && hitResult.getType() == HitResult.Type.BLOCK) {
                to = Vec3d.ofCenter(hitResult.getBlockPos());
            }
        }

        if (to == null) {
            return new CapabilityResult(Map.of("measured", false), "No destination position available for distance measurement.");
        }

        double dx = Math.abs(from.x - to.x);
        double dy = Math.abs(from.y - to.y);
        double dz = Math.abs(from.z - to.z);

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("measured", true);
        payload.put("from", GameContextCollector.vectorMap(from));
        payload.put("to", GameContextCollector.vectorMap(to));
        payload.put("delta", Map.of("x", dx, "y", dy, "z", dz));
        payload.put("spherical", Math.sqrt((dx * dx) + (dy * dy) + (dz * dz)));
        payload.put("cylindrical", Math.sqrt((dx * dx) + (dz * dz)));
        payload.put("manhattan", dx + dy + dz);
        return new CapabilityResult(payload, "Measured distance between two positions.");
    }

    private CapabilityResult executeMobcapsRead(ServerPlayerEntity player) {
        List<Text> lines = SpawnReporter.printMobcapsForDimension(player.getEntityWorld(), true);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("dimension", player.getEntityWorld().getRegistryKey().getValue().toString());
        payload.put("lines", CarpetCapabilitySupport.textLines(lines));
        payload.put("summary", CarpetCapabilitySupport.joinLines(lines));
        return new CapabilityResult(payload, "Read Carpet mobcap report.");
    }

    private CapabilityResult executeNearbyEntitiesRead(ServerPlayerEntity player, Map<String, Object> arguments) {
        double radius = clampRadius(numberArg(arguments.get("radius"), 32.0));
        int limit = clampEntityLimit(intArg(arguments.get("limit"), 24));
        String rawFilter = stringArg(arguments.get("entity_type"));
        String normalizedFilter = rawFilter == null || rawFilter.isBlank() ? "all" : rawFilter.trim().toLowerCase(Locale.ROOT);

        Vec3d origin = new Vec3d(player.getX(), player.getY(), player.getZ());
        double maxDistanceSquared = radius * radius;
        Box searchBox = player.getBoundingBox().expand(radius);

        List<Entity> matches = player.getEntityWorld().getOtherEntities(
                player,
                searchBox,
                entity -> entity != null
                        && entity.squaredDistanceTo(origin) <= maxDistanceSquared
                        && matchesEntityFilter(entity, normalizedFilter)
        );
        matches.sort(Comparator.comparingDouble(entity -> entity.squaredDistanceTo(origin)));

        List<Map<String, Object>> entities = new ArrayList<>();
        for (Entity entity : matches) {
            if (entities.size() >= limit) {
                break;
            }
            entities.add(entityPayload(origin, entity));
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("radius", radius);
        payload.put("filter", normalizedFilter);
        payload.put("count", entities.size());
        payload.put("entities", entities);
        payload.put("truncated", matches.size() > entities.size());
        payload.put("summary", buildNearbyEntitiesSummary(normalizedFilter, radius, entities.size(), matches.size() > entities.size()));
        payload.put("selector_backend", vanillaCommandBackend.executeProbe(player, Map.of("entity_type", normalizedFilter, "radius", radius)));
        return new CapabilityResult(payload, "Scanned nearby entities around the player.");
    }

    private Map<String, Object> collectPlayerSnapshot(ServerPlayerEntity player) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("uuid", player.getUuidAsString());
        payload.put("name", player.getName().getString());
        payload.put("health", player.getHealth());
        payload.put("hunger", player.getHungerManager().getFoodLevel());
        payload.put("position", GameContextCollector.positionMap(player));
        payload.put("main_hand", GameContextCollector.stackMap(player.getMainHandStack()));
        payload.put("off_hand", GameContextCollector.stackMap(player.getOffHandStack()));
        return payload;
    }

    private boolean matchesEntityFilter(Entity entity, String normalizedFilter) {
        if (normalizedFilter == null || normalizedFilter.isBlank() || "all".equals(normalizedFilter)) {
            return true;
        }
        String entityId = Registries.ENTITY_TYPE.getId(entity.getType()).toString();
        String spawnGroup = entity.getType().getSpawnGroup().name().toLowerCase(Locale.ROOT);

        return switch (normalizedFilter) {
            case "monster", "hostile" -> entity instanceof HostileEntity || "monster".equals(spawnGroup);
            case "living" -> entity instanceof LivingEntity;
            case "player", "players" -> entity instanceof ServerPlayerEntity;
            default -> normalizedFilter.equals(spawnGroup) || normalizedFilter.equals(entityId);
        };
    }

    private Map<String, Object> entityPayload(Vec3d origin, Entity entity) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("uuid", entity.getUuidAsString());
        payload.put("entity_id", Registries.ENTITY_TYPE.getId(entity.getType()).toString());
        payload.put("name", entity.getName().getString());
        payload.put("position", GameContextCollector.vectorMap(new Vec3d(entity.getX(), entity.getY(), entity.getZ())));
        payload.put("block_pos", GameContextCollector.blockPosMap(entity.getBlockPos()));
        payload.put("distance", Math.sqrt(entity.squaredDistanceTo(origin)));
        payload.put("spawn_group", entity.getType().getSpawnGroup().name().toLowerCase(Locale.ROOT));
        payload.put("is_living", entity instanceof LivingEntity);
        payload.put("is_hostile", entity instanceof HostileEntity);
        if (entity instanceof LivingEntity livingEntity) {
            payload.put("health", livingEntity.getHealth());
        }
        payload.put("tags", new ArrayList<>(entity.getCommandTags()));
        return payload;
    }

    private String buildNearbyEntitiesSummary(String normalizedFilter, double radius, int count, boolean truncated) {
        String filterLabel = "all".equals(normalizedFilter) ? "实体" : normalizedFilter + " 实体";
        if (count <= 0) {
            return "No nearby " + filterLabel + " found within " + formatRadius(radius) + " blocks.";
        }
        return "Found " + count + " nearby " + filterLabel + " within " + formatRadius(radius) + " blocks."
                + (truncated ? " Results were limited." : "");
    }

    private double clampRadius(double radius) {
        return Math.max(4.0, Math.min(radius, 96.0));
    }

    private int clampEntityLimit(int limit) {
        return Math.max(1, Math.min(limit, 32));
    }

    private String formatRadius(double radius) {
        if (Math.rint(radius) == radius) {
            return Integer.toString((int) radius);
        }
        return Double.toString(radius);
    }

    private double numberArg(Object raw, double fallback) {
        if (raw instanceof Number number) {
            return number.doubleValue();
        }
        return fallback;
    }

    private int intArg(Object raw, int fallback) {
        if (raw instanceof Number number) {
            return number.intValue();
        }
        return fallback;
    }

    private String stringArg(Object raw) {
        return raw instanceof String string ? string : null;
    }
}
