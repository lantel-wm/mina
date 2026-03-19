package mina.execution;

import com.google.gson.internal.LinkedTreeMap;
import mina.MinaMod;
import mina.bridge.AgentServiceClient;
import mina.bridge.BridgeModels;
import mina.capability.CapabilityDefinition;
import mina.capability.CapabilityExecutorRegistry;
import mina.capability.CapabilityResult;
import mina.chat.MinaChatRenderer;
import mina.config.MinaConfig;
import mina.context.GameContextCollector;
import mina.context.TurnContext;
import mina.policy.ExecutionGuard;
import mina.policy.ExecutionGuard.Decision;
import mina.policy.PlayerRole;
import mina.util.ServerExecutor;
import com.mojang.brigadier.exceptions.CommandSyntaxException;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;

public final class TurnCoordinator {
    private final MinaConfig config;
    private final AgentServiceClient agentServiceClient;
    private final GameContextCollector contextCollector;
    private final CapabilityExecutorRegistry capabilityRegistry;
    private final ExecutionGuard executionGuard;
    private final PendingTurnRegistry pendingTurnRegistry;
    private final ExecutorService ioExecutor;

    public TurnCoordinator(
            MinaConfig config,
            AgentServiceClient agentServiceClient,
            GameContextCollector contextCollector,
            CapabilityExecutorRegistry capabilityRegistry,
            ExecutionGuard executionGuard,
            PendingTurnRegistry pendingTurnRegistry,
            ExecutorService ioExecutor
    ) {
        this.config = config;
        this.agentServiceClient = agentServiceClient;
        this.contextCollector = contextCollector;
        this.capabilityRegistry = capabilityRegistry;
        this.executionGuard = executionGuard;
        this.pendingTurnRegistry = pendingTurnRegistry;
        this.ioExecutor = ioExecutor;
    }

    public boolean submitTurn(ServerCommandSource source, String userMessage, Runnable acceptedCallback) throws CommandSyntaxException {
        ServerPlayerEntity player = source.getPlayerOrThrow();
        UUID playerId = player.getUuid();
        String turnId = UUID.randomUUID().toString();

        if (!pendingTurnRegistry.tryOpen(playerId, turnId)) {
            source.sendError(net.minecraft.text.Text.literal("Mina is already handling another request for you."));
            return false;
        }

        acceptedCallback.run();
        ioExecutor.submit(() -> runTurn(playerId, turnId, userMessage, source.getServer()));
        return true;
    }

    private void runTurn(UUID playerId, String turnId, String userMessage, net.minecraft.server.MinecraftServer server) {
        try {
            TurnContext turnContext = ServerExecutor.call(server, () -> collectTurnContext(playerId, server)).join();
            BridgeModels.TurnStartRequest startRequest = toStartRequest(turnContext, turnId, userMessage);
            BridgeModels.TurnResponse response = agentServiceClient.startTurn(startRequest);

            int continuationDepth = 0;
            int actionCount = 0;

            while (response != null) {
                if (response.isFinalReply()) {
                    deliverReply(server, playerId, response.final_reply);
                    return;
                }

                if (!response.isActionBatch() || response.action_request_batch == null || response.action_request_batch.isEmpty()) {
                    throw new IllegalStateException("Agent service returned neither a final reply nor an action batch.");
                }

                if (++continuationDepth > config.maxContinuationDepth()) {
                    throw new IllegalStateException("Continuation depth exceeded configured limit.");
                }

                BridgeModels.TurnResumeRequest resumeRequest = new BridgeModels.TurnResumeRequest();
                resumeRequest.turn_id = turnId;

                for (BridgeModels.ActionRequestPayload actionRequest : response.action_request_batch) {
                    actionCount++;
                    int currentActionCount = actionCount;
                    BridgeModels.ActionResultPayload actionResult = ServerExecutor.call(
                            server,
                            () -> executeAction(server, playerId, actionRequest, currentActionCount)
                    ).join();
                    resumeRequest.action_results.add(actionResult);
                }

                response = agentServiceClient.resumeTurn(response.continuation_id, resumeRequest);
            }

            throw new IllegalStateException("Agent service returned an empty response.");
        } catch (Exception exception) {
            MinaMod.LOGGER.error("Mina turn {} failed", turnId, exception);
            deliverReply(server, playerId, "Mina failed to complete the request: " + exception.getMessage());
        } finally {
            pendingTurnRegistry.close(playerId, turnId);
        }
    }

