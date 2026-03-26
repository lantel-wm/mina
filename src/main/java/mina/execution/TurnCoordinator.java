package mina.execution;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import mina.MinaMod;
import mina.bridge.AppServerClient;
import mina.bridge.AppServerModels;
import mina.capability.CapabilityExecutorRegistry;
import mina.chat.MinaChatRenderer;
import mina.chat.MinaChatRenderer.ActionTracePresentation;
import mina.chat.MinaChatRenderer.ChipTone;
import mina.chat.MinaChatRenderer.ReplyPresentation;
import mina.chat.MinaChatRenderer.SecondaryChip;
import mina.config.MinaConfig;
import mina.context.GameContextCollector;
import mina.context.RecentEventTracker;
import mina.context.TurnContext;
import mina.policy.ExecutionGuard;
import mina.policy.ExecutionGuard.Decision;
import mina.util.ServerExecutor;
import com.mojang.brigadier.exceptions.CommandSyntaxException;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeoutException;

public final class TurnCoordinator {
    private static final Set<String> APPROVAL_YES = Set.of(
            "yes", "y", "ok", "okay", "sure", "confirm", "go", "go ahead",
            "继续", "可以", "好", "好的", "确认", "行", "同意", "开始吧"
    );
    private static final Set<String> APPROVAL_NO = Set.of(
            "no", "n", "stop", "cancel", "don't", "不要", "不用", "取消", "算了", "先别", "停", "拒绝"
    );

    private final MinaConfig config;
    private final AppServerClient appServerClient;
    private final GameContextCollector contextCollector;
    private final CapabilityExecutorRegistry capabilityRegistry;
    private final ExecutionGuard executionGuard;
    private final PendingTurnRegistry pendingTurnRegistry;
    private final PendingApprovalRegistry pendingApprovalRegistry;
    private final RecentEventTracker recentEventTracker;
    private final DevTurnLog devTurnLog;
    private final ExecutorService ioExecutor;
    private final Gson gson;

    public TurnCoordinator(
            MinaConfig config,
            AppServerClient appServerClient,
            GameContextCollector contextCollector,
            CapabilityExecutorRegistry capabilityRegistry,
            ExecutionGuard executionGuard,
            PendingTurnRegistry pendingTurnRegistry,
            PendingApprovalRegistry pendingApprovalRegistry,
            RecentEventTracker recentEventTracker,
            DevTurnLog devTurnLog,
            ExecutorService ioExecutor
    ) {
        this.config = config;
        this.appServerClient = appServerClient;
        this.contextCollector = contextCollector;
        this.capabilityRegistry = capabilityRegistry;
        this.executionGuard = executionGuard;
        this.pendingTurnRegistry = pendingTurnRegistry;
        this.pendingApprovalRegistry = pendingApprovalRegistry;
        this.recentEventTracker = recentEventTracker;
        this.devTurnLog = devTurnLog;
        this.ioExecutor = ioExecutor;
        this.gson = new GsonBuilder().serializeNulls().create();
    }

    public boolean submitTurn(ServerCommandSource source, String userMessage, Runnable acceptedCallback) throws CommandSyntaxException {
        ServerPlayerEntity player = source.getPlayerOrThrow();
        UUID playerId = player.getUuid();
        PendingApprovalRegistry.PendingApproval pendingApproval = pendingApprovalRegistry.get(playerId);
        if (pendingApproval != null) {
            return handleApprovalReply(source, player, userMessage, pendingApproval, acceptedCallback);
        }

        String turnId = UUID.randomUUID().toString();
        String sessionRef = player.getUuidAsString();
        String playerName = player.getName().getString();
        Instant startedAt = Instant.now();

        if (!pendingTurnRegistry.tryOpen(playerId, turnId)) {
            source.sendError(MinaChatRenderer.commandError("我这边还在看上一件事，先别急。"));
            return false;
        }

        recentEventTracker.recordPlayerEvent("mina_user_message", player, Map.of("message", userMessage));
        acceptedCallback.run();
        devTurnLog.recordAccepted(turnId, sessionRef, playerName, userMessage, startedAt);
        ioExecutor.submit(() -> runTurn(playerId, sessionRef, playerName, turnId, userMessage, source.getServer(), startedAt));
        return true;
    }

