package mina.context;

import mina.bridge.AppServerModels;
import mina.capability.DirectWorldReader;
import mina.capability.CapabilityDefinition;
import mina.capability.CapabilityExecutorRegistry;
import mina.policy.PermissionResolver;
import mina.policy.PlayerRole;
import mina.util.JsonHelper;
import mina.util.ObservationTextResolver;
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
    private final PlayerStateProvider playerStateProvider;
    private final WorldStateProvider worldStateProvider;
    private final WorldSnapshotProvider ruleSnapshotProvider;
    private final DirectWorldReader directWorldReader;
    private final TargetBlockSnapshotProvider targetBlockSnapshotProvider;
    private final RecentEventTracker recentEventTracker;
    private final boolean experimentalEnabled;
    private final boolean dynamicScriptingEnabled;

    public GameContextCollector(
            PermissionResolver permissionResolver,
            CapabilityExecutorRegistry capabilityRegistry,
            PlayerStateProvider playerStateProvider,
            WorldStateProvider worldStateProvider,
            WorldSnapshotProvider ruleSnapshotProvider,
            DirectWorldReader directWorldReader,
            TargetBlockSnapshotProvider targetBlockSnapshotProvider,
            RecentEventTracker recentEventTracker,
            boolean experimentalEnabled,
            boolean dynamicScriptingEnabled
    ) {
        this.permissionResolver = permissionResolver;
        this.capabilityRegistry = capabilityRegistry;
        this.playerStateProvider = playerStateProvider;
        this.worldStateProvider = worldStateProvider;
        this.ruleSnapshotProvider = ruleSnapshotProvider;
        this.directWorldReader = directWorldReader;
        this.targetBlockSnapshotProvider = targetBlockSnapshotProvider;
        this.recentEventTracker = recentEventTracker;
        this.experimentalEnabled = experimentalEnabled;
        this.dynamicScriptingEnabled = dynamicScriptingEnabled;
    }

    public TurnContext collect(ServerPlayerEntity player) {
        PlayerRole role = permissionResolver.resolveRole(player);
        List<CapabilityDefinition> visibleCapabilities = capabilityRegistry.visibleCapabilities(player, role);
        var world = player.getEntityWorld();

        Map<String, Object> playerSnapshot = playerStateProvider.collect(player, role, recentEventTracker);
        @SuppressWarnings("unchecked")
        Map<String, Object> sceneSnapshot = directWorldReader.readScene(player);
        Map<String, Object> worldSnapshot = worldStateProvider.collectWorld(player, String.valueOf(sceneSnapshot.get("location_kind")));
        Map<String, Object> interactablesSnapshot = directWorldReader.readInteractables(player);
        Map<String, Object> socialSnapshot = directWorldReader.readSocial(player);
        Map<String, Object> technicalSnapshot = capabilityRegistry.ambientTechnicalSnapshot(player);
        Map<String, Object> targetBlockSnapshot = targetBlockSnapshotProvider.collect(player);
        Map<String, Object> ruleReferences = ruleSnapshotProvider.collectRuleReferences(world.getGameRules());
        Map<String, Object> recentEvents = Map.of("events", recentEventTracker.collect(player));
        Object riskState = sceneSnapshot.get("risk_state");

        Map<String, Object> scopedSnapshot = new LinkedHashMap<>();
        scopedSnapshot.put("player", playerSnapshot);
        scopedSnapshot.put("world", worldSnapshot);
        scopedSnapshot.put("scene", sceneSnapshot);
        scopedSnapshot.put("interactables", interactablesSnapshot);
        scopedSnapshot.put("social", socialSnapshot);
        scopedSnapshot.put("technical", technicalSnapshot);
        scopedSnapshot.put("risk_state", riskState instanceof Map<?, ?> ? riskState : Map.of());
        scopedSnapshot.put("target_block", targetBlockSnapshot);
        scopedSnapshot.put("server_rules_refs", ruleReferences);
        scopedSnapshot.put("recent_events", recentEventTracker.collect(player));
        scopedSnapshot.put("visible_capability_ids", visibleCapabilities.stream().map(CapabilityDefinition::id).toList());

        AppServerModels.PlayerPayload playerPayload = new AppServerModels.PlayerPayload();
        playerPayload.uuid = player.getUuidAsString();
        playerPayload.name = player.getName().getString();
        playerPayload.role = role.wireValue();
        playerPayload.dimension = world.getRegistryKey().getValue().toString();
        playerPayload.position = positionMap(player);

        AppServerModels.ServerEnvPayload serverEnvPayload = new AppServerModels.ServerEnvPayload();
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

    public static Map<String, Object> stackMap(ItemStack stack, ObservationTextResolver textResolver) {
        if (stack == null || stack.isEmpty()) {
            return null;
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("item_id", Registries.ITEM.getId(stack.getItem()).toString());
        payload.put("count", stack.getCount());
        payload.put("name", textResolver.itemName(stack));
        return payload;
    }
}
