package mina.companion;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import mina.bridge.AppServerClient;
import mina.bridge.AppServerModels;
import mina.config.MinaConfig;
import mina.context.GameContextCollector;
import mina.context.RecentEventTracker;
import mina.context.TurnContext;
import mina.execution.PendingTurnRegistry;
import mina.execution.TurnCoordinator;
import mina.util.ServerExecutor;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.damage.DamageSource;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;

import java.io.IOException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ThreadLocalRandom;

public final class CompanionCoordinator {
    private final MinaConfig config;
    private final MinecraftServer server;
    private final AppServerClient appServerClient;
    private final GameContextCollector contextCollector;
    private final TurnCoordinator turnCoordinator;
    private final PendingTurnRegistry pendingTurnRegistry;
    private final RecentEventTracker recentEventTracker;
    private final ExecutorService ioExecutor;
    private final PlayerActivityTracker activityTracker = new PlayerActivityTracker();
    private final Map<UUID, PlayerCompanionState> playerStates = new ConcurrentHashMap<>();
    private long currentTick = 0L;

    public CompanionCoordinator(
            MinaConfig config,
            MinecraftServer server,
            AppServerClient appServerClient,
            GameContextCollector contextCollector,
            TurnCoordinator turnCoordinator,
            PendingTurnRegistry pendingTurnRegistry,
            RecentEventTracker recentEventTracker,
            ExecutorService ioExecutor
    ) {
        this.config = config;
        this.server = server;
        this.appServerClient = appServerClient;
        this.contextCollector = contextCollector;
        this.turnCoordinator = turnCoordinator;
        this.pendingTurnRegistry = pendingTurnRegistry;
        this.recentEventTracker = recentEventTracker;
        this.ioExecutor = ioExecutor;
    }

    public synchronized void onPlayerJoin(ServerPlayerEntity player) {
        PlayerCompanionState state = state(player.getUuid());
        state.online = true;
        state.playerName = player.getName().getString();
        state.playerUuid = player.getUuidAsString();
        state.sessionJoinTick = currentTick;
        state.playerMessagedSinceJoin = false;
        state.evaluationInFlight = false;
        enqueueSignal(
                state,
                new CompanionSignal(
                        null,
                        "player_join_greeting",
                        "low",
                        Instant.now(),
                        currentTick,
                        currentTick + ThreadLocalRandom.current().nextLong(40L, 61L),
                        currentTick + 2_400L,
                        "join_greeting",
                        Map.of("session_join_tick", currentTick)
                )
        );
        loadPersistedCompanionStateAsync(player.getUuidAsString(), player.getName().getString(), player.getUuid());
    }

    public synchronized void onPlayerUserMessage(ServerPlayerEntity player, String message) {
        PlayerCompanionState state = state(player.getUuid());
        state.playerMessagedSinceJoin = true;
        state.pendingSignals.remove("join_greeting");
        state.lastUserMessageAt = Instant.now();
        state.lastUserMessage = message;
    }

    public synchronized void onPlayerLeave(ServerPlayerEntity player) {
        state(player.getUuid()).online = false;
    }

    public synchronized void onPlayerRespawn(ServerPlayerEntity oldPlayer, ServerPlayerEntity newPlayer, boolean alive) {
        PlayerCompanionState state = state(newPlayer.getUuid());
        state.online = true;
        state.playerName = newPlayer.getName().getString();
        state.playerUuid = newPlayer.getUuidAsString();
        if (state.pendingDeathFollowup != null) {
            Map<String, Object> payload = new LinkedHashMap<>(state.pendingDeathFollowup);
            payload.put("alive", alive);
            payload.put("respawn_dimension", newPlayer.getEntityWorld().getRegistryKey().getValue().toString());
            enqueueSignal(
                    state,
                    new CompanionSignal(
                            null,
                            "death_followup",
                            "high",
                            Instant.now(),
                            currentTick,
                            currentTick + 20L,
                            currentTick + 12_000L,
                            "death_followup",
                            payload
                    )
            );
            syncCompanionMetadataAsync(newPlayer.getUuidAsString(), state);
        }
    }

