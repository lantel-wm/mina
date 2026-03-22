package mina.capability;

import net.minecraft.server.network.ServerPlayerEntity;

import java.util.Map;

public interface CarpetObservationBackend {
    boolean isAvailable();

    Map<String, Object> readRules(ServerPlayerEntity player);

    Map<String, Object> readObservability(ServerPlayerEntity player);

    Map<String, Object> readFakePlayers(ServerPlayerEntity player);

    Map<String, Object> ambientSnapshot(ServerPlayerEntity player);
}
