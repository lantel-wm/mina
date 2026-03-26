package mina.policy;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import mina.bridge.AppServerModels;
import mina.capability.CapabilityDefinition;
import mina.config.MinaConfig;
import mina.context.TurnContext;
import net.minecraft.server.network.ServerPlayerEntity;

import java.util.List;
import java.util.Map;
import java.util.Objects;

public final class ExecutionGuard {
    private final MinaConfig config;
    private final PermissionResolver permissionResolver;
    private final Gson gson;

    public ExecutionGuard(MinaConfig config, PermissionResolver permissionResolver) {
        this.config = config;
        this.permissionResolver = permissionResolver;
        this.gson = new GsonBuilder().serializeNulls().create();
    }

    public Decision evaluate(
            ServerPlayerEntity player,
            TurnContext context,
            AppServerModels.ToolCallRequestPayload actionRequest,
            int actionCount
    ) {
        if (actionCount > config.maxBridgeActionsPerTurn()) {
            return new Decision(false, false, "action_budget_exhausted", "Bridge action budget exhausted.");
        }

        PlayerRole role = permissionResolver.resolveRole(player);
        RiskClass riskClass = RiskClass.fromWire(actionRequest.risk_class);
        if (!permissionResolver.allowsRisk(role, riskClass)) {
            return new Decision(false, true, "role_forbidden", "Your current role cannot run that risk class.");
        }

        if (actionRequest.requires_confirmation) {
            return new Decision(false, true, "confirmation_required", "This action requires natural-language confirmation.");
        }

        if (!isVisible(context.visibleCapabilities(), actionRequest.tool_id)) {
            return new Decision(false, true, "capability_hidden", "The requested capability is not visible in the current context.");
        }

        boolean preconditionsPassed = preconditionsPass(actionRequest.preconditions, context.scopedSnapshot());
        if (!preconditionsPassed) {
            return new Decision(false, false, "precondition_failed", "Execution was denied because state changed since planning.");
        }

        return new Decision(true, true, "allowed", "Allowed.");
    }

    private boolean isVisible(List<CapabilityDefinition> visibleCapabilities, String capabilityId) {
        return visibleCapabilities.stream().anyMatch(definition -> definition.id().equals(capabilityId));
    }

    private boolean preconditionsPass(List<AppServerModels.PreconditionPayload> preconditions, Map<String, Object> snapshot) {
        if (preconditions == null || preconditions.isEmpty()) {
            return true;
        }

        for (AppServerModels.PreconditionPayload precondition : preconditions) {
            Object actual = resolvePath(snapshot, precondition.path);
            if (!Objects.equals(gson.toJson(actual), gson.toJson(precondition.expected))) {
                return false;
            }
        }

        return true;
    }

    @SuppressWarnings("unchecked")
    private Object resolvePath(Map<String, Object> snapshot, String path) {
        if (path == null || path.isBlank()) {
            return null;
        }

        Object current = snapshot;
        for (String segment : path.split("\\.")) {
            if (!(current instanceof Map<?, ?> map)) {
                return null;
            }
            current = map.get(segment);
        }
        return current;
    }

    public record Decision(
            boolean allowed,
            boolean preconditionsPassed,
            String status,
            String message
    ) {
    }
}
