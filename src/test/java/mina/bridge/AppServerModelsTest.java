package mina.bridge;

import mina.capability.CapabilityDefinition;
import mina.capability.CapabilityResult;
import mina.policy.PlayerRole;
import mina.policy.RiskClass;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

class AppServerModelsTest {
    @Test
    void toolSpecPayloadIncludesSchemasFromDefinition() {
        Map<String, Object> argsSchema = Map.of(
                "block_pos",
                Map.of(
                        "type", "object",
                        "required", false,
                        "fields", Map.of("x", "integer", "y", "integer", "z", "integer")
                )
        );
        Map<String, Object> resultSchema = Map.of(
                "pos", "object{x,y,z}",
                "block_name", "string"
        );
        CapabilityDefinition definition = new CapabilityDefinition(
                "game.target_block.read",
                "tool",
                "Inspect the targeted block.",
                RiskClass.READ_ONLY,
                "server_main_thread",
                false,
                argsSchema,
                resultSchema,
                "world",
                true,
                "semantic",
                "ambient",
                (player, role) -> role == PlayerRole.READ_ONLY,
                (player, arguments) -> new CapabilityResult(Map.of("target_found", true), "ok")
        );

        AppServerModels.ToolSpecPayload payload = AppServerModels.ToolSpecPayload.fromDefinition(definition);

        assertEquals("game.target_block.read", payload.id);
        assertEquals("tool", payload.kind);
        assertEquals("Inspect the targeted block.", payload.description);
        assertEquals("read_only", payload.risk_class);
        assertEquals("server_main_thread", payload.execution_mode);
        assertFalse(payload.requires_confirmation);
        assertSame(argsSchema, payload.input_schema);
        assertSame(resultSchema, payload.output_schema);
        assertEquals("world", payload.domain);
        assertTrue(payload.preferred);
        assertEquals("semantic", payload.semantic_level);
        assertEquals("ambient", payload.freshness);
    }
}
