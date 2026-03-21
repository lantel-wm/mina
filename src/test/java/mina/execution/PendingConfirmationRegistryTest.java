package mina.execution;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class PendingConfirmationRegistryTest {
    @Test
    void putGetAndClearConfirmationBySession() {
        PendingConfirmationRegistry registry = new PendingConfirmationRegistry();

        registry.put("session-1", "confirm-1", "Move the rare item.");
        var payload = registry.get("session-1");

        assertNotNull(payload);
        assertEquals("confirm-1", payload.confirmation_id);
        assertEquals("Move the rare item.", payload.effect_summary);

        registry.clear("session-1");
        assertNull(registry.get("session-1"));
    }
}