    public synchronized void onPlayerAfterDamage(
            ServerPlayerEntity player,
            DamageSource source,
            float baseDamageTaken,
            float damageTaken,
            boolean blocked
    ) {
        if (damageTaken < 6.0F || blocked) {
            return;
        }
        PlayerCompanionState state = state(player.getUuid());
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("damage_source", source.getName());
        payload.put("damage_taken", damageTaken);
        payload.put("health_after", player.getHealth());
        payload.put("blocked", blocked);
        enqueueSignal(
                state,
                new CompanionSignal(
                        null,
                        "danger_warning",
                        "high",
                        Instant.now(),
                        currentTick,
                        currentTick,
                        currentTick + 600L,
                        "danger_warning",
                        payload
                )
        );
    }

    public synchronized void onPlayerDeath(ServerPlayerEntity player, DamageSource source) {
        PlayerCompanionState state = state(player.getUuid());
        Map<String, Object> damageState = recentEventTracker.recentDamageState(player);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("damage_source", source.getName());
        payload.put("death_dimension", player.getEntityWorld().getRegistryKey().getValue().toString());
        payload.put("recently_hurt", damageState.get("recently_hurt"));
        payload.put("long_in_danger", damageState.get("long_in_danger"));
        payload.put("is_alone", damageState.get("long_alone"));
        payload.put("occurred_at", Instant.now().toString());
        state.pendingDeathFollowup = payload;
        syncCompanionMetadataAsync(player.getUuidAsString(), state);
    }

    public synchronized void onPlayerChangeWorld(ServerPlayerEntity player, ServerWorld origin, ServerWorld destination) {
        String to = destination.getRegistryKey().getValue().toString();
        if ("minecraft:the_nether".equals(to)) {
            maybeEnqueueMilestone(state(player.getUuid()), "entered_nether", player, Map.of("to_dimension", to));
        } else if ("minecraft:the_end".equals(to)) {
            maybeEnqueueMilestone(state(player.getUuid()), "entered_end", player, Map.of("to_dimension", to));
        }
    }

    public synchronized void onPlayerKilledEntity(ServerPlayerEntity player, LivingEntity killedEntity, DamageSource source) {
        String entityId = Registries.ENTITY_TYPE.getId(killedEntity.getType()).toString();
        if (isBossLike(entityId)) {
            maybeEnqueueMilestone(
                    state(player.getUuid()),
                    "boss_victory",
                    player,
                    Map.of(
                            "entity_id", entityId,
                            "entity_name", killedEntity.getName().getString(),
                            "damage_source", source.getName()
                    )
            );
        }
    }

    public synchronized void onPlayerEquipmentChange(
            ServerPlayerEntity player,
            EquipmentSlot slot,
            ItemStack previous,
            ItemStack current
    ) {
        String currentId = Registries.ITEM.getId(current.getItem()).toString();
        String milestone = switch (tierOf(currentId)) {
            case "iron" -> "gear_upgrade_iron";
            case "diamond" -> "gear_upgrade_diamond";
            case "netherite" -> "gear_upgrade_netherite";
            default -> null;
        };
        if (milestone == null) {
            return;
        }
        maybeEnqueueMilestone(
                state(player.getUuid()),
                milestone,
                player,
                Map.of(
                        "slot", slot.asString(),
                        "item_id", currentId
                )
        );
    }

    public synchronized void onServerTick(MinecraftServer minecraftServer) {
        currentTick++;
        if (currentTick % 20L != 0L) {
            return;
        }
        for (ServerPlayerEntity player : minecraftServer.getPlayerManager().getPlayerList()) {
            PlayerCompanionState state = state(player.getUuid());
            state.online = true;
            state.playerName = player.getName().getString();
            state.playerUuid = player.getUuidAsString();
            TurnContext context = contextCollector.collect(player);
            enqueueDangerIfNeeded(state, context);
            sampleAdvancements(state, player);
            sampleBaseMilestone(state, player, context);
            sampleRepetitionComfort(state, player, context);
            maybeScheduleEvaluation(state, player, context);
        }
    }

