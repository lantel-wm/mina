package mina.execution;

import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

public final class PendingTurnRegistry {
    private final ConcurrentMap<UUID, String> activeTurns = new ConcurrentHashMap<>();

    public boolean tryOpen(UUID playerId, String turnId) {
        return activeTurns.putIfAbsent(playerId, turnId) == null;
    }

    public void close(UUID playerId, String turnId) {
        activeTurns.remove(playerId, turnId);
    }

    public boolean hasActiveTurn(UUID playerId) {
        return activeTurns.containsKey(playerId);
    }

    public void closeAll() {
        activeTurns.clear();
    }
}
