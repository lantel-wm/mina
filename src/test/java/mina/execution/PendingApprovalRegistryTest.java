package mina.execution;

import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class PendingApprovalRegistryTest {
    @Test
    void pendingApprovalCanBeStoredAndCleared() {
        PendingApprovalRegistry registry = new PendingApprovalRegistry();
        UUID playerId = UUID.randomUUID();

        PendingApprovalRegistry.PendingApproval pending = registry.put(
                playerId,
                "thread-1",
                "turn-1",
                "approval-1",
                "Break the block."
        );

        assertNotNull(pending);
        assertEquals("thread-1", pending.threadId());
        assertEquals("approval-1", pending.approvalId());
        assertEquals("Break the block.", pending.effectSummary());
        assertEquals(pending, registry.get(playerId));

        registry.clear(playerId);
        assertNull(registry.get(playerId));
    }
}