    private void enqueueDangerIfNeeded(PlayerCompanionState state, TurnContext context) {
        Object rawRisk = context.scopedSnapshot().get("risk_state");
        if (!(rawRisk instanceof Map<?, ?> risk)) {
            return;
        }
        Object rawLevel = risk.containsKey("level") ? risk.get("level") : "";
        String level = String.valueOf(rawLevel);
        boolean immediate = Boolean.TRUE.equals(risk.get("immediate_action_needed"));
        if (!(immediate && ("high".equals(level) || "critical".equals(level)))) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("level", level);
        payload.put("risk_state", risk);
        enqueueSignal(
                state,
                new CompanionSignal(
                        null,
                        "danger_warning",
                        "high",
                        Instant.now(),
                        currentTick,
                        currentTick,
                        currentTick + 600L,
                        "danger_warning",
                        payload
                )
        );
    }

    private void sampleAdvancements(PlayerCompanionState state, ServerPlayerEntity player) {
        try {
            Collection<?> advancements = server.getAdvancementLoader().getAdvancements();
            Set<String> currentCompleted = new LinkedHashSet<>();
            List<Map<String, Object>> newlyCompleted = new ArrayList<>();
            var tracker = player.getAdvancementTracker();
            for (Object rawEntry : advancements) {
                if (!(rawEntry instanceof net.minecraft.advancement.AdvancementEntry entry)) {
                    continue;
                }
                String advancementId = entry.id().toString();
                if (advancementId.contains("recipes/")) {
                    continue;
                }
                var progress = tracker.getProgress(entry);
                if (progress == null || !progress.isDone()) {
                    continue;
                }
                currentCompleted.add(advancementId);
                if (state.knownAdvancementIds.contains(advancementId) || state.celebratedAdvancementIds.contains(advancementId)) {
                    continue;
                }
                newlyCompleted.add(
                        Map.of(
                                "advancement_id", advancementId,
                                "display_name", advancementId
                        )
                );
            }
            state.knownAdvancementIds.clear();
            state.knownAdvancementIds.addAll(currentCompleted);
            if (!newlyCompleted.isEmpty()) {
                Map<String, Object> payload = new LinkedHashMap<>();
                payload.put("advancements", newlyCompleted.subList(0, Math.min(newlyCompleted.size(), 3)));
                enqueueSignal(
                        state,
                        new CompanionSignal(
                                null,
                                "advancement_celebration",
                                "medium",
                                Instant.now(),
                                currentTick,
                                currentTick,
                                currentTick + 6_000L,
                                "advancement:" + advancementHash(newlyCompleted),
                                payload
                        )
                );
            }
        } catch (Exception ignored) {
            // keep advancement sampling best-effort
        }
    }

    private void sampleBaseMilestone(PlayerCompanionState state, ServerPlayerEntity player, TurnContext context) {
        Object rawInteractables = context.scopedSnapshot().get("interactables");
        if (!(rawInteractables instanceof Map<?, ?> interactables)) {
            state.baseEvidenceTicks = 0L;
            return;
        }
        boolean hasShelter = hasNonEmptyList(interactables.get("shelter_markers"));
        boolean hasContainer = hasNonEmptyList(interactables.get("containers"));
        boolean hasWorkstation = hasNonEmptyList(interactables.get("workstations"));
        String bucket = floorBucket(player.getX(), 16) + ":" + floorBucket(player.getZ(), 16);
        if (hasShelter && hasContainer && hasWorkstation && bucket.equals(state.baseBucket)) {
            state.baseEvidenceTicks += 20L;
        } else if (hasShelter && hasContainer && hasWorkstation) {
            state.baseBucket = bucket;
            state.baseEvidenceTicks = 20L;
        } else {
            state.baseEvidenceTicks = 0L;
            state.baseBucket = bucket;
        }
        if (state.baseEvidenceTicks < 60L || state.celebratedMilestones.contains("base_established")) {
            return;
        }
        maybeEnqueueMilestone(
                state,
                "base_established",
                player,
                Map.of("bucket", bucket, "evidence_ticks", state.baseEvidenceTicks)
        );
    }

