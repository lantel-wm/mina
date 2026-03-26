package mina;

import mina.bridge.AppServerClient;
import mina.capability.CarpetObservationBackend;
import mina.capability.CapabilityExecutorRegistry;
import mina.capability.DefaultCarpetObservationBackend;
import mina.capability.DirectWorldReader;
import mina.capability.ServerDirectWorldReader;
import mina.capability.ServerVanillaCommandBackend;
import mina.capability.VanillaCommandBackend;
import mina.companion.CompanionCoordinator;
import mina.command.MinaCommand;
import mina.config.MinaConfig;
import mina.context.EnvironmentAssessmentProvider;
import mina.context.GameContextCollector;
import mina.context.InteractableScanProvider;
import mina.context.PlayerStateProvider;
import mina.context.RecentEventTracker;
import mina.context.TargetBlockSnapshotProvider;
import mina.context.ThreatAssessmentProvider;
import mina.context.WorldStateProvider;
import mina.execution.DevTurnLog;
import mina.execution.PendingTurnRegistry;
import mina.execution.PendingApprovalRegistry;
import mina.execution.TurnCoordinator;
import mina.policy.ExecutionGuard;
import mina.policy.PermissionResolver;
import mina.context.RiskStateProvider;
import mina.context.WorldSnapshotProvider;
import mina.context.SocialStateProvider;
import mina.util.ObservationTextResolver;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.MinecraftServer;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.Entity;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.damage.DamageSource;
import net.minecraft.item.ItemStack;
import net.minecraft.server.world.ServerWorld;
import net.fabricmc.loader.api.FabricLoader;

import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;

public final class MinaRuntime {
    private static final MinaRuntime INSTANCE = new MinaRuntime();

    private MinaConfig config;
    private ExecutorService ioExecutor;
    private PendingTurnRegistry pendingTurns;
    private PendingApprovalRegistry pendingApprovals;
    private PermissionResolver permissionResolver;
    private CapabilityExecutorRegistry capabilityRegistry;
    private GameContextCollector contextCollector;
    private RecentEventTracker recentEventTracker;
    private AppServerClient appServerClient;
    private ExecutionGuard executionGuard;
    private TurnCoordinator turnCoordinator;
    private DevTurnLog devTurnLog;
    private CompanionCoordinator companionCoordinator;
    private MinecraftServer server;

    private MinaRuntime() {
    }

    public static MinaRuntime getInstance() {
        return INSTANCE;
    }

    public synchronized void start(MinecraftServer minecraftServer) {
        if (this.server != null) {
            return;
        }

        this.config = MinaConfig.load();
        this.server = minecraftServer;
        this.ioExecutor = Executors.newFixedThreadPool(
                config.ioThreads(),
                new MinaThreadFactory()
        );
        this.pendingTurns = new PendingTurnRegistry();
        this.pendingApprovals = new PendingApprovalRegistry();
        this.permissionResolver = new PermissionResolver(config);
        ObservationTextResolver observationTextResolver = new ObservationTextResolver(config.observationLanguage());
        this.recentEventTracker = new RecentEventTracker(
                48,
                config.entityScanIntervalTicks(),
                config.longDangerThresholdTicks(),
                observationTextResolver
        );
        PlayerStateProvider playerStateProvider = new PlayerStateProvider(config.inventorySummaryLimit(), observationTextResolver);
        WorldStateProvider worldStateProvider = new WorldStateProvider();
        WorldSnapshotProvider ruleSnapshotProvider = new WorldSnapshotProvider(config.serverRuleSummaryLimit());
        ThreatAssessmentProvider threatAssessmentProvider = new ThreatAssessmentProvider(
                config.entityScanRadius(),
                config.entityScanLimit(),
                observationTextResolver
        );
        InteractableScanProvider interactableScanProvider = new InteractableScanProvider(
                config.interactableScanRadius(),
                config.interactableScanVerticalRange()
        );
        SocialStateProvider socialStateProvider = new SocialStateProvider(config.entityScanRadius());
        EnvironmentAssessmentProvider environmentAssessmentProvider = new EnvironmentAssessmentProvider(config.interactableScanRadius());
        RiskStateProvider riskStateProvider = new RiskStateProvider();
        VanillaCommandBackend vanillaCommandBackend = new ServerVanillaCommandBackend(observationTextResolver);
        CarpetObservationBackend carpetObservationBackend = new DefaultCarpetObservationBackend(
                FabricLoader.getInstance().isModLoaded("carpet")
        );
        DirectWorldReader directWorldReader = new ServerDirectWorldReader(
                playerStateProvider,
                worldStateProvider,
                threatAssessmentProvider,
                environmentAssessmentProvider,
                interactableScanProvider,
                socialStateProvider,
                riskStateProvider,
                recentEventTracker,
                vanillaCommandBackend,
                carpetObservationBackend,
                observationTextResolver
        );
        this.capabilityRegistry = new CapabilityExecutorRegistry(
                config,
                directWorldReader,
                vanillaCommandBackend,
                carpetObservationBackend,
                observationTextResolver
        );
        this.contextCollector = new GameContextCollector(
                permissionResolver,
                capabilityRegistry,
                playerStateProvider,
                worldStateProvider,
                ruleSnapshotProvider,
                directWorldReader,
                new TargetBlockSnapshotProvider(config.targetReachBlocks(), observationTextResolver),
                recentEventTracker,
                config.enableExperimentalCapabilities(),
                config.enableDynamicScripting()
        );
        this.appServerClient = new AppServerClient(config);
        this.executionGuard = new ExecutionGuard(config, permissionResolver);
        this.devTurnLog = DevTurnLog.forRunDirectory(
                minecraftServer.getRunDirectory(),
                FabricLoader.getInstance().isDevelopmentEnvironment()
        );
        this.turnCoordinator = new TurnCoordinator(
                config,
                appServerClient,
                contextCollector,
                capabilityRegistry,
                executionGuard,
                pendingTurns,
                pendingApprovals,
                recentEventTracker,
                devTurnLog,
                ioExecutor
        );
        this.companionCoordinator = new CompanionCoordinator(
                config,
                minecraftServer,
                appServerClient,
                contextCollector,
                turnCoordinator,
                pendingTurns,
                recentEventTracker,
                ioExecutor
        );
        this.turnCoordinator.setCompanionCoordinator(companionCoordinator);

        MinaMod.LOGGER.info("Mina runtime started against agent service {}", config.agentBaseUrl());
    }

