package mina;

import mina.bridge.AgentServiceClient;
import mina.capability.CapabilityExecutorRegistry;
import mina.command.MinaCommand;
import mina.config.MinaConfig;
import mina.context.GameContextCollector;
import mina.context.PlayerSnapshotProvider;
import mina.context.RecentEventBuffer;
import mina.context.RecentEventsProvider;
import mina.context.TargetBlockSnapshotProvider;
import mina.execution.PendingTurnRegistry;
import mina.execution.PendingConfirmationRegistry;
import mina.execution.TurnCoordinator;
import mina.policy.ExecutionGuard;
import mina.policy.PermissionResolver;
import mina.context.WorldSnapshotProvider;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.MinecraftServer;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;

public final class MinaRuntime {
    private static final MinaRuntime INSTANCE = new MinaRuntime();

    private MinaConfig config;
    private ExecutorService ioExecutor;
    private PendingTurnRegistry pendingTurns;
    private PendingConfirmationRegistry pendingConfirmations;
    private PermissionResolver permissionResolver;
    private CapabilityExecutorRegistry capabilityRegistry;
    private GameContextCollector contextCollector;
    private RecentEventBuffer recentEventBuffer;
    private AgentServiceClient agentServiceClient;
    private ExecutionGuard executionGuard;
    private TurnCoordinator turnCoordinator;
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
        this.pendingConfirmations = new PendingConfirmationRegistry();
        this.permissionResolver = new PermissionResolver(config);
        this.capabilityRegistry = new CapabilityExecutorRegistry(config);
        this.recentEventBuffer = new RecentEventBuffer(24);
        this.contextCollector = new GameContextCollector(
                permissionResolver,
                capabilityRegistry,
                new PlayerSnapshotProvider(config.inventorySummaryLimit()),
                new WorldSnapshotProvider(config.serverRuleSummaryLimit()),
                new TargetBlockSnapshotProvider(config.targetReachBlocks()),
                new RecentEventsProvider(recentEventBuffer),
                config.enableExperimentalCapabilities(),
                config.enableDynamicScripting()
        );
        this.agentServiceClient = new AgentServiceClient(config);
        this.executionGuard = new ExecutionGuard(config, permissionResolver);
        this.turnCoordinator = new TurnCoordinator(
                config,
                agentServiceClient,
                contextCollector,
                capabilityRegistry,
                executionGuard,
                pendingTurns,
                pendingConfirmations,
                recentEventBuffer,
                ioExecutor
        );

        MinaMod.LOGGER.info("Mina runtime started against agent service {}", config.agentBaseUrl());
    }

    public synchronized void stop(MinecraftServer minecraftServer) {
        if (this.server == null) {
            return;
        }

        pendingTurns.closeAll();
        pendingConfirmations.clearAll();
        turnCoordinator = null;
        executionGuard = null;
        agentServiceClient = null;
        contextCollector = null;
        capabilityRegistry = null;
        permissionResolver = null;
        pendingTurns = null;
        pendingConfirmations = null;
        recentEventBuffer = null;

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
        if (recentEventBuffer == null) {
            return;
        }
        recentEventBuffer.recordPlayerEvent(kind, player, Map.of());
    }

    public synchronized void recordTurnEvent(String kind, ServerPlayerEntity player, String message) {
        if (recentEventBuffer == null) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("message", message);
        recentEventBuffer.recordPlayerEvent(kind, player, payload);
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