    private void sampleRepetitionComfort(PlayerCompanionState state, ServerPlayerEntity player, TurnContext context) {
        String locationKind = stringAt(context.scopedSnapshot(), "world", "location_kind");
        String riskLevel = stringAt(context.scopedSnapshot(), "risk_state", "level");
        PlayerActivityTracker.ActivityAlert alert = activityTracker.sample(
                state.activityState,
                currentTick,
                player,
                config.targetReachBlocks(),
                locationKind,
                riskLevel
        );
        if (alert == null) {
            return;
        }
        enqueueSignal(
                state,
                new CompanionSignal(
                        null,
                        "repetition_comfort",
                        "low",
                        Instant.now(),
                        currentTick,
                        currentTick,
                        currentTick + 12_000L,
                        "repetition:" + alert.family(),
                        alert.payload()
                )
        );
    }

    private void maybeScheduleEvaluation(PlayerCompanionState state, ServerPlayerEntity player, TurnContext context) {
        pruneStaleSignals(state, context);
        if (state.evaluationInFlight || pendingTurnRegistry.hasActiveTurn(player.getUuid())) {
            return;
        }
        if (state.nextEvaluationTick > currentTick) {
            return;
        }
        List<CompanionSignal> candidates = topCandidates(state);
        if (candidates.isEmpty()) {
            return;
        }
        state.evaluationInFlight = true;
        Map<String, Object> companionStateSnapshot = companionStateSnapshot(state);
        ioExecutor.submit(() -> runEvaluation(player.getUuid(), context, candidates, companionStateSnapshot));
    }

    private void runEvaluation(
            UUID playerId,
            TurnContext context,
            List<CompanionSignal> candidates,
            Map<String, Object> companionStateSnapshot
    ) {
        String threadId = context.playerPayload().uuid;
        try {
            appServerClient.ensureThread(threadId, context.playerPayload().uuid, context.playerPayload().name);
            AppServerModels.CompanionEvaluateParams params = new AppServerModels.CompanionEvaluateParams();
            params.thread_id = threadId;
            params.signals = candidates.stream().map(CompanionSignal::toPayload).toList();
            params.context = new AppServerModels.CompanionEvaluateContextPayload();
            params.context.player = context.playerPayload();
            params.context.server_env = context.serverEnvPayload();
            params.context.scoped_snapshot = context.scopedSnapshot();
            params.companion_state = companionStateSnapshot;
            params.delivery_constraints = defaultDeliveryConstraints();
            AppServerModels.CompanionEvaluateResult result = appServerClient.evaluateCompanion(params);
            handleEvaluationResult(playerId, context, candidates, result);
        } catch (IOException | InterruptedException exception) {
            synchronized (this) {
                PlayerCompanionState state = state(playerId);
                state.evaluationInFlight = false;
                state.nextEvaluationTick = currentTick + 100L;
            }
        }
    }

