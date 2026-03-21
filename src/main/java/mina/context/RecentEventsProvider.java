package mina.context;

import net.minecraft.server.network.ServerPlayerEntity;

import java.util.List;
import java.util.Map;

public final class RecentEventsProvider {
    private final RecentEventBuffer buffer;

    public RecentEventsProvider(RecentEventBuffer buffer) {
        this.buffer = buffer;
    }

    public List<Map<String, Object>> collect(ServerPlayerEntity player) {
        return buffer.snapshotForPlayer(player.getUuidAsString());
    }
}
