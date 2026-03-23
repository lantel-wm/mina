package mina.execution;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DevTurnLogTest {
    @Test
    void recordsAcceptedCompletedAndFailedTurnEvents() throws Exception {
        Path runDir = Files.createTempDirectory("mina-dev-turn-log");
        DevTurnLog log = DevTurnLog.forRunDirectory(runDir, true);
        Instant startedAt = Instant.parse("2026-03-23T03:00:00Z");
        Instant endedAt = Instant.parse("2026-03-23T03:00:02Z");

        log.recordAccepted("turn-1", "session-1", "Steve", "hello Mina", startedAt);
        log.recordCompleted("turn-1", "session-1", "Steve", "hello Mina", startedAt, endedAt, "reply\nline");
        log.recordFailed("turn-2", "session-2", "Alex", "where are we", startedAt, endedAt, "boom", null);

        Path turnsPath = runDir.resolve("mina-dev").resolve("turns.jsonl");
        List<String> lines = Files.readAllLines(turnsPath);
        assertEquals(3, lines.size());

        JsonObject accepted = JsonParser.parseString(lines.get(0)).getAsJsonObject();
        assertEquals("turn-1", accepted.get("turn_id").getAsString());
        assertEquals("session-1", accepted.get("session_ref").getAsString());
        assertEquals("Steve", accepted.get("player_name").getAsString());
        assertEquals("accepted", accepted.get("status").getAsString());
        assertEquals("2026-03-23T03:00:00Z", accepted.get("started_at").getAsString());
        assertTrue(accepted.get("ended_at").isJsonNull());
        assertTrue(accepted.get("final_reply_preview").isJsonNull());

        JsonObject completed = JsonParser.parseString(lines.get(1)).getAsJsonObject();
        assertEquals("completed", completed.get("status").getAsString());
        assertEquals("2026-03-23T03:00:02Z", completed.get("ended_at").getAsString());
        assertEquals("reply line", completed.get("final_reply_preview").getAsString());
        assertTrue(completed.get("error").isJsonNull());

        JsonObject failed = JsonParser.parseString(lines.get(2)).getAsJsonObject();
        assertEquals("turn-2", failed.get("turn_id").getAsString());
        assertEquals("failed", failed.get("status").getAsString());
        assertEquals("boom", failed.get("error").getAsString());
        assertTrue(failed.get("final_reply_preview").isJsonNull());
    }

    @Test
    void disabledLogDoesNotCreateFiles() throws Exception {
        Path runDir = Files.createTempDirectory("mina-dev-turn-log-disabled");
        DevTurnLog log = DevTurnLog.forRunDirectory(runDir, false);

        log.recordAccepted("turn-1", "session-1", "Steve", "hello Mina", Instant.parse("2026-03-23T03:00:00Z"));

        assertFalse(Files.exists(runDir.resolve("mina-dev").resolve("turns.jsonl")));
    }
}
