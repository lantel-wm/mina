package mina.context;

import mina.policy.PlayerRole;
import mina.util.ObservationTextResolver;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.item.BlockItem;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class PlayerStateProvider {
    private final int inventorySummaryLimit;
    private final ObservationTextResolver textResolver;

    public PlayerStateProvider(int inventorySummaryLimit, ObservationTextResolver textResolver) {
        this.inventorySummaryLimit = Math.max(1, inventorySummaryLimit);
        this.textResolver = textResolver;
    }

    public Map<String, Object> collect(ServerPlayerEntity player, PlayerRole role, RecentEventTracker recentEventTracker) {
        Map<String, Object> inventoryBrief = collectInventory(player);
        Map<String, Object> damageState = recentEventTracker.recentDamageState(player);
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("uuid", player.getUuidAsString());
        snapshot.put("name", player.getName().getString());
        snapshot.put("role", role.wireValue());
        snapshot.put("position", GameContextCollector.positionMap(player));
        snapshot.put("look_vector", GameContextCollector.vectorMap(player.getRotationVector()));
        snapshot.put("health", player.getHealth());
        snapshot.put("hunger", player.getHungerManager().getFoodLevel());
        snapshot.put("experience_level", player.experienceLevel);
        snapshot.put("selected_slot", player.getInventory().getSelectedSlot());
        snapshot.put("main_hand", GameContextCollector.stackMap(player.getMainHandStack(), textResolver));
        snapshot.put("off_hand", GameContextCollector.stackMap(player.getOffHandStack(), textResolver));
        snapshot.put("inventory_summary", inventoryBrief.get("hotbar_and_main"));
        snapshot.put("core_status", Map.of(
                "health", player.getHealth(),
                "max_health", player.getMaxHealth(),
                "hunger", player.getHungerManager().getFoodLevel(),
                "saturation", player.getHungerManager().getSaturationLevel()
        ));
        snapshot.put("movement_flags", Map.of(
                "in_water", player.isSubmergedInWater(),
                "touching_water", player.isTouchingWater(),
                "on_fire", player.isOnFire(),
                "fall_flying", player.isGliding(),
                "flying", player.getAbilities().flying,
                "sprinting", player.isSprinting(),
                "sneaking", player.isSneaking(),
                "on_ground", player.isOnGround()
        ));
        snapshot.put(
                "hands",
                handsSnapshot(
                        GameContextCollector.stackMap(player.getMainHandStack(), textResolver),
                        GameContextCollector.stackMap(player.getOffHandStack(), textResolver),
                        player.isUsingItem()
                )
        );
        snapshot.put("experience", Map.of(
                "level", player.experienceLevel,
                "progress", player.experienceProgress,
                "total", player.totalExperience
        ));
        snapshot.put("game_mode", player.getGameMode().name().toLowerCase(Locale.ROOT));
        snapshot.put("recent_damage_state", damageState);
        snapshot.put("inventory_brief", inventoryBrief);
        return snapshot;
    }

    public Map<String, Object> collectInventory(ServerPlayerEntity player) {
        PlayerInventory inventory = player.getInventory();
        List<Map<String, Object>> hotbarAndMain = new ArrayList<>();
        int torchCount = 0;
        int foodCount = 0;
        int buildingBlockCount = 0;
        boolean hasBed = false;
        boolean hasPickaxe = false;
        boolean hasFood = false;
        boolean hasTorches = false;

        for (ItemStack stack : inventory.getMainStacks()) {
            if (stack == null || stack.isEmpty()) {
                continue;
            }
            if (hotbarAndMain.size() < inventorySummaryLimit) {
                hotbarAndMain.add(stackBrief(stack));
            }
            String path = Registries.ITEM.getId(stack.getItem()).getPath();
            if (path.contains("torch")) {
                torchCount += stack.getCount();
                hasTorches = true;
            }
            if (looksLikeFood(path)) {
                foodCount += stack.getCount();
                hasFood = true;
            }
            if (stack.getItem() instanceof BlockItem) {
                buildingBlockCount += stack.getCount();
            }
            if (path.contains("bed")) {
                hasBed = true;
            }
            if (path.endsWith("pickaxe")) {
                hasPickaxe = true;
            }
        }

        List<Map<String, Object>> armor = new ArrayList<>();
        for (EquipmentSlot slot : List.of(EquipmentSlot.HEAD, EquipmentSlot.CHEST, EquipmentSlot.LEGS, EquipmentSlot.FEET)) {
            ItemStack stack = player.getEquippedStack(slot);
            if (stack == null || stack.isEmpty()) {
                continue;
            }
            armor.add(stackBrief(stack));
        }

        Map<String, Object> shortages = new LinkedHashMap<>();
        shortages.put("needs_food", !hasFood || foodCount < 8);
        shortages.put("needs_torches", !hasTorches || torchCount < 16);
        shortages.put("needs_blocks", buildingBlockCount < 32);
        shortages.put("needs_bed", !hasBed);
        shortages.put("needs_pickaxe", !hasPickaxe);

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("hotbar_and_main", hotbarAndMain);
        payload.put("armor", armor);
        payload.put("torch_count", torchCount);
        payload.put("food_count", foodCount);
        payload.put("building_block_count", buildingBlockCount);
        payload.put("has_bed", hasBed);
        payload.put("has_pickaxe", hasPickaxe);
        payload.put("shortages", shortages);
        return payload;
    }

    private Map<String, Object> stackBrief(ItemStack stack) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("item", GameContextCollector.stackMap(stack, textResolver));
        payload.put("damageable", stack.isDamageable());
        if (stack.isDamageable()) {
            payload.put("durability_left", Math.max(0, stack.getMaxDamage() - stack.getDamage()));
        }
        return payload;
    }

    static Map<String, Object> handsSnapshot(
            Map<String, Object> mainHand,
            Map<String, Object> offHand,
            boolean usingItem
    ) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("main_hand", mainHand);
        payload.put("off_hand", offHand);
        payload.put("using_item", usingItem);
        return payload;
    }

    private boolean looksLikeFood(String itemPath) {
        return itemPath.contains("bread")
                || itemPath.contains("beef")
                || itemPath.contains("pork")
                || itemPath.contains("mutton")
                || itemPath.contains("chicken")
                || itemPath.contains("carrot")
                || itemPath.contains("potato")
                || itemPath.contains("berry")
                || itemPath.contains("apple")
                || itemPath.contains("stew")
                || itemPath.contains("melon")
                || itemPath.contains("cookie")
                || itemPath.contains("fish");
    }
}
