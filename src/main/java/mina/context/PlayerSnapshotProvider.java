package mina.context;

import mina.policy.PlayerRole;
import mina.util.ObservationTextResolver;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class PlayerSnapshotProvider {
    private final int inventorySummaryLimit;
    private final ObservationTextResolver textResolver;

    public PlayerSnapshotProvider(int inventorySummaryLimit, ObservationTextResolver textResolver) {
        this.inventorySummaryLimit = inventorySummaryLimit;
        this.textResolver = textResolver;
    }

    public Map<String, Object> collect(ServerPlayerEntity player, PlayerRole role) {
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("uuid", player.getUuidAsString());
        snapshot.put("name", player.getName().getString());
        snapshot.put("role", role.wireValue());
        snapshot.put("health", player.getHealth());
        snapshot.put("hunger", player.getHungerManager().getFoodLevel());
        snapshot.put("experience_level", player.experienceLevel);
        snapshot.put("selected_slot", player.getInventory().getSelectedSlot());
        snapshot.put("position", GameContextCollector.positionMap(player));
        snapshot.put("look_vector", GameContextCollector.vectorMap(player.getRotationVector()));
        snapshot.put("main_hand", GameContextCollector.stackMap(player.getMainHandStack(), textResolver));
        snapshot.put("off_hand", GameContextCollector.stackMap(player.getOffHandStack(), textResolver));
        snapshot.put("inventory_summary", summarizeInventory(player.getInventory()));
        return snapshot;
    }

    private List<Map<String, Object>> summarizeInventory(PlayerInventory inventory) {
        List<Map<String, Object>> summary = new ArrayList<>();
        for (ItemStack stack : inventory.getMainStacks()) {
            if (summary.size() >= inventorySummaryLimit) {
                break;
            }
            if (stack == null || stack.isEmpty()) {
                continue;
            }
            summary.add(GameContextCollector.stackMap(stack, textResolver));
        }
        return summary;
    }
}
