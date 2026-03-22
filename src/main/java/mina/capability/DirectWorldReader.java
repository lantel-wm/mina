package mina.capability;

import net.minecraft.server.network.ServerPlayerEntity;

import java.util.Map;

public interface DirectWorldReader {
    Map<String, Object> readPlayerState(ServerPlayerEntity player);

    Map<String, Object> readScene(ServerPlayerEntity player);

    Map<String, Object> readThreats(ServerPlayerEntity player);

    Map<String, Object> readEnvironment(ServerPlayerEntity player);

    Map<String, Object> readInteractables(ServerPlayerEntity player);

    Map<String, Object> readInventory(ServerPlayerEntity player);

    Map<String, Object> readPoi(ServerPlayerEntity player, Map<String, Object> arguments);

    Map<String, Object> readSocial(ServerPlayerEntity player);

    Map<String, Object> readEvents(ServerPlayerEntity player);

    Map<String, Object> readAmbientTechnicalState(ServerPlayerEntity player);
}
