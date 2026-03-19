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
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.text.Text;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import net.minecraft.world.rule.GameRule;
import net.minecraft.world.rule.GameRules;

import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

public final class CapabilityExecutorRegistry {
    private final MinaConfig config;
    private final Map<String, CapabilityDefinition> capabilities;
    private final boolean carpetAvailable;

    public CapabilityExecutorRegistry(MinaConfig config) {
        this.config = config;
        this.carpetAvailable = FabricLoader.getInstance().isModLoaded("carpet");
        this.capabilities = buildCapabilities();
    }

    public boolean isCarpetAvailable() {
        return carpetAvailable;
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
        map.put("game.player_snapshot.read", new CapabilityDefinition(
                "game.player_snapshot.read",
                "tool",
                "Read the current player's server-side snapshot, including health, hunger, hands, position, and a small inventory summary.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of(),
                (player, role) -> true,
                (player, arguments) -> new CapabilityResult(collectPlayerSnapshot(player), "Read current player snapshot.")
        ));
        map.put("game.target_block.read", new CapabilityDefinition(
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
                (player, role) -> true,
                (player, arguments) -> executeTargetBlockRead(player, arguments)
        ));
        map.put("server.rules.read", new CapabilityDefinition(
                "server.rules.read",
                "tool",
                "Read a summary of current gamerules plus server.properties when available.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of(),
                (player, role) -> true,
                (player, arguments) -> executeServerRulesRead(player)
        ));
        map.put("carpet.block_info.read", new CapabilityDefinition(
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
                (player, role) -> carpetAvailable,
                (player, arguments) -> executeCarpetBlockInfo(player, arguments)
        ));
        map.put("carpet.distance.measure", new CapabilityDefinition(
                "carpet.distance.measure",
                "tool",
                "Measure distance between two positions using structured metrics instead of raw Carpet commands.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of(),
                (player, role) -> carpetAvailable,
                (player, arguments) -> executeDistanceMeasure(player, arguments)
        ));
        map.put("carpet.mobcaps.read", new CapabilityDefinition(
                "carpet.mobcaps.read",
                "tool",
                "Read Carpet's current mobcap report for the player's dimension.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                Map.of(),
                Map.of(),
                (player, role) -> carpetAvailable,
                (player, arguments) -> executeMobcapsRead(player)
        ));
        return Map.copyOf(map);
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
        return new CapabilityResult(payload, "Read targeted block state.");
    }

    private CapabilityResult executeServerRulesRead(ServerPlayerEntity player) {
        Map<String, Object> payload = new LinkedHashMap<>();
        GameRules gameRules = player.getEntityWorld().getGameRules();
        Map<String, Object> gamerules = new LinkedHashMap<>();
        for (GameRule<?> rule : gameRules.streamRules().toList()) {
            gamerules.put(Registries.GAME_RULE.getId(rule).toString(), gameRules.getRuleValueName(rule));
        }
        payload.put("gamerules", gamerules);

        var serverPropertiesPath = player.getEntityWorld().getServer().getRunDirectory().resolve("server.properties");
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
}
