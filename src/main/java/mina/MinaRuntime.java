package mina;

import mina.bridge.AgentServiceClient;
import mina.capability.CapabilityExecutorRegistry;
import mina.command.MinaCommand;
import mina.config.MinaConfig;
import mina.context.GameContextCollector;
import mina.execution.PendingTurnRegistry;
import mina.execution.TurnCoordinator;
import mina.policy.ExecutionGuard;
import mina.policy.PermissionResolver;
import net.minecraft.server.MinecraftServer;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;

public final class MinaRuntime {
    private static final MinaRuntime INSTANCE = new MinaRuntime();

    private MinaConfig config;
    private ExecutorService ioExecutor;
    private PendingTurnRegistry pendingTurns;
    private PermissionResolver permissionResolver;
    private CapabilityExecutorRegistry capabilityRegistry;
    private GameContextCollector contextCollector;
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
        this.permissionResolver = new PermissionResolver(config);
        this.capabilityRegistry = new CapabilityExecutorRegistry(config);
        this.contextCollector = new GameContextCollector(config, permissionResolver, capabilityRegistry);
        this.agentServiceClient = new AgentServiceClient(config);
        this.executionGuard = new ExecutionGuard(config, permissionResolver);
        this.turnCoordinator = new TurnCoordinator(
                config,
                agentServiceClient,
                contextCollector,
                capabilityRegistry,
                executionGuard,
                pendingTurns,
                ioExecutor
        );

        MinaMod.LOGGER.info("Mina runtime started against agent service {}", config.agentBaseUrl());
    }

    public synchronized void stop(MinecraftServer minecraftServer) {
        if (this.server == null) {
            return;
        }

        pendingTurns.closeAll();
        turnCoordinator = null;
        executionGuard = null;
        agentServiceClient = null;
        contextCollector = null;
        capabilityRegistry = null;
        permissionResolver = null;
        pendingTurns = null;

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
