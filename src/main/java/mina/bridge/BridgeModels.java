package mina.bridge;

import mina.capability.CapabilityDefinition;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

public final class BridgeModels {
    private BridgeModels() {
    }

    public static final class TurnStartRequest {
        public String session_ref;
        public String turn_id;
        public PlayerPayload player;
        public ServerEnvPayload server_env;
        public Map<String, Object> scoped_snapshot;
        public List<VisibleCapabilityPayload> visible_capabilities;
        public LimitsPayload limits;
        public PendingConfirmationPayload pending_confirmation;
        public String user_message;
    }

    public static final class TurnResumeRequest {
        public String turn_id;
        public List<ActionResultPayload> action_results = new ArrayList<>();
    }

    public static final class TurnResponse {
        public String type;
        public String final_reply;
        public String continuation_id;
        public List<ActionRequestPayload> action_request_batch;
        public String pending_confirmation_id;
        public String pending_confirmation_effect_summary;
        public List<TraceEventPayload> trace_events;

        public boolean isFinalReply() {
            return "final_reply".equals(type);
        }

        public boolean isActionBatch() {
            return "action_request_batch".equals(type);
        }

        public boolean isProgressUpdate() {
            return "progress_update".equals(type);
        }
    }

    public static final class PlayerPayload {
        public String uuid;
        public String name;
        public String role;
        public String dimension;
        public Map<String, Object> position;
    }

    public static final class ServerEnvPayload {
        public boolean dedicated;
        public String motd;
        public int current_players;
        public int max_players;
        public boolean carpet_loaded;
        public boolean experimental_enabled;
        public boolean dynamic_scripting_enabled;
    }

    public static final class VisibleCapabilityPayload {
        public String id;
        public String kind;
        public String description;
        public String risk_class;
        public String execution_mode;
        public boolean requires_confirmation;
        public Map<String, Object> args_schema;
        public Map<String, Object> result_schema;
        public String domain;
        public boolean preferred;
        public String semantic_level;
        public String freshness_hint;

        public static VisibleCapabilityPayload fromDefinition(CapabilityDefinition definition) {
            VisibleCapabilityPayload payload = new VisibleCapabilityPayload();
            payload.id = definition.id();
            payload.kind = definition.kind();
            payload.description = definition.description();
            payload.risk_class = definition.riskClass().wireValue();
            payload.execution_mode = definition.executionMode();
            payload.requires_confirmation = definition.requiresConfirmation();
            payload.args_schema = definition.argsSchema();
            payload.result_schema = definition.resultSchema();
            payload.domain = definition.domain();
            payload.preferred = definition.preferred();
            payload.semantic_level = definition.semanticLevel();
            payload.freshness_hint = definition.freshnessHint();
            return payload;
        }
    }

    public static final class LimitsPayload {
        public int max_agent_steps;
        public int max_bridge_actions_per_turn;
        public int max_continuation_depth;
    }

    public static final class PendingConfirmationPayload {
        public String confirmation_id;
        public String effect_summary;
    }

    public static final class ActionRequestPayload {
        public String continuation_id;
        public String intent_id;
        public String capability_id;
        public String risk_class;
        public String effect_summary;
        public List<PreconditionPayload> preconditions;
        public Map<String, Object> arguments;
        public boolean requires_confirmation;
    }

    public static final class PreconditionPayload {
        public String path;
        public Object expected;
        public String reason;
    }

    public static final class ActionResultPayload {
        public String intent_id;
        public String status;
        public Map<String, Object> observations;
        public boolean preconditions_passed;
        public String side_effect_summary;
        public long timing_ms;
        public String state_fingerprint;
        public String error_message;
    }

    public static final class TraceEventPayload {
        public String status_label;
        public String status_tone;
        public String title;
        public String detail;
        public List<TraceChipPayload> secondary;
    }

    public static final class TraceChipPayload {
        public String label;
        public String tone;
    }
}
