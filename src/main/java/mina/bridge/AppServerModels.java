package mina.bridge;

import mina.capability.CapabilityDefinition;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class AppServerModels {
    private AppServerModels() {
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

    public static final class PreconditionPayload {
        public String path;
        public Object expected;
        public String reason;
    }

    public static final class ToolSpecPayload {
        public String id;
        public String kind;
        public String description;
        public String risk_class;
        public String execution_mode;
        public boolean requires_confirmation;
        public Map<String, Object> input_schema;
        public Map<String, Object> output_schema;
        public String freshness;
        public List<PreconditionPayload> preconditions = new ArrayList<>();
        public String domain;
        public boolean preferred;
        public String semantic_level;

        public static ToolSpecPayload fromDefinition(CapabilityDefinition definition) {
            ToolSpecPayload payload = new ToolSpecPayload();
            payload.id = definition.id();
            payload.kind = definition.kind();
            payload.description = definition.description();
            payload.risk_class = definition.riskClass().wireValue();
            payload.execution_mode = definition.executionMode();
            payload.requires_confirmation = definition.requiresConfirmation();
            payload.input_schema = definition.argsSchema();
            payload.output_schema = definition.resultSchema();
            payload.freshness = definition.freshnessHint();
            payload.domain = definition.domain();
            payload.preferred = definition.preferred();
            payload.semantic_level = definition.semanticLevel();
            return payload;
        }
    }

    public static final class LimitsPayload {
        public int max_agent_steps;
        public int max_bridge_actions_per_turn;
        public int max_continuation_depth;
    }

    public static final class TurnContextPayload {
        public PlayerPayload player;
        public ServerEnvPayload server_env;
        public Map<String, Object> scoped_snapshot;
        public List<ToolSpecPayload> tool_specs;
        public LimitsPayload limits;
    }

    public static final class ThreadStartParams {
        public String thread_id;
        public String player_uuid;
        public String player_name;
        public Map<String, Object> metadata = new LinkedHashMap<>();
    }

    public static final class ThreadResumeParams {
        public String thread_id;
    }

    public static final class ThreadUnsubscribeParams {
        public String thread_id;
    }

    public static final class ThreadMetadataUpdateParams {
        public String thread_id;
        public Map<String, Object> metadata = new LinkedHashMap<>();
    }

    public static final class ThreadShellCommandParams {
        public String thread_id;
        public String command;
    }

    public static final class TurnStartParams {
        public String thread_id;
        public String turn_id;
        public String user_message;
        public TurnContextPayload context;
    }

    public static final class ThreadRollbackParams {
        public String thread_id;
        public int num_turns;
    }

    public static final class TurnSteerInputPayload {
        public String type = "text";
        public String text;
    }

    public static final class TurnSteerParams {
        public String thread_id;
        public String expected_turn_id;
        public List<TurnSteerInputPayload> input = new ArrayList<>();
    }

    public static final class CommandExecTerminalSize {
        public int cols;
        public int rows;
    }

    public static final class CommandExecParams {
        public List<String> command = new ArrayList<>();
        public String cwd;
        public Map<String, Object> env;
        public String process_id;
        public Boolean stream_stdin;
        public Boolean stream_stdout_stderr;
        public Boolean tty;
        public Integer timeout_ms;
        public Boolean disable_timeout;
        public Integer output_bytes_cap;
        public Boolean disable_output_cap;
        public CommandExecTerminalSize size;
    }

    public static final class CommandExecWriteParams {
        public String process_id;
        public String delta_base64;
        public Boolean close_stdin;
    }

    public static final class CommandExecTerminateParams {
        public String process_id;
    }

    public static final class CommandExecResizeParams {
        public String process_id;
        public CommandExecTerminalSize size;
    }

    public static final class ThreadPayload {
        public String thread_id;
        public String player_uuid;
        public String player_name;
        public String status;
        public Map<String, Object> metadata;
        public String created_at;
        public String updated_at;
    }

    public static final class TurnPayload {
        public String thread_id;
        public String turn_id;
        public String status;
        public String created_at;
        public String updated_at;
        public String final_reply;
    }

    public static final class ToolCallRequestPayload {
        public String item_id;
        public String thread_id;
        public String turn_id;
        public String tool_id;
        public Map<String, Object> arguments;
        public String risk_class;
        public String execution_mode;
        public String effect_summary;
        public boolean requires_confirmation;
        public List<PreconditionPayload> preconditions;
        public String source_tool_id;
    }

    public static final class ToolResultParams {
        public String thread_id;
        public String turn_id;
        public String item_id;
        public String tool_id;
        public String status;
        public Map<String, Object> observations;
        public boolean preconditions_passed;
        public String side_effect_summary;
        public int timing_ms;
        public String state_fingerprint;
        public String error_message;
    }

    public static final class ApprovalRequestPayload {
        public String approval_id;
        public String item_id;
        public String thread_id;
        public String turn_id;
        public String effect_summary;
        public String reason;
        public String risk_class;
        public ToolCallRequestPayload tool_call;
    }

    public static final class ApprovalResponseParams {
        public String thread_id;
        public String turn_id;
        public String approval_id;
        public boolean approved;
        public String reason;
    }

    public static final class ItemDeltaPayload {
        public String thread_id;
        public String turn_id;
        public String item_id;
        public String delta;
    }

    public static final class WarningPayload {
        public String thread_id;
        public String turn_id;
        public String message;
        public String detail;
    }
}