    private void handleEvaluationResult(
            UUID playerId,
            TurnContext context,
            List<CompanionSignal> candidates,
            AppServerModels.CompanionEvaluateResult result
    ) {
        List<CompanionSignal> selected = selectedSignals(candidates, result.selected_signal_ids);
        synchronized (this) {
            PlayerCompanionState state = state(playerId);
            state.evaluationInFlight = false;
            switch (normalize(result.action)) {
                case "start_turn" -> {
                    if (selected.isEmpty() && !candidates.isEmpty()) {
                        selected = List.of(candidates.get(0));
                    }
                    AppServerModels.CompanionTriggerPayload trigger = buildCompanionTrigger(selected);
                    boolean started = turnCoordinator.submitProactiveCompanionTurn(
                            playerId,
                            server,
                            result.synthetic_user_message == null || result.synthetic_user_message.isBlank()
                                    ? defaultSyntheticUserMessage()
                                    : result.synthetic_user_message,
                            trigger
                    );
                    if (started) {
                        removeSignals(state, selected);
                        markDelivered(state, selected);
                        syncCompanionMetadataAsync(context.playerPayload().uuid, state);
                        state.nextEvaluationTick = currentTick + 20L;
                    } else {
                        state.nextEvaluationTick = currentTick + 100L;
                    }
                }
                case "defer" -> state.nextEvaluationTick = currentTick + Math.max(result.defer_seconds == null ? 30 : result.defer_seconds, 1);
                default -> {
                    if (!selected.isEmpty()) {
                        removeSignals(state, selected);
                    } else if (!candidates.isEmpty()) {
                        state.pendingSignals.remove(candidates.get(0).dedupeKey());
                    }
                    state.nextEvaluationTick = currentTick + 20L;
                }
            }
        }
    }

    private void pruneStaleSignals(PlayerCompanionState state, TurnContext context) {
        List<String> dropKeys = new ArrayList<>();
        for (CompanionSignal signal : state.pendingSignals.values()) {
            if (signal.isExpired(currentTick) || !isStillFresh(state, signal, context)) {
                dropKeys.add(signal.dedupeKey());
            }
        }
        for (String key : dropKeys) {
            CompanionSignal removed = state.pendingSignals.remove(key);
            if (removed != null && "death_followup".equals(removed.kind())) {
                state.pendingDeathFollowup = null;
            }
        }
    }

    private boolean isStillFresh(PlayerCompanionState state, CompanionSignal signal, TurnContext context) {
        return switch (signal.kind()) {
            case "player_join_greeting" -> !state.playerMessagedSinceJoin && currentTick - state.sessionJoinTick <= 2_400L;
            case "danger_warning" -> isHighRisk(context);
            case "death_followup" -> state.pendingDeathFollowup != null;
            case "repetition_comfort" -> signal.payload().get("activity_family") != null
                    && signal.payload().get("activity_family").equals(state.activityState.lastActivityFamily());
            default -> true;
        };
    }

    private boolean isHighRisk(TurnContext context) {
        Object rawRisk = context.scopedSnapshot().get("risk_state");
        if (!(rawRisk instanceof Map<?, ?> risk)) {
            return false;
        }
        Object rawLevel = risk.containsKey("level") ? risk.get("level") : "";
        String level = String.valueOf(rawLevel);
        boolean immediate = Boolean.TRUE.equals(risk.get("immediate_action_needed"));
        return immediate && ("high".equals(level) || "critical".equals(level));
    }

    private void maybeEnqueueMilestone(PlayerCompanionState state, String milestone, ServerPlayerEntity player, Map<String, Object> extraPayload) {
        if (state.celebratedMilestones.contains(milestone)) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>(extraPayload);
        payload.put("milestone_key", milestone);
        payload.put("player_uuid", player.getUuidAsString());
        enqueueSignal(
                state,
                new CompanionSignal(
                        null,
                        "milestone_encouragement",
                        "medium",
                        Instant.now(),
                        currentTick,
                        currentTick,
                        currentTick + 6_000L,
                        "milestone:" + milestone,
                        payload
                )
        );
    }

    private void enqueueSignal(PlayerCompanionState state, CompanionSignal signal) {
        Instant lastSent = state.lastSentAtByKind.get(signal.kind());
        if (lastSent != null && !cooldownElapsed(signal.kind(), lastSent, Instant.now())) {
            return;
        }
        CompanionSignal existing = state.pendingSignals.get(signal.dedupeKey());
        if (existing == null || signal.priority() >= existing.priority()) {
            state.pendingSignals.put(signal.dedupeKey(), signal);
        }
    }

    private boolean cooldownElapsed(String kind, Instant lastSentAt, Instant now) {
        long requiredSeconds = switch (kind) {
            case "danger_warning" -> 90L;
            case "repetition_comfort" -> 600L;
            default -> 60L;
        };
        return lastSentAt.plusSeconds(requiredSeconds).isBefore(now);
    }

