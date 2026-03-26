package mina.context;

import mina.companion.PlayerActivityTracker;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class WorldReadingHeuristicsTest {
    @Test
    void worldTimePhaseUsesExpectedDayBoundaries() {
        assertEquals("dawn", WorldStateProvider.timePhase(0));
        assertEquals("day", WorldStateProvider.timePhase(1_000));
        assertEquals("dusk", WorldStateProvider.timePhase(12_000));
        assertEquals("night", WorldStateProvider.timePhase(13_000));
        assertEquals("dawn", WorldStateProvider.timePhase(24_000));
    }

    @Test
    void interactableClassificationSeparatesCoreCategories() {
        assertEquals("container", InteractableScanProvider.classifyBlock("minecraft:chest"));
        assertEquals("workstation", InteractableScanProvider.classifyBlock("minecraft:crafting_table"));
        assertEquals("shelter", InteractableScanProvider.classifyBlock("minecraft:red_bed"));
        assertEquals("danger_block", InteractableScanProvider.classifyBlock("minecraft:spawner"));
        assertEquals("other", InteractableScanProvider.classifyBlock("minecraft:stone"));
    }

    @Test
    void threatDirectionMapsCompassQuadrants() {
        assertEquals("south", ThreatAssessmentProvider.directionFromDelta(0.0, 3.0));
        assertEquals("west", ThreatAssessmentProvider.directionFromDelta(-3.0, 0.0));
        assertEquals("north", ThreatAssessmentProvider.directionFromDelta(0.0, -3.0));
        assertEquals("east", ThreatAssessmentProvider.directionFromDelta(3.0, 0.0));
        assertEquals("nearby", ThreatAssessmentProvider.directionFromDelta(0.2, 0.3));
    }

    @Test
    void riskAssessmentEscalatesWhenPlayerIsLowAndSurrounded() {
        Map<String, Object> riskState = RiskStateProvider.assess(
                Map.of(
                        "core_status", Map.of("health", 4.0F, "hunger", 4),
                        "recent_damage_state", Map.of("recently_hurt", true, "long_in_danger", true)
                ),
                Map.of(
                        "hostile_count", 3,
                        "explosive_count", 1,
                        "nearest_threat", Map.of("name", "Creeper")
                ),
                Map.of("hazard_summary", Map.of("hazards", java.util.List.of("lava"))),
                Map.of("is_alone", true)
        );

        assertEquals("critical", riskState.get("level"));
        assertTrue((Boolean) riskState.get("immediate_action_needed"));
        assertEquals("Creeper", ((Map<?, ?>) riskState.get("highest_threat")).get("name"));
    }

    @Test
    void environmentSummaryIncludesBiomeAndLocalTreeSignals() {
        String summary = EnvironmentAssessmentProvider.summarizeLocation(
                "surface",
                "minecraft:dark_forest",
                new EnvironmentAssessmentProvider.LocalTerrainProfile(
                        "minecraft:dark_oak_leaves",
                        "leaves",
                        "minecraft:air",
                        "minecraft:air",
                        12,
                        2,
                        3,
                        5,
                        true
                )
        );

        assertTrue(summary.contains("surface terrain"));
        assertTrue(summary.contains("minecraft:dark_forest"));
        assertTrue(summary.contains("minecraft:dark_oak_leaves"));
        assertTrue(summary.contains("12 nearby leaf blocks"));
    }

    @Test
    void recentEventTrackerClassifiesImportanceAndStaleness() {
        assertEquals("high", RecentEventTracker.importanceForKind("player_died"));
        assertEquals("medium", RecentEventTracker.importanceForKind("player_hurt"));
        assertEquals("low", RecentEventTracker.importanceForKind("equipment_changed"));

        assertEquals("fresh", RecentEventTracker.stalenessForAge(Duration.ofSeconds(8)));
        assertEquals("recent", RecentEventTracker.stalenessForAge(Duration.ofSeconds(60)));
        assertEquals("stale", RecentEventTracker.stalenessForAge(Duration.ofMinutes(5)));
    }

    @Test
    void handsSnapshotAllowsEmptyHandsWithoutThrowing() {
        Map<String, Object> hands = PlayerStateProvider.handsSnapshot(null, null, false);

        assertTrue(hands.containsKey("main_hand"));
        assertTrue(hands.containsKey("off_hand"));
        assertNull(hands.get("main_hand"));
        assertNull(hands.get("off_hand"));
        assertEquals(false, hands.get("using_item"));
    }

    @Test
    void playerActivityClassificationRecognizesCoreFamilies() {
        assertEquals(
                "mining_like",
                PlayerActivityTracker.classifyFamily(
                        "minecraft:iron_pickaxe",
                        "minecraft:stone",
                        "surface",
                        "low"
                )
        );
        assertEquals(
                "building_like",
                PlayerActivityTracker.classifyFamily(
                        "minecraft:oak_planks",
                        "minecraft:oak_planks",
                        "base",
                        "low"
                )
        );
        assertEquals(
                "harvesting_like",
                PlayerActivityTracker.classifyFamily(
                        "minecraft:iron_hoe",
                        "minecraft:wheat",
                        "surface",
                        "low"
                )
        );
        assertEquals(
                "combat_grind",
                PlayerActivityTracker.classifyFamily(
                        "minecraft:diamond_sword",
                        "minecraft:air",
                        "surface",
                        "critical"
                )
        );
    }
}