    private TurnContext collectTurnContext(UUID playerId, net.minecraft.server.MinecraftServer server) {
        ServerPlayerEntity player = server.getPlayerManager().getPlayer(playerId);
        if (player == null) {
            throw new IllegalStateException("Player disconnected before Mina could process the request.");
        }

        return contextCollector.collect(player);
    }

    private BridgeModels.TurnStartRequest toStartRequest(TurnContext context, String turnId, String userMessage) {
        BridgeModels.TurnStartRequest request = new BridgeModels.TurnStartRequest();
        request.session_ref = context.sessionRef();
        request.turn_id = turnId;
        request.player = context.playerPayload();
        request.server_env = context.serverEnvPayload();
        request.scoped_snapshot = context.scopedSnapshot();
        request.visible_capabilities = context.visibleCapabilities().stream()
                .map(BridgeModels.VisibleCapabilityPayload::fromDefinition)
                .toList();

        BridgeModels.LimitsPayload limits = new BridgeModels.LimitsPayload();
        limits.max_agent_steps = config.maxAgentSteps();
        limits.max_bridge_actions_per_turn = config.maxBridgeActionsPerTurn();
        limits.max_continuation_depth = config.maxContinuationDepth();
        request.limits = limits;
        request.pending_confirmation = null;
        request.user_message = userMessage;
        return request;
    }

    private BridgeModels.ActionResultPayload executeAction(
            net.minecraft.server.MinecraftServer server,
            UUID playerId,
            BridgeModels.ActionRequestPayload actionRequest,
            int actionCount
    ) {
        long startedAt = System.nanoTime();
        ServerPlayerEntity player = server.getPlayerManager().getPlayer(playerId);

        if (player == null) {
            return rejectedResult(actionRequest, "player_unavailable", false, "Player disconnected.");
        }

        TurnContext turnContext = contextCollector.collect(player);
        Decision decision = executionGuard.evaluate(player, turnContext, actionRequest, actionCount);
        if (!decision.allowed()) {
            return rejectedResult(actionRequest, decision.status(), decision.preconditionsPassed(), decision.message(), turnContext.stateFingerprint());
        }

        try {
            CapabilityResult result = capabilityRegistry.execute(actionRequest.capability_id, player, actionRequest.arguments);
            BridgeModels.ActionResultPayload payload = new BridgeModels.ActionResultPayload();
            payload.intent_id = actionRequest.intent_id;
            payload.status = "executed";
            payload.observations = result.observations();
            payload.preconditions_passed = true;
            payload.side_effect_summary = result.sideEffectSummary();
            payload.timing_ms = (System.nanoTime() - startedAt) / 1_000_000L;
            payload.state_fingerprint = turnContext.stateFingerprint();
            return payload;
        } catch (Exception exception) {
            return rejectedResult(
                    actionRequest,
                    "execution_failed",
                    true,
                    exception.getMessage(),
                    turnContext.stateFingerprint()
            );
        }
    }

    private BridgeModels.ActionResultPayload rejectedResult(
            BridgeModels.ActionRequestPayload actionRequest,
            String status,
            boolean preconditionsPassed,
            String message
    ) {
        return rejectedResult(actionRequest, status, preconditionsPassed, message, null);
    }

    private BridgeModels.ActionResultPayload rejectedResult(
            BridgeModels.ActionRequestPayload actionRequest,
            String status,
            boolean preconditionsPassed,
            String message,
            String stateFingerprint
    ) {
        BridgeModels.ActionResultPayload payload = new BridgeModels.ActionResultPayload();
        payload.intent_id = actionRequest.intent_id;
        payload.status = status;
        payload.observations = Map.of("message", message);
        payload.preconditions_passed = preconditionsPassed;
        payload.side_effect_summary = message;
        payload.timing_ms = 0L;
        payload.state_fingerprint = stateFingerprint;
        payload.error_message = message;
        return payload;
    }

    private void deliverReply(net.minecraft.server.MinecraftServer server, UUID playerId, String message) {
        server.execute(() -> {
            ServerPlayerEntity player = server.getPlayerManager().getPlayer(playerId);
            if (player == null) {
                MinaMod.LOGGER.info("Skipping Mina reply delivery because player {} is offline", playerId);
                return;
            }

            MinaChatRenderer.sendReply(player, message);
        });
    }
}
