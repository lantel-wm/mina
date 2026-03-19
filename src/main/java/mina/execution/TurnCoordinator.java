package mina.execution;

import com.google.gson.internal.LinkedTreeMap;
import mina.MinaMod;
import mina.bridge.AgentServiceClient;
import mina.bridge.BridgeModels;
import mina.capability.CapabilityDefinition;
import mina.capability.CapabilityExecutorRegistry;
import mina.capability.CapabilityResult;
import mina.chat.MinaChatRenderer;
import mina.chat.MinaChatRenderer.ActionTracePresentation;
import mina.chat.MinaChatRenderer.ChipTone;
import mina.chat.MinaChatRenderer.ReplyPresentation;
import mina.chat.MinaChatRenderer.SecondaryChip;
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
import java.util.concurrent.ThreadLocalRandom;

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
            source.sendError(MinaChatRenderer.commandError("我这边还在看上一件事，先别急。"));
            return false;
        }

        acceptedCallback.run();
        ioExecutor.submit(() -> runTurn(playerId, turnId, userMessage, source.getServer()));
        return true;
    }

    private void runTurn(UUID playerId, String turnId, String userMessage, net.minecraft.server.MinecraftServer server) {
        long turnStartedAt = System.nanoTime();
        try {
            TurnContext turnContext = ServerExecutor.call(server, () -> collectTurnContext(playerId, server)).join();
            BridgeModels.TurnStartRequest startRequest = toStartRequest(turnContext, turnId, userMessage);
            BridgeModels.TurnResponse response = agentServiceClient.startTurn(startRequest);

            int continuationDepth = 0;
            int actionCount = 0;
            List<String> executedCapabilityIds = new ArrayList<>();

            while (response != null) {
                deliverResponseTraceEvents(server, playerId, response);

                if (response.isFinalReply()) {
                    deliverReply(
                            server,
                            playerId,
                            buildReplyPresentation(response, turnStartedAt, actionCount, continuationDepth, executedCapabilityIds)
                    );
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
                    executedCapabilityIds.add(actionRequest.capability_id);
                    deliverActionTrace(server, playerId, actionStartedPresentation(actionRequest, currentActionCount));
                    BridgeModels.ActionResultPayload actionResult = ServerExecutor.call(
                            server,
                            () -> executeAction(server, playerId, actionRequest, currentActionCount)
                    ).join();
                    deliverActionTrace(server, playerId, actionFinishedPresentation(actionRequest, actionResult, currentActionCount));
                    resumeRequest.action_results.add(actionResult);
                }

                response = agentServiceClient.resumeTurn(response.continuation_id, resumeRequest);
            }

            throw new IllegalStateException("Agent service returned an empty response.");
        } catch (Exception exception) {
            MinaMod.LOGGER.error("Mina turn {} failed", turnId, exception);
            deliverError(server, playerId, "刚刚这一步有点不对，我再处理也许会更稳。原因：" + exception.getMessage());
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

    private ReplyPresentation buildReplyPresentation(
            BridgeModels.TurnResponse response,
            long turnStartedAt,
            int actionCount,
            int continuationDepth,
            List<String> executedCapabilityIds
    ) {
        boolean requiresConfirmation = response.pending_confirmation_id != null && !response.pending_confirmation_id.isBlank();
        List<SecondaryChip> chips = new ArrayList<>();

        if (requiresConfirmation) {
            chips.add(new SecondaryChip("需要确认", ChipTone.WARNING));
            chips.add(new SecondaryChip("高风险计划", ChipTone.WARNING));
        } else if (actionCount > 0) {
            chips.add(new SecondaryChip("已完成", ChipTone.SUCCESS));
            chips.add(new SecondaryChip(actionCount + " 次执行", ChipTone.INFO));
        } else {
            chips.add(new SecondaryChip("已回复", ChipTone.INFO));
            chips.add(new SecondaryChip("纯对话", ChipTone.MUTED));
        }

        chips.add(new SecondaryChip(formatElapsed(System.nanoTime() - turnStartedAt), ChipTone.MUTED));
        if (continuationDepth > 0) {
            chips.add(new SecondaryChip(continuationDepth + " 轮规划", ChipTone.MUTED));
        }

        String title = requiresConfirmation ? "需要确认的计划" : buildReplyTitle(executedCapabilityIds);
        String note = requiresConfirmation
                ? "你点头之后我再继续。也可以直接让我改一下。"
                : null;
        ChipTone noteTone = requiresConfirmation ? ChipTone.WARNING : ChipTone.MUTED;

        return new ReplyPresentation(title, response.final_reply, chips, note, noteTone);
    }

    private String buildReplyTitle(List<String> executedCapabilityIds) {
        if (executedCapabilityIds == null || executedCapabilityIds.isEmpty()) {
            return "";
        }

        String lastCapabilityId = executedCapabilityIds.get(executedCapabilityIds.size() - 1);
        return switch (lastCapabilityId) {
            case "game.player_snapshot.read" -> "你的状态";
            case "game.target_block.read", "carpet.block_info.read" -> "眼前这个方块";
            case "server.rules.read" -> "服务器这边的规则";
            case "carpet.distance.measure" -> "距离结果";
            case "carpet.mobcaps.read" -> "生物生成情况";
            default -> "我替你看到的结果";
        };
    }

    private String formatElapsed(long elapsedNanos) {
        long elapsedMs = Math.max(1L, elapsedNanos / 1_000_000L);
        if (elapsedMs < 1_000L) {
            return elapsedMs + " ms";
        }

        long wholeSeconds = elapsedMs / 1_000L;
        long tenths = (elapsedMs % 1_000L) / 100L;
        return wholeSeconds + "." + tenths + " s";
    }

    private ActionTracePresentation actionStartedPresentation(BridgeModels.ActionRequestPayload actionRequest, int actionIndex) {
        List<SecondaryChip> chips = new ArrayList<>();
        chips.add(new SecondaryChip("第 " + actionIndex + " 步", ChipTone.MUTED));
        chips.add(new SecondaryChip(riskLabel(actionRequest.risk_class), ChipTone.MUTED));
        if (actionRequest.requires_confirmation) {
            chips.add(new SecondaryChip("待确认", ChipTone.WARNING));
        }

        return new ActionTracePresentation(
                "处理中",
                ChipTone.INFO,
                capabilityLabel(actionRequest.capability_id),
                actionIntentDetail(actionRequest),
                chips
        );
    }

    private ActionTracePresentation actionFinishedPresentation(
            BridgeModels.ActionRequestPayload actionRequest,
            BridgeModels.ActionResultPayload actionResult,
            int actionIndex
    ) {
        List<SecondaryChip> chips = new ArrayList<>();
        chips.add(new SecondaryChip("第 " + actionIndex + " 步", ChipTone.MUTED));
        if (actionResult.timing_ms > 0) {
            chips.add(new SecondaryChip(actionResult.timing_ms + " ms", ChipTone.MUTED));
        }
        if (!actionResult.preconditions_passed) {
            chips.add(new SecondaryChip("状态已变化", ChipTone.WARNING));
        }

        return new ActionTracePresentation(
                statusLabel(actionResult.status),
                statusTone(actionResult.status),
                capabilityLabel(actionRequest.capability_id),
                actionResultDetail(actionRequest, actionResult),
                chips
        );
    }

    private String capabilityLabel(String capabilityId) {
        return switch (capabilityId) {
            case "game.player_snapshot.read" -> "看看你的状态";
            case "game.target_block.read" -> "看看你正在看的方块";
            case "server.rules.read" -> "看看服务器规则";
            case "carpet.block_info.read" -> "看看这个方块的详细信息";
            case "carpet.distance.measure" -> "测量距离";
            case "carpet.mobcaps.read" -> "看看生物生成情况";
            default -> capabilityId;
        };
    }

    private String actionIntentDetail(BridgeModels.ActionRequestPayload actionRequest) {
        return switch (actionRequest.capability_id) {
            case "game.player_snapshot.read" -> "我会替你看看生命、饥饿、坐标和手里的东西。";
            case "game.target_block.read" -> "我会替你看看你现在盯着的方块。";
            case "server.rules.read" -> "我会替你看看服务器这边现在的规则。";
            case "carpet.block_info.read" -> "我会替你把这个方块的细节看清楚。";
            case "carpet.distance.measure" -> "我会替你量一下这里到目标位置的距离。";
            case "carpet.mobcaps.read" -> "我会替你看看这个维度现在的生物生成情况。";
            default -> "我先替你把这一步看清楚。";
        };
    }

    private String actionResultDetail(
            BridgeModels.ActionRequestPayload actionRequest,
            BridgeModels.ActionResultPayload actionResult
    ) {
        return switch (actionResult.status) {
            case "executed" -> nonBlank(actionResult.side_effect_summary, executedResultFallback());
            case "action_budget_exhausted" -> "这回合我先看到这里，不能再往下做了。";
            case "role_forbidden" -> "这一步我现在还不能替你做。";
            case "confirmation_required" -> "这一步要先等你确认，我就先停在这里。";
            case "capability_hidden" -> "现在这个情况里，我还不能用这一步。";
            case "precondition_failed" -> "刚刚情况变了，我先不乱动这一步。";
            case "player_unavailable" -> "你已经不在这里了，这一步就先停下。";
            case "execution_failed" -> nonBlank(actionResult.error_message, "刚刚这一步没有做好，我再换个办法会更稳。");
            default -> nonBlank(actionResult.error_message, nonBlank(actionResult.side_effect_summary, actionIntentDetail(actionRequest)));
        };
    }

    private String executedResultFallback() {
        String[] variants = new String[]{
                "已经替你处理好了。",
                "弄好了，这样就可以 (￣▽￣)",
                "这边已经理顺了 (｡•̀ᴗ-)",
                "处理好了，没让你白等吧。",
                "已经看明白了，你可以放心了 >_<",
        };
        return variants[ThreadLocalRandom.current().nextInt(variants.length)];
    }

    private String statusLabel(String status) {
        return switch (status) {
            case "executed" -> "已完成";
            case "action_budget_exhausted" -> "已停止";
            case "role_forbidden" -> "已拒绝";
            case "confirmation_required" -> "待确认";
            case "capability_hidden" -> "不可用";
            case "precondition_failed" -> "已取消";
            case "player_unavailable" -> "已取消";
            case "execution_failed" -> "失败";
            default -> "已更新";
        };
    }

    private ChipTone statusTone(String status) {
        return switch (status) {
            case "executed" -> ChipTone.SUCCESS;
            case "confirmation_required" -> ChipTone.WARNING;
            case "action_budget_exhausted", "role_forbidden", "capability_hidden", "precondition_failed", "player_unavailable", "execution_failed" -> ChipTone.ERROR;
            default -> ChipTone.INFO;
        };
    }

    private String riskLabel(String riskClass) {
        return switch (riskClass) {
            case "read_only" -> "只读";
            case "world_low_risk" -> "低风险";
            case "admin_mutation" -> "管理员";
            case "experimental_privileged" -> "实验能力";
            default -> "对话";
        };
    }

    private String nonBlank(String primary, String fallback) {
        return primary != null && !primary.isBlank() ? primary : fallback;
    }

    private void deliverReply(net.minecraft.server.MinecraftServer server, UUID playerId, ReplyPresentation presentation) {
        server.execute(() -> {
            ServerPlayerEntity player = server.getPlayerManager().getPlayer(playerId);
            if (player == null) {
                MinaMod.LOGGER.info("Skipping Mina reply delivery because player {} is offline", playerId);
                return;
            }

            MinaChatRenderer.sendReply(player, presentation);
        });
    }

    private void deliverActionTrace(net.minecraft.server.MinecraftServer server, UUID playerId, ActionTracePresentation presentation) {
        ServerExecutor.call(server, () -> {
            ServerPlayerEntity player = server.getPlayerManager().getPlayer(playerId);
            if (player == null) {
                MinaMod.LOGGER.info("Skipping Mina action trace delivery because player {} is offline", playerId);
                return null;
            }

            MinaChatRenderer.sendActionTrace(player, presentation);
            return null;
        }).join();
    }

    private void deliverResponseTraceEvents(
            net.minecraft.server.MinecraftServer server,
            UUID playerId,
            BridgeModels.TurnResponse response
    ) {
        if (response.trace_events == null || response.trace_events.isEmpty()) {
            return;
        }

        for (BridgeModels.TraceEventPayload traceEvent : response.trace_events) {
            deliverActionTrace(server, playerId, fromTraceEvent(traceEvent));
        }
    }

    private ActionTracePresentation fromTraceEvent(BridgeModels.TraceEventPayload traceEvent) {
        List<SecondaryChip> chips = new ArrayList<>();
        if (traceEvent.secondary != null) {
            for (BridgeModels.TraceChipPayload chip : traceEvent.secondary) {
                if (chip == null || chip.label == null || chip.label.isBlank()) {
                    continue;
                }
                chips.add(new SecondaryChip(chip.label, chipTone(chip.tone)));
            }
        }

        return new ActionTracePresentation(
                nonBlank(traceEvent.status_label, "已更新"),
                chipTone(traceEvent.status_tone),
                nonBlank(traceEvent.title, "Mina 步骤"),
                traceEvent.detail,
                chips
        );
    }

    private ChipTone chipTone(String raw) {
        if (raw == null) {
            return ChipTone.MUTED;
        }

        return switch (raw.trim().toLowerCase()) {
            case "success" -> ChipTone.SUCCESS;
            case "info" -> ChipTone.INFO;
            case "warning" -> ChipTone.WARNING;
            case "error" -> ChipTone.ERROR;
            default -> ChipTone.MUTED;
        };
    }

    private void deliverError(net.minecraft.server.MinecraftServer server, UUID playerId, String message) {
        server.execute(() -> {
            ServerPlayerEntity player = server.getPlayerManager().getPlayer(playerId);
            if (player == null) {
                MinaMod.LOGGER.info("Skipping Mina error delivery because player {} is offline", playerId);
                return;
            }

            MinaChatRenderer.sendErrorReply(player, message);
        });
    }
}
