package mina.context;

import mina.util.ObservationTextResolver;
import net.minecraft.block.BlockState;
import net.minecraft.registry.Registries;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;
import net.minecraft.util.math.BlockPos;

import java.util.LinkedHashMap;
import java.util.Map;

public final class TargetBlockSnapshotProvider {
    private final int targetReachBlocks;
    private final ObservationTextResolver textResolver;

    public TargetBlockSnapshotProvider(int targetReachBlocks, ObservationTextResolver textResolver) {
        this.targetReachBlocks = targetReachBlocks;
        this.textResolver = textResolver;
    }

    public Map<String, Object> collect(ServerPlayerEntity player) {
        BlockHitResult hitResult = GameContextCollector.raycast(player, targetReachBlocks);
        if (hitResult == null || hitResult.getType() != HitResult.Type.BLOCK) {
            return null;
        }

        ServerWorld world = player.getEntityWorld();
        BlockPos blockPos = hitResult.getBlockPos();
        BlockState blockState = world.getBlockState(blockPos);
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("pos", GameContextCollector.blockPosMap(blockPos));
        snapshot.put("block_id", Registries.BLOCK.getId(blockState.getBlock()).toString());
        snapshot.put("block_name", textResolver.blockName(blockState));
        snapshot.put("side", hitResult.getSide().asString());
        snapshot.put("inside_block", hitResult.isInsideBlock());
        return snapshot;
    }
}
