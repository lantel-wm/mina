package mina.context;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class RecentEventBufferTest {
    @Test
    void snapshotKeepsMostRecentEventsWithinCapacity() {
        RecentEventBuffer buffer = new RecentEventBuffer(2);

        buffer.record("event.one", Map.of("value", 1));
        buffer.record("event.two", Map.of("value", 2));
        buffer.record("event.three", Map.of("value", 3));

        var snapshot = buffer.snapshot();
        assertEquals(2, snapshot.size());
        assertEquals("event.two", snapshot.get(0).get("kind"));
        assertEquals("event.three", snapshot.get(1).get("kind"));
    }

    @Test
    void snapshotForPlayerFiltersOutOtherPlayersEvents() {
        RecentEventBuffer buffer = new RecentEventBuffer(4);

        buffer.record("event.one", Map.of("player_uuid", "player-a", "value", 1));
        buffer.record("event.two", Map.of("player_uuid", "player-b", "value", 2));
        buffer.record("event.three", Map.of("player_uuid", "player-a", "value", 3));

        var snapshot = buffer.snapshotForPlayer("player-a");
        assertEquals(2, snapshot.size());
        assertEquals("event.one", snapshot.get(0).get("kind"));
        assertEquals("event.three", snapshot.get(1).get("kind"));
    }
}
