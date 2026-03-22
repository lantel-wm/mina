package mina.context;

import net.minecraft.block.entity.BlockEntity;
import net.minecraft.inventory.Inventory;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.math.BlockPos;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class InteractableScanProvider {
    private final int radius;
    private final int verticalRange;

    public InteractableScanProvider(int radius, int verticalRange) {
        this.radius = Math.max(4, radius);
        this.verticalRange = Math.max(2, verticalRange);
    }

    public Map<String, Object> collect(ServerPlayerEntity player) {
        BlockPos origin = player.getBlockPos();
        var world = player.getEntityWorld();
        List<Map<String, Object>> nearbyBlocks = new ArrayList<>();
        List<Map<String, Object>> containers = new ArrayList<>();
        List<Map<String, Object>> workstations = new ArrayList<>();
        List<String> shelterMarkers = new ArrayList<>();

        for (int dx = -radius; dx <= radius; dx++) {
            for (int dy = -verticalRange; dy <= verticalRange; dy++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    BlockPos pos = origin.add(dx, dy, dz);
                    var blockState = world.getBlockState(pos);
                    if (blockState.isAir()) {
                        continue;
                    }

                    String blockId = Registries.BLOCK.getId(blockState.getBlock()).toString();
                    String classification = classifyBlock(blockId);
                    if ("other".equals(classification)) {
                        continue;
                    }

                    Map<String, Object> entry = new LinkedHashMap<>();
                    entry.put("block_id", blockId);
                    entry.put("kind", classification);
                    entry.put("pos", GameContextCollector.blockPosMap(pos));
                    entry.put("distance", Math.sqrt(pos.getSquaredDistance(origin)));
                    nearbyBlocks.add(entry);

                    if (isShelterMarker(classification) && !shelterMarkers.contains(classification)) {
                        shelterMarkers.add(classification);
                    }

                    BlockEntity blockEntity = world.getBlockEntity(pos);
                    if (blockEntity instanceof Inventory inventory) {
                        containers.add(containerPayload(entry, inventory));
                    } else if ("workstation".equals(classification)) {
                        workstations.add(entry);
                    }
                }
            }
        }

        nearbyBlocks.sort(Comparator.comparingDouble(entry -> ((Number) entry.get("distance")).doubleValue()));
        containers.sort(Comparator.comparingDouble(entry -> ((Number) entry.get("distance")).doubleValue()));
        workstations.sort(Comparator.comparingDouble(entry -> ((Number) entry.get("distance")).doubleValue()));

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("nearby_blocks", nearbyBlocks.subList(0, Math.min(nearbyBlocks.size(), 12)));
        payload.put("containers", containers.subList(0, Math.min(containers.size(), 8)));
        payload.put("workstations", workstations.subList(0, Math.min(workstations.size(), 8)));
        payload.put("shelter_markers", shelterMarkers);
        payload.put("summary", buildSummary(containers.size(), workstations.size(), shelterMarkers));
        return payload;
    }

    public static String classifyBlock(String blockId) {
        String path = blockId.toLowerCase(Locale.ROOT);
        if (path.contains("chest") || path.contains("barrel") || path.contains("shulker_box") || path.contains("hopper")) {
            return "container";
        }
        if (path.contains("furnace") || path.contains("crafting_table") || path.contains("smithing_table")
                || path.contains("grindstone") || path.contains("loom") || path.contains("cartography_table")
                || path.contains("enchanting_table") || path.contains("anvil") || path.contains("brewing_stand")
                || path.contains("stonecutter")) {
            return "workstation";
        }
        if (path.contains("bed") || path.contains("door") || path.contains("campfire")
                || path.contains("respawn_anchor") || path.contains("nether_portal")) {
            return "shelter";
        }
        if (path.contains("spawner")) {
            return "danger_block";
        }
        return "other";
    }

    private Map<String, Object> containerPayload(Map<String, Object> base, Inventory inventory) {
        Map<String, Object> payload = new LinkedHashMap<>(base);
        int occupiedSlots = 0;
        int totalItems = 0;
        for (int slot = 0; slot < inventory.size(); slot++) {
            if (inventory.getStack(slot).isEmpty()) {
                continue;
            }
            occupiedSlots++;
            totalItems += inventory.getStack(slot).getCount();
        }
        payload.put("occupied_slots", occupiedSlots);
        payload.put("total_items", totalItems);
        return payload;
    }

    private boolean isShelterMarker(String classification) {
        return "shelter".equals(classification) || "container".equals(classification) || "workstation".equals(classification);
    }

    private String buildSummary(int containers, int workstations, List<String> shelterMarkers) {
        if (containers == 0 && workstations == 0 && shelterMarkers.isEmpty()) {
            return "No nearby containers, workstations, or shelter markers detected.";
        }
        return "Detected %d containers, %d workstations, and %d shelter markers nearby."
                .formatted(containers, workstations, shelterMarkers.size());
    }
}
