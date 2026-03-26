package mina.companion;

import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;

import java.util.LinkedHashMap;
import java.util.Map;

public final class PlayerActivityTracker {
    private static final long REPETITION_THRESHOLD_TICKS = 6_000L;

    public ActivityAlert sample(
            ActivityState state,
            long currentTick,
            ServerPlayerEntity player,
            int targetReachBlocks,
            String locationKind,
            String riskLevel
    ) {
        ActivitySnapshot snapshot = snapshot(player, targetReachBlocks, locationKind, riskLevel);
        if (snapshot.family == null) {
            state.reset();
            return null;
        }
        if (snapshot.sameGroup(state.lastFamily, state.lastBucket, state.lastDimension)) {
            state.consecutiveTicks += Math.max(20L, currentTick - state.lastObservedTick);
        } else {
            state.lastFamily = snapshot.family;
            state.lastBucket = snapshot.positionBucket;
            state.lastDimension = snapshot.dimension;
            state.consecutiveTicks = 20L;
            state.alertIssued = false;
        }
        state.lastObservedTick = currentTick;
        if (state.alertIssued || state.consecutiveTicks < REPETITION_THRESHOLD_TICKS) {
            return null;
        }
        state.alertIssued = true;
        state.lastActivityFamily = snapshot.family;
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("activity_family", snapshot.family);
        payload.put("dimension", snapshot.dimension);
        payload.put("position_bucket", snapshot.positionBucket);
        payload.put("main_hand_item", snapshot.mainHandItemId);
        payload.put("target_block_id", snapshot.targetBlockId);
        payload.put("location_kind", snapshot.locationKind);
        payload.put("repetition_duration_ticks", state.consecutiveTicks);
        return new ActivityAlert(snapshot.family, payload);
    }

    public static String classifyFamily(String mainHandItemId, String targetBlockId, String locationKind, String riskLevel) {
        String hand = normalize(mainHandItemId);
        String target = normalize(targetBlockId);
        String location = normalize(locationKind);
        String risk = normalize(riskLevel);
        if ("high".equals(risk) || "critical".equals(risk)) {
            return "combat_grind";
        }
        if (target.contains("wheat")
                || target.contains("carrots")
                || target.contains("potatoes")
                || target.contains("beetroots")
                || target.contains("melon")
                || target.contains("pumpkin")
                || target.contains("sugar_cane")
                || target.contains("nether_wart")
                || hand.endsWith("hoe")) {
            return "harvesting_like";
        }
        if (hand.endsWith("pickaxe") || hand.endsWith("shovel") || hand.endsWith("axe")) {
            return "mining_like";
        }
        if (location.contains("base")
                || target.contains("planks")
                || target.contains("log")
                || target.contains("stone")
                || target.contains("glass")
                || target.contains("brick")) {
            return "building_like";
        }
        return null;
    }

    private ActivitySnapshot snapshot(ServerPlayerEntity player, int targetReachBlocks, String locationKind, String riskLevel) {
        String mainHandItemId = normalize(Registries.ITEM.getId(player.getMainHandStack().getItem()).toString());
        String dimension = player.getEntityWorld().getRegistryKey().getValue().toString();
        int bucketX = floorBucket(player.getX(), 12);
        int bucketY = floorBucket(player.getY(), 12);
        int bucketZ = floorBucket(player.getZ(), 12);
        String targetBlockId = "";
        BlockHitResult hit = mina.context.GameContextCollector.raycast(player, targetReachBlocks);
        if (hit.getType() == HitResult.Type.BLOCK) {
            BlockPos blockPos = hit.getBlockPos();
            targetBlockId = normalize(Registries.BLOCK.getId(player.getEntityWorld().getBlockState(blockPos).getBlock()).toString());
        }
        String family = classifyFamily(mainHandItemId, targetBlockId, locationKind, riskLevel);
        return new ActivitySnapshot(
                family,
                dimension,
                bucketX + ":" + bucketY + ":" + bucketZ,
                mainHandItemId,
                targetBlockId,
                normalize(locationKind)
        );
    }

    private static int floorBucket(double coordinate, int bucketSize) {
        return (int) Math.floor(coordinate / Math.max(1, bucketSize));
    }

    private static String normalize(String value) {
        return value == null ? "" : value.trim().toLowerCase();
    }

    public static final class ActivityState {
        private String lastFamily;
        private String lastBucket;
        private String lastDimension;
        private long lastObservedTick = -1L;
        private long consecutiveTicks = 0L;
        private boolean alertIssued;
        private String lastActivityFamily;

        public void reset() {
            lastFamily = null;
            lastBucket = null;
            lastDimension = null;
            lastObservedTick = -1L;
            consecutiveTicks = 0L;
            alertIssued = false;
        }

        public String lastActivityFamily() {
            return lastActivityFamily;
        }
    }

    public record ActivityAlert(String family, Map<String, Object> payload) {
    }

    private record ActivitySnapshot(
            String family,
            String dimension,
            String positionBucket,
            String mainHandItemId,
            String targetBlockId,
            String locationKind
    ) {
        private boolean sameGroup(String previousFamily, String previousBucket, String previousDimension) {
            return family != null
                    && family.equals(previousFamily)
                    && positionBucket.equals(previousBucket)
                    && dimension.equals(previousDimension);
        }
    }
}
