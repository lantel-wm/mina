package mina.context;

import mina.bridge.BridgeModels;
import mina.capability.CapabilityDefinition;
import mina.capability.CapabilityExecutorRegistry;
import mina.config.MinaConfig;
import mina.policy.PermissionResolver;
import mina.policy.PlayerRole;
import mina.util.JsonHelper;
import net.minecraft.block.BlockState;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import net.minecraft.world.RaycastContext;
import net.minecraft.world.rule.GameRule;
import net.minecraft.world.rule.GameRules;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class GameContextCollector {
    private final MinaConfig config;
    private final PermissionResolver permissionResolver;
    private final CapabilityExecutorRegistry capabilityRegistry;

    public GameContextCollector(
            MinaConfig config,
            PermissionResolver permissionResolver,
            CapabilityExecutorRegistry capabilityRegistry
    ) {
        this.config = config;
        this.permissionResolver = permissionResolver;
        this.capabilityRegistry = capabilityRegistry;
    }

    public TurnContext collect(ServerPlayerEntity player) {
        PlayerRole role = permissionResolver.resolveRole(player);
        List<CapabilityDefinition> visibleCapabilities = capabilityRegistry.visibleCapabilities(player, role);
        ServerWorld world = player.getEntityWorld();

        Map<String, Object> playerSnapshot = collectPlayerSnapshot(player, role);
        Map<String, Object> worldSnapshot = collectWorldSnapshot(player);
        Map<String, Object> targetBlockSnapshot = collectTargetBlockSnapshot(player);
        Map<String, Object> ruleReferences = collectRuleReferences(world.getGameRules());

        Map<String, Object> scopedSnapshot = new LinkedHashMap<>();
        scopedSnapshot.put("player", playerSnapshot);
        scopedSnapshot.put("world", worldSnapshot);
        scopedSnapshot.put("target_block", targetBlockSnapshot);
        scopedSnapshot.put("server_rules_refs", ruleReferences);
        scopedSnapshot.put("visible_capability_ids", visibleCapabilities.stream().map(CapabilityDefinition::id).toList());

        String dimension = world.getRegistryKey().getValue().toString();
        BridgeModels.PlayerPayload playerPayload = new BridgeModels.PlayerPayload();
        playerPayload.uuid = player.getUuidAsString();
        playerPayload.name = player.getName().getString();
        playerPayload.role = role.wireValue();
        playerPayload.dimension = dimension;
        playerPayload.position = positionMap(player);

        BridgeModels.ServerEnvPayload serverEnvPayload = new BridgeModels.ServerEnvPayload();
        serverEnvPayload.dedicated = world.getServer().isDedicated();
        serverEnvPayload.motd = world.getServer().getServerMotd();
        serverEnvPayload.current_players = world.getServer().getCurrentPlayerCount();
        serverEnvPayload.max_players = world.getServer().getMaxPlayerCount();
        serverEnvPayload.carpet_loaded = capabilityRegistry.isCarpetAvailable();
        serverEnvPayload.experimental_enabled = config.enableExperimentalCapabilities();
        serverEnvPayload.dynamic_scripting_enabled = config.enableDynamicScripting();

        return new TurnContext(
                player.getUuidAsString(),
                role,
                playerPayload,
                serverEnvPayload,
                scopedSnapshot,
                visibleCapabilities,
                JsonHelper.sha256(scopedSnapshot)
        );
    }

    private Map<String, Object> collectPlayerSnapshot(ServerPlayerEntity player, PlayerRole role) {
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("uuid", player.getUuidAsString());
        snapshot.put("name", player.getName().getString());
        snapshot.put("role", role.wireValue());
        snapshot.put("health", player.getHealth());
        snapshot.put("hunger", player.getHungerManager().getFoodLevel());
        snapshot.put("experience_level", player.experienceLevel);
        snapshot.put("selected_slot", player.getInventory().getSelectedSlot());
        snapshot.put("position", positionMap(player));
        snapshot.put("look_vector", vectorMap(player.getRotationVector()));
        snapshot.put("main_hand", stackMap(player.getMainHandStack()));
        snapshot.put("off_hand", stackMap(player.getOffHandStack()));
        snapshot.put("inventory_summary", summarizeInventory(player.getInventory()));
        return snapshot;
    }

    private Map<String, Object> collectWorldSnapshot(ServerPlayerEntity player) {
        ServerWorld world = player.getEntityWorld();
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("dimension", world.getRegistryKey().getValue().toString());
        snapshot.put("time_of_day", world.getTimeOfDay());
        snapshot.put("is_day", world.isDay());
        snapshot.put("is_raining", world.isRaining());
        snapshot.put("is_thundering", world.isThundering());
        snapshot.put("weather", world.isThundering() ? "thunder" : world.isRaining() ? "rain" : "clear");
        return snapshot;
    }

    private Map<String, Object> collectTargetBlockSnapshot(ServerPlayerEntity player) {
        BlockHitResult hitResult = raycast(player);
        if (hitResult == null || hitResult.getType() != HitResult.Type.BLOCK) {
            return null;
        }

        ServerWorld world = player.getEntityWorld();
        BlockPos blockPos = hitResult.getBlockPos();
        BlockState blockState = world.getBlockState(blockPos);
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("pos", blockPosMap(blockPos));
        snapshot.put("block_id", Registries.BLOCK.getId(blockState.getBlock()).toString());
        snapshot.put("side", hitResult.getSide().asString());
        snapshot.put("inside_block", hitResult.isInsideBlock());
        return snapshot;
    }

    private Map<String, Object> collectRuleReferences(GameRules gameRules) {
        Map<String, Object> summary = new LinkedHashMap<>();
        int remaining = config.serverRuleSummaryLimit();

        for (GameRule<?> rule : gameRules.streamRules().toList()) {
            if (remaining-- <= 0) {
                break;
            }

            summary.put(Registries.GAME_RULE.getId(rule).toString(), gameRules.getRuleValueName(rule));
        }

        return summary;
    }

    private List<Map<String, Object>> summarizeInventory(PlayerInventory inventory) {
        List<Map<String, Object>> summary = new ArrayList<>();

        for (ItemStack stack : inventory.getMainStacks()) {
            if (summary.size() >= config.inventorySummaryLimit()) {
                break;
            }

            if (stack == null || stack.isEmpty()) {
                continue;
            }

            summary.add(stackMap(stack));
        }

        return summary;
    }

    public BlockHitResult raycast(ServerPlayerEntity player) {
        Vec3d start = player.getEyePos();
        Vec3d end = start.add(player.getRotationVector().multiply(config.targetReachBlocks()));
        RaycastContext context = new RaycastContext(
                start,
                end,
                RaycastContext.ShapeType.OUTLINE,
                RaycastContext.FluidHandling.NONE,
                player
        );

        return player.getEntityWorld().raycast(context);
    }

    public static Map<String, Object> blockPosMap(BlockPos blockPos) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("x", blockPos.getX());
        payload.put("y", blockPos.getY());
        payload.put("z", blockPos.getZ());
        return payload;
    }

    public static Map<String, Object> vectorMap(Vec3d vector) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("x", vector.x);
        payload.put("y", vector.y);
        payload.put("z", vector.z);
        return payload;
    }

    public static Map<String, Object> positionMap(ServerPlayerEntity player) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("x", player.getX());
        payload.put("y", player.getY());
        payload.put("z", player.getZ());
        return payload;
    }

    public static Map<String, Object> stackMap(ItemStack stack) {
        if (stack == null || stack.isEmpty()) {
            return null;
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("item_id", Registries.ITEM.getId(stack.getItem()).toString());
        payload.put("count", stack.getCount());
        payload.put("name", stack.getName().getString());
        return payload;
    }
}
