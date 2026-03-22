package mina.capability;

import net.minecraft.entity.Entity;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.math.BlockPos;

import java.util.List;
import java.util.Map;

public interface VanillaCommandBackend {
    List<Entity> selector(ServerPlayerEntity player, Map<String, Object> selectorSpec);

    Map<String, Object> executeProbe(ServerPlayerEntity player, Map<String, Object> probeSpec);

    Map<String, Object> readBlockData(ServerPlayerEntity player, BlockPos blockPos);

    Map<String, Object> readEntityData(ServerPlayerEntity player, Entity entity);

    Map<String, Object> readScore(ServerPlayerEntity player, String holderName, String objectiveName);

    Map<String, Object> readTags(ServerPlayerEntity player, Entity entity);

    Map<String, Object> locate(ServerPlayerEntity player, Map<String, Object> arguments);
}
