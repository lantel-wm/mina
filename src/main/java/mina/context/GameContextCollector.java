package mina.context;

import mina.bridge.BridgeModels;
import mina.capability.CapabilityDefinition;
import mina.capability.CapabilityExecutorRegistry;
import mina.policy.PermissionResolver;
import mina.policy.PlayerRole;
import mina.util.JsonHelper;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import net.minecraft.world.RaycastContext;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class GameContextCollector {
    private final PermissionResolver permissionResolver;
    private final CapabilityExecutorRegistry capabilityRegistry;
    private final PlayerSnapshotProvider playerSnapshotProvider;
    private final WorldSnapshotProvider worldSnapshotProvider;
    private final TargetBlockSnapshotProvider targetBlockSnapshotProvider;
    private final RecentEventsProvider recentEventsProvider;
    private final boolean experimentalEnabled;
    private final boolean dynamicScriptingEnabled;

    public GameContextCollector(
            PermissionResolver permissionResolver,
            CapabilityExecutorRegistry capabilityRegistry,
            PlayerSnapshotProvider playerSnapshotProvider,
            WorldSnapshotProvider worldSnapshotProvider,
            TargetBlockSnapshotProvider targetBlockSnapshotProvider,
            RecentEventsProvider recentEventsProvider,
            boolean experimentalEnabled,
            boolean dynamicScriptingEnabled
    ) {
        this.permissionResolver = permissionResolver;
        this.capabilityRegistry = capabilityRegistry;
        this.playerSnapshotProvider = playerSnapshotProvider;
        this.worldSnapshotProvider = worldSnapshotProvider;
        this.targetBlockSnapshotProvider = targetBlockSnapshotProvider;
        this.recentEventsProvider = recentEventsProvider;
        this.experimentalEnabled = experimentalEnabled;
        this.dynamicScriptingEnabled = dynamicScriptingEnabled;
    }

    public TurnContext collect(ServerPlayerEntity player) {
        PlayerRole role = permissionResolver.resolveRole(player);
        List<CapabilityDefinition> visibleCapabilities = capabilityRegistry.visibleCapabilities(player, role);
        var world = player.getEntityWorld();

        Map<String, Object> playerSnapshot = playerSnapshotProvider.collect(player, role);
        Map<String, Object> worldSnapshot = worldSnapshotProvider.collectWorld(player);
        Map<String, Object> targetBlockSnapshot = targetBlockSnapshotProvider.collect(player);
        Map<String, Object> ruleReferences = worldSnapshotProvider.collectRuleReferences(world.getGameRules());

        Map<String, Object> scopedSnapshot = new LinkedHashMap<>();
        scopedSnapshot.put("player", playerSnapshot);
        scopedSnapshot.put("world", worldSnapshot);
        scopedSnapshot.put("target_block", targetBlockSnapshot);
        scopedSnapshot.put("server_rules_refs", ruleReferences);
        scopedSnapshot.put("recent_events", recentEventsProvider.collect(player));
        scopedSnapshot.put("visible_capability_ids", visibleCapabilities.stream().map(CapabilityDefinition::id).toList());

        BridgeModels.PlayerPayload playerPayload = new BridgeModels.PlayerPayload();
        playerPayload.uuid = player.getUuidAsString();
        playerPayload.name = player.getName().getString();
        playerPayload.role = role.wireValue();
        playerPayload.dimension = world.getRegistryKey().getValue().toString();
        playerPayload.position = positionMap(player);

        BridgeModels.ServerEnvPayload serverEnvPayload = new BridgeModels.ServerEnvPayload();
        serverEnvPayload.dedicated = world.getServer().isDedicated();
        serverEnvPayload.motd = world.getServer().getServerMotd();
        serverEnvPayload.current_players = world.getServer().getCurrentPlayerCount();
        serverEnvPayload.max_players = world.getServer().getMaxPlayerCount();
        serverEnvPayload.carpet_loaded = capabilityRegistry.isCarpetAvailable();
        serverEnvPayload.experimental_enabled = experimentalEnabled;
        serverEnvPayload.dynamic_scripting_enabled = dynamicScriptingEnabled;

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

    public static BlockHitResult raycast(ServerPlayerEntity player, int targetReachBlocks) {
        Vec3d start = player.getEyePos();
        Vec3d end = start.add(player.getRotationVector().multiply(targetReachBlocks));
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
