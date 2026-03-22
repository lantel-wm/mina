package mina.context;

import net.minecraft.block.BlockState;
import net.minecraft.fluid.FluidState;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.BlockView;
import net.minecraft.world.Heightmap;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class EnvironmentAssessmentProvider {
    private final int interactableRadius;

    public EnvironmentAssessmentProvider(int interactableRadius) {
        this.interactableRadius = Math.max(4, interactableRadius);
    }

    public Map<String, Object> collect(
            ServerPlayerEntity player,
            Map<String, Object> interactables,
            Map<String, Object> threats
    ) {
        BlockPos pos = player.getBlockPos();
        var world = player.getEntityWorld();
        LocalTerrainProfile terrainProfile = inspectLocalTerrain(player, pos);
        List<String> hazards = detectHazards(player, pos);
        boolean underground = world.getTopY(Heightmap.Type.MOTION_BLOCKING_NO_LEAVES, pos.getX(), pos.getZ()) - pos.getY() >= 8;
        String biomeId = world.getBiome(pos)
                .getKey()
                .map(key -> key.getValue().toString())
                .orElse("unknown");
        String locationKind = determineLocationKind(player, interactables, underground, terrainProfile);
        Map<String, Object> safeSpot = findSafeSpot(player, pos);
        boolean worthAlerting = !hazards.isEmpty()
                || ((Number) threats.getOrDefault("hostile_count", 0)).intValue() > 0
                || ((Number) threats.getOrDefault("explosive_count", 0)).intValue() > 0
                || (terrainProfile.elevatedPerch() && terrainProfile.groundOffset() >= 5);
        Map<String, Object> terrainSafety = new LinkedHashMap<>();
        terrainSafety.put("stable_footing", isStableFooting(world, pos.down()));
        terrainSafety.put("underground", underground);
        terrainSafety.put("immediate_drop", !isStableFooting(world, pos.down().down()));
        terrainSafety.put("elevated_perch", terrainProfile.elevatedPerch());
        terrainSafety.put("drop_to_ground", terrainProfile.groundOffset());
        terrainSafety.put("sky_visible", terrainProfile.skyVisible());
        terrainSafety.put("support_block_id", terrainProfile.supportBlockId());
        terrainSafety.put("support_block_kind", terrainProfile.supportBlockKind());
        terrainSafety.put("nearby_leaf_blocks", terrainProfile.nearbyLeafBlocks());
        terrainSafety.put("nearby_log_blocks", terrainProfile.nearbyLogBlocks());
        terrainSafety.put("canopy_depth", terrainProfile.canopyDepth());
        terrainSafety.put("observations", terrainObservations(terrainProfile));

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("location_kind", locationKind);
        payload.put("biome", biomeId);
        payload.put("hazard_summary", Map.of(
                "hazards", hazards,
                "hazard_count", hazards.size()
        ));
        payload.put("terrain_safety", terrainSafety);
        payload.put("safe_spot_summary", safeSpot);
        payload.put("worth_alerting", worthAlerting);
        payload.put("summary", summarizeLocation(locationKind, biomeId, terrainProfile));
        return payload;
    }

    public static String determineLocationKind(
            ServerPlayerEntity player,
            Map<String, Object> interactables,
            boolean underground,
            LocalTerrainProfile terrainProfile
    ) {
        if (player.isSubmergedInWater()) {
            return "underwater";
        }
        String dimension = player.getEntityWorld().getRegistryKey().getValue().toString();
        if (dimension.endsWith("the_nether")) {
            return "nether";
        }
        if (dimension.endsWith("the_end")) {
            return "end";
        }

        @SuppressWarnings("unchecked")
        List<String> shelterMarkers = (List<String>) interactables.getOrDefault("shelter_markers", List.of());
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> containers = (List<Map<String, Object>>) interactables.getOrDefault("containers", List.of());
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> workstations = (List<Map<String, Object>>) interactables.getOrDefault("workstations", List.of());
        if (!containers.isEmpty() && !workstations.isEmpty() && shelterMarkers.contains("shelter")) {
            return "base";
        }
        if (shelterMarkers.stream().anyMatch(marker -> marker.contains("bed")) || shelterMarkers.contains("shelter")) {
            return "village_like";
        }
        if (underground) {
            return "cave";
        }
        return "surface";
    }

    static String summarizeLocation(String locationKind, String biomeId, LocalTerrainProfile terrainProfile) {
        String biomeLabel = biomeId == null || biomeId.isBlank() ? "unknown biome" : biomeId;
        if ("cave".equals(locationKind)) {
            return "The player appears to be underground in %s.".formatted(biomeLabel);
        }
        if ("surface".equals(locationKind)) {
            return "The player appears to be on surface terrain in %s; support block is %s with %d nearby leaf blocks, %d nearby log blocks, sky visible=%s, drop to ground=%d."
                    .formatted(
                            biomeLabel,
                            terrainProfile.supportBlockId(),
                            terrainProfile.nearbyLeafBlocks(),
                            terrainProfile.nearbyLogBlocks(),
                            terrainProfile.skyVisible(),
                            terrainProfile.groundOffset()
                    );
        }
        return "The player appears to be in a %s environment in %s."
                .formatted(locationKind.replace('_', ' '), biomeLabel);
    }

    private List<String> detectHazards(ServerPlayerEntity player, BlockPos origin) {
        List<String> hazards = new ArrayList<>();
        var world = player.getEntityWorld();
        if (player.isOnFire()) {
            hazards.add("on_fire");
        }
        if (player.isSubmergedInWater()) {
            hazards.add("underwater");
        }

        for (int dx = -2; dx <= 2; dx++) {
            for (int dz = -2; dz <= 2; dz++) {
                BlockPos pos = origin.add(dx, 0, dz);
                BlockState blockState = world.getBlockState(pos);
                FluidState fluidState = world.getFluidState(pos);
                String blockId = Registries.BLOCK.getId(blockState.getBlock()).getPath().toLowerCase(Locale.ROOT);
                if (fluidState.isIn(net.minecraft.registry.tag.FluidTags.LAVA) || blockId.contains("lava")) {
                    hazards.add("lava");
                } else if (blockId.contains("fire") || blockId.contains("campfire")) {
                    hazards.add("fire");
                } else if (blockId.contains("cactus")) {
                    hazards.add("cactus");
                } else if (fluidState.isIn(net.minecraft.registry.tag.FluidTags.WATER)) {
                    hazards.add("water_flow");
                }
            }
        }
        return hazards.stream().distinct().toList();
    }

    private Map<String, Object> findSafeSpot(ServerPlayerEntity player, BlockPos origin) {
        var world = player.getEntityWorld();
        for (int radius = 1; radius <= Math.min(6, interactableRadius); radius++) {
            for (int dx = -radius; dx <= radius; dx++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    BlockPos floor = origin.add(dx, -1, dz);
                    BlockPos stand = origin.add(dx, 0, dz);
                    BlockPos head = origin.add(dx, 1, dz);
                    if (!isStableFooting(world, floor)) {
                        continue;
                    }
                    if (!world.getBlockState(stand).isAir() || !world.getBlockState(head).isAir()) {
                        continue;
                    }
                    return Map.of(
                            "available", true,
                            "distance", Math.sqrt(floor.getSquaredDistance(origin)),
                            "pos", GameContextCollector.blockPosMap(stand)
                    );
                }
            }
        }
        return Map.of("available", false);
    }

    private boolean isStableFooting(BlockView world, BlockPos pos) {
        BlockState blockState = world.getBlockState(pos);
        return !blockState.isAir()
                && blockState.isSolidBlock(world, pos)
                && !blockState.getCollisionShape(world, pos).isEmpty();
    }

    private LocalTerrainProfile inspectLocalTerrain(ServerPlayerEntity player, BlockPos origin) {
        var world = player.getEntityWorld();
        String supportBlockId = blockId(world.getBlockState(origin.down()));
        String feetBlockId = blockId(world.getBlockState(origin));
        String headBlockId = blockId(world.getBlockState(origin.up()));
        int nearbyLeafBlocks = 0;
        int nearbyLogBlocks = 0;
        for (int dx = -2; dx <= 2; dx++) {
            for (int dy = -1; dy <= 2; dy++) {
                for (int dz = -2; dz <= 2; dz++) {
                    String nearbyBlockId = blockId(world.getBlockState(origin.add(dx, dy, dz)));
                    if (isLeafBlockId(nearbyBlockId)) {
                        nearbyLeafBlocks++;
                    } else if (isWoodBlockId(nearbyBlockId)) {
                        nearbyLogBlocks++;
                    }
                }
            }
        }

        int topWithLeaves = world.getTopY(Heightmap.Type.MOTION_BLOCKING, origin.getX(), origin.getZ());
        int topNoLeaves = world.getTopY(Heightmap.Type.MOTION_BLOCKING_NO_LEAVES, origin.getX(), origin.getZ());
        int canopyDepth = Math.max(0, topWithLeaves - topNoLeaves);
        int groundOffset = Math.max(0, origin.getY() - topNoLeaves);
        boolean skyVisible = world.isSkyVisible(origin);
        return new LocalTerrainProfile(
                supportBlockId,
                supportBlockKind(supportBlockId),
                feetBlockId,
                headBlockId,
                nearbyLeafBlocks,
                nearbyLogBlocks,
                canopyDepth,
                groundOffset,
                skyVisible
        );
    }

    private static String blockId(BlockState blockState) {
        return Registries.BLOCK.getId(blockState.getBlock()).toString();
    }

    static String supportBlockKind(String blockId) {
        if (isLeafBlockId(blockId)) {
            return "leaves";
        }
        if (isWoodBlockId(blockId)) {
            return "log";
        }
        if (blockId == null || blockId.isBlank() || blockId.endsWith(":air")) {
            return "air";
        }
        return "solid";
    }

    static boolean isLeafBlockId(String blockId) {
        return blockId != null && blockId.toLowerCase(Locale.ROOT).contains("leaves");
    }

    static boolean isWoodBlockId(String blockId) {
        if (blockId == null) {
            return false;
        }
        String normalized = blockId.toLowerCase(Locale.ROOT);
        return normalized.endsWith("_log")
                || normalized.endsWith("_wood")
                || normalized.contains("mangrove_roots")
                || normalized.contains("stem");
    }

    private List<String> terrainObservations(LocalTerrainProfile terrainProfile) {
        List<String> observations = new ArrayList<>();
        if (terrainProfile.skyVisible()) {
            observations.add("open_sky");
        }
        if (terrainProfile.elevatedPerch()) {
            observations.add("elevated_position");
        }
        if (isLeafBlockId(terrainProfile.supportBlockId()) || isWoodBlockId(terrainProfile.supportBlockId())) {
            observations.add("supported_by_tree_blocks");
        }
        if (terrainProfile.nearbyLeafBlocks() >= 6) {
            observations.add("dense_leaf_cover");
        }
        if (terrainProfile.nearbyLogBlocks() >= 2) {
            observations.add("tree_trunks_or_branches_nearby");
        }
        if (terrainProfile.canopyDepth() >= 1) {
            observations.add("column_has_leaf_canopy");
        }
        return observations;
    }

    record LocalTerrainProfile(
            String supportBlockId,
            String supportBlockKind,
            String feetBlockId,
            String headBlockId,
            int nearbyLeafBlocks,
            int nearbyLogBlocks,
            int canopyDepth,
            int groundOffset,
            boolean skyVisible
    ) {
        boolean elevatedPerch() {
            return groundOffset >= 4 && skyVisible;
        }
    }
}