    private boolean handleApprovalReply(
            ServerCommandSource source,
            ServerPlayerEntity player,
            String userMessage,
            PendingApprovalRegistry.PendingApproval pendingApproval,
            Runnable acceptedCallback
    ) {
        String normalized = userMessage == null ? "" : userMessage.trim().toLowerCase();
        if (!APPROVAL_YES.contains(normalized) && !APPROVAL_NO.contains(normalized)) {
            source.sendError(MinaChatRenderer.commandError("这一步需要你明确回复“确认/取消”之类的话。"));
            return false;
        }

        acceptedCallback.run();
        boolean approved = APPROVAL_YES.contains(normalized);
        pendingApproval.decisionFuture().complete(new PendingApprovalRegistry.ApprovalDecision(approved, userMessage.trim()));
        pendingApprovalRegistry.clear(player.getUuid());

        MinaChatRenderer.sendActionTrace(
                player,
                new ActionTracePresentation(
                        approved ? "已确认" : "已取消",
                        approved ? ChipTone.SUCCESS : ChipTone.WARNING,
                        approved ? "继续执行" : "取消执行",
                        pendingApproval.effectSummary(),
                        List.of(new SecondaryChip("确认已处理", approved ? ChipTone.SUCCESS : ChipTone.WARNING))
                )
        );
        return true;
    }

