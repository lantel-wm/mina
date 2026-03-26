package mina.execution;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import mina.MinaMod;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

public final class DevTurnLog {
    private static final Gson GSON = new GsonBuilder().serializeNulls().disableHtmlEscaping().create();
    private static final int MAX_PREVIEW_CHARS = 600;

    private final Path turnsPath;
    private final boolean enabled;

    private DevTurnLog(Path turnsPath, boolean enabled) {
        this.turnsPath = turnsPath;
        this.enabled = enabled;
    }

    public static DevTurnLog forRunDirectory(Path runDirectory, boolean enabled) {
        return new DevTurnLog(runDirectory.resolve("mina-dev").resolve("turns.jsonl"), enabled);
    }

    public static DevTurnLog disabled() {
        return new DevTurnLog(Path.of("mina-dev", "turns.jsonl"), false);
    }

    public void recordAccepted(
            String turnId,
            String threadId,
            String playerName,
            String userMessage,
            Instant startedAt
    ) {
        append(record(
                turnId,
                threadId,
                playerName,
                userMessage,
                "accepted",
                startedAt,
                null,
                null,
                null
        ));
    }

    public void recordCompleted(
            String turnId,
            String threadId,
            String playerName,
            String userMessage,
            Instant startedAt,
            Instant endedAt,
            String finalReplyPreview
    ) {
        append(record(
                turnId,
                threadId,
                playerName,
                userMessage,
                "completed",
                startedAt,
                endedAt,
                null,
                finalReplyPreview
        ));
    }

    public void recordFailed(
            String turnId,
            String threadId,
            String playerName,
            String userMessage,
            Instant startedAt,
            Instant endedAt,
            String error,
            String finalReplyPreview
    ) {
        append(record(
                turnId,
                threadId,
                playerName,
                userMessage,
                "failed",
                startedAt,
                endedAt,
                error,
                finalReplyPreview
        ));
    }

    private Map<String, Object> record(
            String turnId,
            String threadId,
            String playerName,
            String userMessage,
            String status,
            Instant startedAt,
            Instant endedAt,
            String error,
            String finalReplyPreview
    ) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("turn_id", turnId);
        payload.put("thread_id", threadId);
        payload.put("player_name", playerName);
        payload.put("user_message", userMessage);
        payload.put("status", status);
        payload.put("started_at", startedAt == null ? null : startedAt.toString());
        payload.put("ended_at", endedAt == null ? null : endedAt.toString());
        payload.put("error", truncate(error));
        payload.put("final_reply_preview", truncate(finalReplyPreview));
        return payload;
    }

    private synchronized void append(Map<String, Object> payload) {
        if (!enabled) {
            return;
        }

        try {
            Files.createDirectories(turnsPath.getParent());
            Files.writeString(
                    turnsPath,
                    GSON.toJson(payload) + System.lineSeparator(),
                    StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.APPEND
            );
        } catch (IOException exception) {
            MinaMod.LOGGER.warn("Failed to append Mina dev turn log {}", turnsPath, exception);
        }
    }

    private String truncate(String value) {
        if (value == null) {
            return null;
        }
        String normalized = value.replace('\r', ' ').replace('\n', ' ').trim();
        if (normalized.length() <= MAX_PREVIEW_CHARS) {
            return normalized;
        }
        return normalized.substring(0, MAX_PREVIEW_CHARS);
    }
}