    private List<CompanionSignal> topCandidates(PlayerCompanionState state) {
        return state.pendingSignals.values().stream()
                .filter(signal -> signal.isReady(currentTick))
                .sorted(Comparator.comparingInt(CompanionSignal::priority).reversed().thenComparingLong(CompanionSignal::occurredTick).reversed())
                .limit(4)
                .toList();
    }

    private List<CompanionSignal> selectedSignals(List<CompanionSignal> candidates, List<String> selectedSignalIds) {
        if (selectedSignalIds == null || selectedSignalIds.isEmpty()) {
            return List.of();
        }
        Set<String> wanted = new LinkedHashSet<>(selectedSignalIds);
        List<CompanionSignal> selected = new ArrayList<>();
        for (CompanionSignal candidate : candidates) {
            if (wanted.contains(candidate.signalId())) {
                selected.add(candidate);
            }
        }
        return selected;
    }

    private AppServerModels.CompanionTriggerPayload buildCompanionTrigger(List<CompanionSignal> signals) {
        CompanionSignal primary = signals.get(0);
        AppServerModels.CompanionTriggerPayload trigger = new AppServerModels.CompanionTriggerPayload();
        trigger.mode = "proactive_companion";
        trigger.primary_signal = primary.toPayload();
        for (int index = 1; index < signals.size(); index++) {
            trigger.supporting_signals.add(signals.get(index).toPayload());
        }
        trigger.synthetic = true;
        trigger.occurred_at = primary.occurredAt().toString();
        trigger.importance = primary.importance();
        trigger.delivery_constraints = defaultDeliveryConstraints();
        return trigger;
    }

    private AppServerModels.CompanionDeliveryConstraintsPayload defaultDeliveryConstraints() {
        AppServerModels.CompanionDeliveryConstraintsPayload payload = new AppServerModels.CompanionDeliveryConstraintsPayload();
        payload.style = "restrained";
        payload.interrupt_policy = "never";
        payload.max_selected_signals = 2;
        return payload;
    }

    private String defaultSyntheticUserMessage() {
        return "Produce one brief companion-first proactive message for the selected companion signals.";
    }

    private void removeSignals(PlayerCompanionState state, List<CompanionSignal> signals) {
        for (CompanionSignal signal : signals) {
            state.pendingSignals.remove(signal.dedupeKey());
        }
    }

    private void markDelivered(PlayerCompanionState state, List<CompanionSignal> signals) {
        Instant now = Instant.now();
        state.lastCompanionTurnAt = now;
        for (CompanionSignal signal : signals) {
            state.lastSentAtByKind.put(signal.kind(), now);
            switch (signal.kind()) {
                case "advancement_celebration" -> {
                    Object rawAdvancements = signal.payload().get("advancements");
                    if (rawAdvancements instanceof List<?> items) {
                        for (Object item : items) {
                            if (item instanceof Map<?, ?> entry) {
                                Object rawId = entry.get("advancement_id");
                                if (rawId != null) {
                                    state.celebratedAdvancementIds.add(String.valueOf(rawId));
                                }
                            }
                        }
                    }
                }
                case "milestone_encouragement" -> {
                    Object milestone = signal.payload().get("milestone_key");
                    if (milestone != null) {
                        state.celebratedMilestones.add(String.valueOf(milestone));
                    }
                }
                case "death_followup" -> state.pendingDeathFollowup = null;
                case "repetition_comfort" -> state.lastActivityFamily = String.valueOf(signal.payload().get("activity_family"));
                default -> {
                }
            }
        }
    }

