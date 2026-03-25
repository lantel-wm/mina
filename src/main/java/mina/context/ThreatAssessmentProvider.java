package mina.context;

import mina.util.ObservationTextResolver;
import net.minecraft.entity.Entity;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.ItemEntity;
import net.minecraft.entity.projectile.ProjectileEntity;
import net.minecraft.entity.mob.HostileEntity;
import net.minecraft.entity.mob.Angerable;
import net.minecraft.entity.passive.IronGolemEntity;
import net.minecraft.entity.passive.VillagerEntity;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.entity.TntEntity;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class ThreatAssessmentProvider {
    private final int radius;
    private final int limit;
    private final ObservationTextResolver textResolver;

    public ThreatAssessmentProvider(int radius, int limit, ObservationTextResolver textResolver) {
        this.radius = Math.max(4, radius);
        this.limit = Math.max(4, limit);
        this.textResolver = textResolver;
    }

    public Map<String, Object> collect(ServerPlayerEntity player) {
        Vec3d origin = new Vec3d(player.getX(), player.getY(), player.getZ());
        Box searchBox = player.getBoundingBox().expand(radius);
        List<Entity> entities = player.getEntityWorld().getOtherEntities(
                player,
                searchBox,
                entity -> entity != null && entity.isAlive() && entity.squaredDistanceTo(origin) <= (double) radius * radius
        );
        entities.sort(Comparator.comparingDouble(entity -> entity.squaredDistanceTo(origin)));

        int hostiles = 0;
        int neutral = 0;
        int friendly = 0;
        int villagers = 0;
        int golems = 0;
        int bosses = 0;
        int projectiles = 0;
        int explosives = 0;
        int droppedItems = 0;
        List<Map<String, Object>> highestThreats = new ArrayList<>();
        Map<String, Object> nearestThreat = null;

        for (Entity entity : entities) {
            String category = classify(entity);
            if ("hostile".equals(category)) {
                hostiles++;
            } else if ("neutral".equals(category)) {
                neutral++;
            } else if ("friendly".equals(category)) {
                friendly++;
            }
            if (entity instanceof VillagerEntity) {
                villagers++;
            }
            if (entity instanceof IronGolemEntity) {
                golems++;
            }
            if (isBoss(entity)) {
                bosses++;
            }
            if (entity instanceof ProjectileEntity) {
                projectiles++;
            }
            if (entity instanceof TntEntity) {
                explosives++;
            }
            if (entity instanceof ItemEntity) {
                droppedItems++;
            }

            int threatLevel = threatLevel(player, entity);
            if (threatLevel <= 0) {
                continue;
            }

            Map<String, Object> entry = entityPayload(player, entity, category, threatLevel);
            if (nearestThreat == null) {
                nearestThreat = entry;
            }
            highestThreats.add(entry);
        }

        highestThreats.sort(Comparator.comparingInt(entry -> -((Number) entry.get("threat_level")).intValue()));
        if (highestThreats.size() > limit) {
            highestThreats = new ArrayList<>(highestThreats.subList(0, limit));
        }

        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("scan_radius", radius);
        summary.put("total_entities", Math.min(entities.size(), limit));
        summary.put("hostile_count", hostiles);
        summary.put("neutral_count", neutral);
        summary.put("friendly_count", friendly);
        summary.put("villager_count", villagers);
        summary.put("golem_count", golems);
        summary.put("boss_count", bosses);
        summary.put("projectile_count", projectiles);
        summary.put("explosive_count", explosives);
        summary.put("dropped_item_count", droppedItems);
        summary.put("nearest_threat", nearestThreat);
        summary.put("highest_threats", highestThreats);
        summary.put("safe_now", hostiles == 0 && explosives == 0);
        summary.put("summary", buildSummary(hostiles, explosives, nearestThreat));
        return summary;
    }

    public static String directionFromDelta(double dx, double dz) {
        if (Math.abs(dx) < 0.75 && Math.abs(dz) < 0.75) {
            return "nearby";
        }
        double angle = Math.toDegrees(Math.atan2(-dx, dz));
        if (angle < 0) {
            angle += 360.0;
        }
        if (angle < 22.5 || angle >= 337.5) {
            return "south";
        }
        if (angle < 67.5) {
            return "south_west";
        }
        if (angle < 112.5) {
            return "west";
        }
        if (angle < 157.5) {
            return "north_west";
        }
        if (angle < 202.5) {
            return "north";
        }
        if (angle < 247.5) {
            return "north_east";
        }
        if (angle < 292.5) {
            return "east";
        }
        return "south_east";
    }

    public static int threatLevel(ServerPlayerEntity player, Entity entity) {
        double distance = Math.sqrt(entity.squaredDistanceTo(player));
        if (isBoss(entity)) {
            return distance <= 24.0 ? 5 : 4;
        }
        if (entity instanceof TntEntity) {
            return distance <= 8.0 ? 5 : 4;
        }
        if (entity instanceof HostileEntity) {
            if (distance <= 6.0) {
                return 4;
            }
            if (distance <= 12.0) {
                return 3;
            }
            return 2;
        }
        if (entity instanceof ProjectileEntity) {
            return distance <= 8.0 ? 3 : 2;
        }
        if (entity instanceof Angerable) {
            return distance <= 8.0 ? 2 : 1;
        }
        return 0;
    }

    private static boolean isBoss(Entity entity) {
        String entityId = Registries.ENTITY_TYPE.getId(entity.getType()).toString();
        return entityId.endsWith(":warden")
                || entityId.endsWith(":wither")
                || entityId.endsWith(":ender_dragon")
                || entityId.endsWith(":elder_guardian")
                || entityId.endsWith(":ravager");
    }

    private static String classify(Entity entity) {
        if (entity instanceof HostileEntity || isBoss(entity) || entity instanceof TntEntity) {
            return "hostile";
        }
        if (entity instanceof Angerable) {
            return "neutral";
        }
        if (entity instanceof PlayerEntity || entity instanceof VillagerEntity || entity instanceof IronGolemEntity) {
            return "friendly";
        }
        if (entity instanceof LivingEntity) {
            return "friendly";
        }
        return "ambient";
    }

    private Map<String, Object> entityPayload(ServerPlayerEntity player, Entity entity, String category, int threatLevel) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("entity_id", Registries.ENTITY_TYPE.getId(entity.getType()).toString());
        payload.put("name", textResolver.entityName(entity));
        payload.put("category", category);
        payload.put("distance", Math.sqrt(entity.squaredDistanceTo(player)));
        payload.put("direction", directionFromDelta(entity.getX() - player.getX(), entity.getZ() - player.getZ()));
        payload.put("position", GameContextCollector.vectorMap(new Vec3d(entity.getX(), entity.getY(), entity.getZ())));
        payload.put("threat_level", threatLevel);
        if (entity instanceof LivingEntity livingEntity) {
            payload.put("health", livingEntity.getHealth());
        }
        return payload;
    }

    private String buildSummary(int hostileCount, int explosiveCount, Map<String, Object> nearestThreat) {
        if (hostileCount <= 0 && explosiveCount <= 0) {
            return "No major nearby threats detected.";
        }
        if (nearestThreat == null) {
            return "Nearby threats detected.";
        }
        return "Detected %d hostile or explosive threats. Nearest threat is %s to the %s."
                .formatted(
                        hostileCount + explosiveCount,
                        nearestThreat.get("name"),
                        String.valueOf(nearestThreat.get("direction")).replace('_', ' ')
                );
    }
}