    public synchronized void stop(MinecraftServer minecraftServer) {
        if (this.server == null) {
            return;
        }

        pendingTurns.closeAll();
        pendingApprovals.clearAll();
        turnCoordinator = null;
        executionGuard = null;
        companionCoordinator = null;
        if (appServerClient != null) {
            appServerClient.close();
        }
        appServerClient = null;
        contextCollector = null;
        capabilityRegistry = null;
        permissionResolver = null;
        devTurnLog = null;
        pendingTurns = null;
        pendingApprovals = null;
        recentEventTracker = null;

        if (ioExecutor != null) {
            ioExecutor.shutdownNow();
            ioExecutor = null;
        }

        server = null;
        MinaMod.LOGGER.info("Mina runtime stopped.");
    }

    public synchronized boolean isStarted() {
        return server != null && turnCoordinator != null;
    }

    public synchronized TurnCoordinator turnCoordinator() {
        return turnCoordinator;
    }

    public synchronized MinaConfig config() {
        return config;
    }

    public synchronized void recordPlayerEvent(String kind, ServerPlayerEntity player) {
        if (recentEventTracker == null) {
            return;
        }
        recentEventTracker.recordPlayerEvent(kind, player, Map.of());
    }

    public synchronized void recordTurnEvent(String kind, ServerPlayerEntity player, String message) {
        if (recentEventTracker == null) {
            return;
        }
        recentEventTracker.recordTurnEvent(kind, player, message);
    }

    public synchronized void onPlayerJoin(ServerPlayerEntity player) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerJoin(player);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerJoin(player);
        }
    }

    public synchronized void onPlayerLeave(ServerPlayerEntity player) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerLeave(player);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerLeave(player);
        }
    }

    public synchronized void onPlayerRespawn(ServerPlayerEntity oldPlayer, ServerPlayerEntity newPlayer, boolean alive) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerRespawn(oldPlayer, newPlayer, alive);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerRespawn(oldPlayer, newPlayer, alive);
        }
    }

    public synchronized void onPlayerAfterDamage(
            ServerPlayerEntity player,
            DamageSource source,
            float baseDamageTaken,
            float damageTaken,
            boolean blocked
    ) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerAfterDamage(player, source, baseDamageTaken, damageTaken, blocked);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerAfterDamage(player, source, baseDamageTaken, damageTaken, blocked);
        }
    }

    public synchronized void onPlayerDeath(ServerPlayerEntity player, DamageSource source) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerDeath(player, source);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerDeath(player, source);
        }
    }

    public synchronized void onPlayerChangeWorld(ServerPlayerEntity player, ServerWorld origin, ServerWorld destination) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerChangeWorld(player, origin, destination);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerChangeWorld(player, origin, destination);
        }
    }

    public synchronized void onPlayerKilledEntity(ServerPlayerEntity player, LivingEntity killedEntity, DamageSource source) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerKilledEntity(player, killedEntity, source);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerKilledEntity(player, killedEntity, source);
        }
    }

    public synchronized void onPlayerEquipmentChange(
            ServerPlayerEntity player,
            EquipmentSlot slot,
            ItemStack previous,
            ItemStack current
    ) {
        if (recentEventTracker != null) {
            recentEventTracker.onPlayerEquipmentChange(player, slot, previous, current);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onPlayerEquipmentChange(player, slot, previous, current);
        }
    }

    public synchronized void onEntityLoad(Entity entity, ServerWorld world) {
        if (recentEventTracker != null) {
            recentEventTracker.onEntityLoad(entity, world);
        }
    }

    public synchronized void onEntityUnload(Entity entity, ServerWorld world) {
        if (recentEventTracker != null) {
            recentEventTracker.onEntityUnload(entity, world);
        }
    }

    public synchronized void onServerTick(MinecraftServer server) {
        if (recentEventTracker != null) {
            recentEventTracker.onServerTick(server);
        }
        if (companionCoordinator != null) {
            companionCoordinator.onServerTick(server);
        }
    }

    private static final class MinaThreadFactory implements ThreadFactory {
        private int index = 0;

        @Override
        public synchronized Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, "mina-io-" + index++);
            thread.setDaemon(true);
            return thread;
        }
    }
}