    private void loadPersistedCompanionStateAsync(String threadId, String playerName, UUID playerId) {
        ioExecutor.submit(() -> {
            try {
                appServerClient.ensureThread(threadId, threadId, playerName);
                JsonObject result = appServerClient.readThread(threadId, false);
                JsonObject thread = result.has("thread") && result.get("thread").isJsonObject()
                        ? result.getAsJsonObject("thread")
                        : null;
                if (thread == null || !thread.has("metadata") || !thread.get("metadata").isJsonObject()) {
                    return;
                }
                JsonObject metadata = thread.getAsJsonObject("metadata");
                JsonObject companion = metadata.has("companion") && metadata.get("companion").isJsonObject()
                        ? metadata.getAsJsonObject("companion")
                        : null;
                if (companion == null) {
                    return;
                }
                synchronized (this) {
                    PlayerCompanionState state = state(playerId);
                    hydrateStateFromMetadata(state, companion);
                }
            } catch (IOException | InterruptedException ignored) {
                // best effort
            }
        });
    }

    private void syncCompanionMetadataAsync(String threadId, PlayerCompanionState stateSnapshot) {
        Map<String, Object> payload = Map.of("companion", companionMetadata(stateSnapshot));
        ioExecutor.submit(() -> {
            try {
                appServerClient.updateThreadMetadata(threadId, payload);
            } catch (IOException | InterruptedException ignored) {
                // best effort
            }
        });
    }

