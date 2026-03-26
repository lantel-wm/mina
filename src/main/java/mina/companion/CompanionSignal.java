package mina.companion;

import mina.bridge.AppServerModels;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;

public record CompanionSignal(
        String signalId,
        String kind,
        String importance,
        Instant occurredAt,
        long occurredTick,
        long availableAfterTick,
        long expiresAfterTick,
        String dedupeKey,
        Map<String, Object> payload
) {
    public CompanionSignal {
        signalId = signalId == null || signalId.isBlank() ? UUID.randomUUID().toString() : signalId;
        importance = importance == null || importance.isBlank() ? "low" : importance;
        payload = payload == null ? Map.of() : Map.copyOf(payload);
        dedupeKey = dedupeKey == null || dedupeKey.isBlank() ? kind : dedupeKey;
    }

    public int priority() {
        return switch (kind) {
            case "danger_warning" -> 100;
            case "death_followup" -> 90;
            case "advancement_celebration" -> 60;
            case "milestone_encouragement" -> 55;
            case "player_join_greeting" -> 40;
            case "repetition_comfort" -> 30;
            default -> 10;
        };
    }

    public boolean isReady(long currentTick) {
        return currentTick >= availableAfterTick;
    }

    public boolean isExpired(long currentTick) {
        return expiresAfterTick >= 0 && currentTick > expiresAfterTick;
    }

    public AppServerModels.CompanionSignalPayload toPayload() {
        AppServerModels.CompanionSignalPayload result = new AppServerModels.CompanionSignalPayload();
        result.signal_id = signalId;
        result.kind = kind;
        result.importance = importance;
        result.occurred_at = occurredAt.toString();
        result.payload = new LinkedHashMap<>(payload);
        return result;
    }

    public CompanionSignal withPayload(Map<String, Object> nextPayload) {
        return new CompanionSignal(
                signalId,
                kind,
                importance,
                occurredAt,
                occurredTick,
                availableAfterTick,
                expiresAfterTick,
                dedupeKey,
                nextPayload
        );
    }

    public boolean sameIdentity(CompanionSignal other) {
        if (other == null) {
            return false;
        }
        return Objects.equals(dedupeKey, other.dedupeKey);
    }
}
