package mina.execution;

import mina.bridge.BridgeModels;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public final class PendingConfirmationRegistry {
    private final Map<String, BridgeModels.PendingConfirmationPayload> confirmations = new ConcurrentHashMap<>();

    public BridgeModels.PendingConfirmationPayload get(String sessionRef) {
        return confirmations.get(sessionRef);
    }

    public void put(String sessionRef, String confirmationId, String effectSummary) {
        BridgeModels.PendingConfirmationPayload payload = new BridgeModels.PendingConfirmationPayload();
        payload.confirmation_id = confirmationId;
        payload.effect_summary = effectSummary;
        confirmations.put(sessionRef, payload);
    }

    public void clear(String sessionRef) {
        confirmations.remove(sessionRef);
    }

    public void clearAll() {
        confirmations.clear();
    }
}