    private void runTurn(
            UUID playerId,
            String sessionRef,
            String playerName,
            String turnId,
            String userMessage,
            net.minecraft.server.MinecraftServer server,
            Instant startedAt
    ) {
        long turnStartedAtNanos = System.nanoTime();
        AppServerClient.TurnStream stream = null;
        try {
            TurnContext turnContext = ServerExecutor.call(server, () -> collectTurnContext(playerId, server)).join();
            sessionRef = turnContext.sessionRef();
            playerName = turnContext.playerPayload().name;

            appServerClient.ensureThread(sessionRef, turnContext.playerPayload().uuid, turnContext.playerPayload().name);
            AppServerModels.TurnStartParams startParams = toTurnStartParams(turnContext, turnId, userMessage);
            stream = appServerClient.startTurn(startParams);

            int actionCount = 0;
            boolean approvalSeen = false;
            StringBuilder assistantBuffer = new StringBuilder();

            while (true) {
                AppServerClient.AppServerEvent event = stream.take(config.requestTimeout());
                switch (event.method()) {
                    case "item/assistantMessage/delta" -> {
                        AppServerModels.ItemDeltaPayload delta = gson.fromJson(event.params(), AppServerModels.ItemDeltaPayload.class);
                        if (delta.delta != null) {
                            assistantBuffer.append(delta.delta);
                        }
                    }
                    case "item/toolCall/requested" -> {
                        AppServerModels.ToolCallRequestPayload toolCall = gson.fromJson(event.params(), AppServerModels.ToolCallRequestPayload.class);
                        actionCount++;
                        int currentActionCount = actionCount;
                        deliverActionTrace(server, playerId, actionStartedPresentation(toolCall, currentActionCount));
                        AppServerModels.ToolResultParams actionResult = ServerExecutor.call(
                                server,
                                () -> executeAction(server, playerId, toolCall, currentActionCount)
                        ).join();
                        deliverActionTrace(server, playerId, actionFinishedPresentation(toolCall, actionResult, currentActionCount));
                        stream.sendToolResult(actionResult);
                    }
                    case "approval/requested" -> {
                        approvalSeen = true;
                        AppServerModels.ApprovalRequestPayload approvalRequest = gson.fromJson(event.params(), AppServerModels.ApprovalRequestPayload.class);
                        PendingApprovalRegistry.PendingApproval pendingApproval = pendingApprovalRegistry.put(
                                playerId,
                                approvalRequest.thread_id,
                                approvalRequest.turn_id,
                                approvalRequest.approval_id,
                                approvalRequest.effect_summary
                        );
                        deliverActionTrace(server, playerId, approvalRequestedPresentation(approvalRequest));
                        PendingApprovalRegistry.ApprovalDecision decision = pendingApproval.decisionFuture().get();
                        AppServerModels.ApprovalResponseParams response = new AppServerModels.ApprovalResponseParams();
                        response.thread_id = approvalRequest.thread_id;
                        response.turn_id = approvalRequest.turn_id;
                        response.approval_id = approvalRequest.approval_id;
                        response.approved = decision.approved();
                        response.reason = decision.reason();
                        stream.sendApprovalResponse(response);
                    }
                    case "warning" -> {
                        AppServerModels.WarningPayload warning = gson.fromJson(event.params(), AppServerModels.WarningPayload.class);
                        deliverActionTrace(server, playerId, warningPresentation(warning));
                    }
                    case "turn/completed" -> {
                        JsonObject turn = event.params().getAsJsonObject("turn");
                        String finalReply = turn != null && turn.has("final_reply")
                                ? turn.get("final_reply").getAsString()
                                : assistantBuffer.toString();
                        devTurnLog.recordCompleted(
                                turnId,
                                sessionRef,
                                playerName,
                                userMessage,
                                startedAt,
                                Instant.now(),
                                finalReply
                        );
                        deliverReply(
                                server,
                                playerId,
                                buildReplyPresentation(finalReply, turnStartedAtNanos, actionCount, approvalSeen)
                        );
                        return;
                    }
                    case "turn/failed" -> {
                        String detail = event.params().has("detail") && !event.params().get("detail").isJsonNull()
                                ? event.params().get("detail").getAsString()
                                : event.params().has("message") ? event.params().get("message").getAsString() : "Turn failed.";
                        devTurnLog.recordFailed(
                                turnId,
                                sessionRef,
                                playerName,
                                userMessage,
                                startedAt,
                                Instant.now(),
                                detail,
                                null
                        );
                        deliverError(server, playerId, "刚刚这一步有点不对，我再处理也许会更稳。原因：" + detail);
                        return;
                    }
                    default -> {
                    }
                }
            }
        } catch (TimeoutException exception) {
            MinaMod.LOGGER.error("Mina turn {} timed out waiting for app-server events", turnId, exception);
            devTurnLog.recordFailed(
                    turnId,
                    sessionRef,
                    playerName,
                    userMessage,
                    startedAt,
                    Instant.now(),
                    exception.getMessage(),
                    null
            );
            deliverError(server, playerId, "Mina 等待处理结果超时了，我这一轮先停下。");
        } catch (Exception exception) {
            MinaMod.LOGGER.error("Mina turn {} failed", turnId, exception);
            devTurnLog.recordFailed(
                    turnId,
                    sessionRef,
                    playerName,
                    userMessage,
                    startedAt,
                    Instant.now(),
                    exception.getMessage(),
                    null
            );
            deliverError(server, playerId, "刚刚这一步有点不对，我再处理也许会更稳。原因：" + exception.getMessage());
        } finally {
            if (stream != null) {
                stream.close();
            }
            pendingApprovalRegistry.clear(playerId);
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

    private AppServerModels.TurnStartParams toTurnStartParams(TurnContext context, String turnId, String userMessage) {
        AppServerModels.TurnStartParams params = new AppServerModels.TurnStartParams();
        params.thread_id = context.sessionRef();
        params.turn_id = turnId;
        params.user_message = userMessage;

        AppServerModels.TurnContextPayload payload = new AppServerModels.TurnContextPayload();
        payload.player = context.playerPayload();
        payload.server_env = context.serverEnvPayload();
        payload.scoped_snapshot = context.scopedSnapshot();
        payload.tool_specs = context.visibleCapabilities().stream()
                .map(AppServerModels.ToolSpecPayload::fromDefinition)
                .toList();

        AppServerModels.LimitsPayload limits = new AppServerModels.LimitsPayload();
        limits.max_agent_steps = config.maxAgentSteps();
        limits.max_bridge_actions_per_turn = config.maxBridgeActionsPerTurn();
        limits.max_continuation_depth = config.maxContinuationDepth();
        payload.limits = limits;

        params.context = payload;
        return params;
    }

    private AppServerModels.ToolResultParams executeAction(
            net.minecraft.server.MinecraftServer server,
            UUID playerId,
            AppServerModels.ToolCallRequestPayload actionRequest,
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
            var result = capabilityRegistry.execute(actionRequest.tool_id, player, actionRequest.arguments);
            Map<String, Object> actionEvent = new java.util.LinkedHashMap<>();
            actionEvent.put("tool_id", actionRequest.tool_id);
            actionEvent.put("summary", result.sideEffectSummary());
            recentEventTracker.recordPlayerEvent("mina_action_executed", player, actionEvent);

            AppServerModels.ToolResultParams payload = new AppServerModels.ToolResultParams();
            payload.thread_id = actionRequest.thread_id;
            payload.turn_id = actionRequest.turn_id;
            payload.item_id = actionRequest.item_id;
            payload.tool_id = actionRequest.tool_id;
            payload.status = "executed";
            payload.observations = result.observations();
            payload.preconditions_passed = true;
            payload.side_effect_summary = result.sideEffectSummary();
            payload.timing_ms = (int) ((System.nanoTime() - startedAt) / 1_000_000L);
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

    private AppServerModels.ToolResultParams rejectedResult(
            AppServerModels.ToolCallRequestPayload actionRequest,
            String status,
            boolean preconditionsPassed,
            String message
    ) {
        return rejectedResult(actionRequest, status, preconditionsPassed, message, null);
    }

    private AppServerModels.ToolResultParams rejectedResult(
            AppServerModels.ToolCallRequestPayload actionRequest,
            String status,
            boolean preconditionsPassed,
            String message,
            String stateFingerprint
    ) {
        AppServerModels.ToolResultParams payload = new AppServerModels.ToolResultParams();
        payload.thread_id = actionRequest.thread_id;
        payload.turn_id = actionRequest.turn_id;
        payload.item_id = actionRequest.item_id;
        payload.tool_id = actionRequest.tool_id;
        payload.status = status;
        payload.observations = Map.of("message", message);
        payload.preconditions_passed = preconditionsPassed;
        payload.side_effect_summary = message;
        payload.timing_ms = 0;
        payload.state_fingerprint = stateFingerprint;
        payload.error_message = message;
        return payload;
    }

    private ReplyPresentation buildReplyPresentation(
            String finalReply,
            long turnStartedAt,
            int actionCount,
            boolean approvalSeen
    ) {
        List<SecondaryChip> chips = new ArrayList<>();
        if (approvalSeen) {
            chips.add(new SecondaryChip("已确认", ChipTone.SUCCESS));
        }
        if (actionCount > 0) {
            chips.add(new SecondaryChip("已完成", ChipTone.SUCCESS));
            chips.add(new SecondaryChip(actionCount + " 次执行", ChipTone.INFO));
        } else {
            chips.add(new SecondaryChip("已回复", ChipTone.INFO));
            chips.add(new SecondaryChip("纯对话", ChipTone.MUTED));
        }
        chips.add(new SecondaryChip(formatElapsed(System.nanoTime() - turnStartedAt), ChipTone.MUTED));
        String title = actionCount > 0 ? "我替你看到的结果" : "";
        return new ReplyPresentation(title, finalReply, chips, null, ChipTone.MUTED);
    }

    private ActionTracePresentation actionStartedPresentation(AppServerModels.ToolCallRequestPayload actionRequest, int actionIndex) {
        List<SecondaryChip> chips = new ArrayList<>();
        chips.add(new SecondaryChip("第 " + actionIndex + " 步", ChipTone.MUTED));
        chips.add(new SecondaryChip(riskLabel(actionRequest.risk_class), ChipTone.MUTED));
        if (actionRequest.requires_confirmation) {
            chips.add(new SecondaryChip("待确认", ChipTone.WARNING));
        }

        return new ActionTracePresentation(
                "处理中",
                ChipTone.INFO,
                capabilityLabel(actionRequest.tool_id),
                nonBlank(actionRequest.effect_summary, "我先替你把这一步看清楚。"),
                chips
        );
    }

    private ActionTracePresentation actionFinishedPresentation(
            AppServerModels.ToolCallRequestPayload actionRequest,
            AppServerModels.ToolResultParams actionResult,
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
                capabilityLabel(actionRequest.tool_id),
                actionResultDetail(actionRequest, actionResult),
                chips
        );
    }

    private ActionTracePresentation approvalRequestedPresentation(AppServerModels.ApprovalRequestPayload approvalRequest) {
        return new ActionTracePresentation(
                "待确认",
                ChipTone.WARNING,
                capabilityLabel(approvalRequest.tool_call.tool_id),
                approvalRequest.effect_summary,
                List.of(
                        new SecondaryChip("回复 确认/取消", ChipTone.WARNING),
                        new SecondaryChip(riskLabel(approvalRequest.risk_class), ChipTone.MUTED)
                )
        );
    }

    private ActionTracePresentation warningPresentation(AppServerModels.WarningPayload warning) {
        return new ActionTracePresentation(
                "已提醒",
                ChipTone.WARNING,
                nonBlank(warning.message, "Mina 提醒"),
                warning.detail,
                List.of()
        );
    }

    private String capabilityLabel(String capabilityId) {
        return switch (capabilityId) {
            case "world.player_state.read" -> "看看你的状态";
            case "world.threats.read" -> "看看附近的威胁";
            case "world.poi.read" -> "找找附近的目标";
            case "game.player_snapshot.read" -> "看看你的状态";
            case "game.nearby_entities.read" -> "看看附近有哪些生物";
            case "game.target_block.read", "carpet.block_info.read" -> "看看你正在看的方块";
            case "server.rules.read", "carpet.rules.read" -> "看看服务器规则";
            case "carpet.distance.measure" -> "测量距离";
            case "carpet.mobcaps.read" -> "看看生物生成情况";
            default -> capabilityId;
        };
    }

    private String actionResultDetail(
            AppServerModels.ToolCallRequestPayload actionRequest,
            AppServerModels.ToolResultParams actionResult
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
            default -> nonBlank(actionResult.error_message, nonBlank(actionResult.side_effect_summary, actionRequest.effect_summary));
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

    private String formatElapsed(long elapsedNanos) {
        long elapsedMs = Math.max(1L, elapsedNanos / 1_000_000L);
        if (elapsedMs < 1_000L) {
            return elapsedMs + " ms";
        }
        long wholeSeconds = elapsedMs / 1_000L;
        long tenths = (elapsedMs % 1_000L) / 100L;
        return wholeSeconds + "." + tenths + " s";
    }

    private void deliverReply(net.minecraft.server.MinecraftServer server, UUID playerId, ReplyPresentation presentation) {
        server.execute(() -> {
            ServerPlayerEntity player = server.getPlayerManager().getPlayer(playerId);
            if (player == null) {
                MinaMod.LOGGER.info("Skipping Mina reply delivery because player {} is offline", playerId);
                return;
            }

            Map<String, Object> replyEvent = new java.util.LinkedHashMap<>();
            replyEvent.put("title", presentation.title());
            replyEvent.put("body", presentation.body());
            recentEventTracker.recordPlayerEvent("mina_reply_sent", player, replyEvent);
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