    private Map<String, Object> companionStateSnapshot(PlayerCompanionState state) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("last_join_greeting_at", iso(state.lastSentAtByKind.get("player_join_greeting")));
        payload.put("last_danger_warning_at", iso(state.lastSentAtByKind.get("danger_warning")));
        payload.put("last_repetition_comfort_at", iso(state.lastSentAtByKind.get("repetition_comfort")));
        payload.put("pending_death_followup", state.pendingDeathFollowup);
        payload.put("celebrated_advancement_ids", List.copyOf(state.celebratedAdvancementIds));
        payload.put("celebrated_milestones", List.copyOf(state.celebratedMilestones));
        payload.put("last_activity_family", state.lastActivityFamily);
        payload.put("last_companion_turn_at", iso(state.lastCompanionTurnAt));
        payload.put("player_messaged_since_join", state.playerMessagedSinceJoin);
        return payload;
    }

    private Map<String, Object> companionMetadata(PlayerCompanionState state) {
        return companionStateSnapshot(state);
    }

    private void hydrateStateFromMetadata(PlayerCompanionState state, JsonObject companion) {
        hydrateInstant(companion, "last_join_greeting_at", instant -> state.lastSentAtByKind.put("player_join_greeting", instant));
        hydrateInstant(companion, "last_danger_warning_at", instant -> state.lastSentAtByKind.put("danger_warning", instant));
        hydrateInstant(companion, "last_repetition_comfort_at", instant -> state.lastSentAtByKind.put("repetition_comfort", instant));
        hydrateInstant(companion, "last_companion_turn_at", instant -> state.lastCompanionTurnAt = instant);
        if (companion.has("last_activity_family") && !companion.get("last_activity_family").isJsonNull()) {
            state.lastActivityFamily = companion.get("last_activity_family").getAsString();
        }
        if (companion.has("pending_death_followup") && companion.get("pending_death_followup").isJsonObject()) {
            state.pendingDeathFollowup = jsonObjectToMap(companion.getAsJsonObject("pending_death_followup"));
        }
        if (companion.has("celebrated_advancement_ids") && companion.get("celebrated_advancement_ids").isJsonArray()) {
            companion.getAsJsonArray("celebrated_advancement_ids").forEach(element -> state.celebratedAdvancementIds.add(element.getAsString()));
        }
        if (companion.has("celebrated_milestones") && companion.get("celebrated_milestones").isJsonArray()) {
            companion.getAsJsonArray("celebrated_milestones").forEach(element -> state.celebratedMilestones.add(element.getAsString()));
        }
    }

    private void hydrateInstant(JsonObject source, String key, java.util.function.Consumer<Instant> consumer) {
        if (!source.has(key) || source.get(key).isJsonNull()) {
            return;
        }
        try {
            consumer.accept(Instant.parse(source.get(key).getAsString()));
        } catch (Exception ignored) {
            // ignore invalid persisted values
        }
    }

    private Map<String, Object> jsonObjectToMap(JsonObject object) {
        Map<String, Object> payload = new LinkedHashMap<>();
        for (Map.Entry<String, JsonElement> entry : object.entrySet()) {
            JsonElement value = entry.getValue();
            if (value == null || value.isJsonNull()) {
                payload.put(entry.getKey(), null);
            } else if (value.isJsonPrimitive()) {
                if (value.getAsJsonPrimitive().isBoolean()) {
                    payload.put(entry.getKey(), value.getAsBoolean());
                } else if (value.getAsJsonPrimitive().isNumber()) {
                    payload.put(entry.getKey(), value.getAsNumber());
                } else {
                    payload.put(entry.getKey(), value.getAsString());
                }
            } else if (value.isJsonObject()) {
                payload.put(entry.getKey(), jsonObjectToMap(value.getAsJsonObject()));
            }
        }
        return payload;
    }

    private static String stringAt(Map<String, Object> payload, String branch, String key) {
        Object rawBranch = payload.get(branch);
        if (!(rawBranch instanceof Map<?, ?> branchMap)) {
            return "";
        }
        Object value = branchMap.get(key);
        return value == null ? "" : String.valueOf(value);
    }

    private static boolean hasNonEmptyList(Object value) {
        return value instanceof List<?> list && !list.isEmpty();
    }

    private static int floorBucket(double coordinate, int bucketSize) {
        return (int) Math.floor(coordinate / Math.max(1, bucketSize));
    }

    private static String iso(Instant instant) {
        return instant == null ? null : instant.toString();
    }

    private static String tierOf(String itemId) {
        String normalized = itemId == null ? "" : itemId.toLowerCase(Locale.ROOT);
        if (normalized.contains("netherite")) {
            return "netherite";
        }
        if (normalized.contains("diamond")) {
            return "diamond";
        }
        if (normalized.contains("iron")) {
            return "iron";
        }
        return "";
    }

    private static boolean isBossLike(String entityId) {
        String normalized = entityId == null ? "" : entityId.toLowerCase(Locale.ROOT);
        return normalized.endsWith(":warden")
                || normalized.endsWith(":wither")
                || normalized.endsWith(":ender_dragon")
                || normalized.endsWith(":elder_guardian")
                || normalized.endsWith(":ravager");
    }

    private static String advancementHash(List<Map<String, Object>> advancements) {
        List<String> ids = new ArrayList<>();
        for (Map<String, Object> advancement : advancements) {
            Object rawId = advancement.get("advancement_id");
            if (rawId != null) {
                ids.add(String.valueOf(rawId));
            }
        }
        ids.sort(String::compareTo);
        return String.join(",", ids);
    }

    private PlayerCompanionState state(UUID playerId) {
        return playerStates.computeIfAbsent(playerId, ignored -> new PlayerCompanionState());
    }

    private static String normalize(String value) {
        return value == null ? "" : value.trim().toLowerCase(Locale.ROOT);
    }

    private static final class PlayerCompanionState {
        private String playerUuid;
        private String playerName;
        private boolean online;
        private boolean evaluationInFlight;
        private boolean playerMessagedSinceJoin;
        private long sessionJoinTick;
        private long nextEvaluationTick;
        private Instant lastUserMessageAt;
        private String lastUserMessage;
        private Instant lastCompanionTurnAt;
        private final Map<String, CompanionSignal> pendingSignals = new LinkedHashMap<>();
        private final Map<String, Instant> lastSentAtByKind = new LinkedHashMap<>();
        private final Set<String> knownAdvancementIds = new LinkedHashSet<>();
        private final Set<String> celebratedAdvancementIds = new LinkedHashSet<>();
        private final Set<String> celebratedMilestones = new LinkedHashSet<>();
        private Map<String, Object> pendingDeathFollowup;
        private final PlayerActivityTracker.ActivityState activityState = new PlayerActivityTracker.ActivityState();
        private long baseEvidenceTicks;
        private String baseBucket = "";
        private String lastActivityFamily;
    }
}
