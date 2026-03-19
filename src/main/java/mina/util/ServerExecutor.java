package mina.util;

import net.minecraft.server.MinecraftServer;

import java.util.concurrent.CompletableFuture;
import java.util.function.Supplier;

public final class ServerExecutor {
    private ServerExecutor() {
    }

    public static <T> CompletableFuture<T> call(MinecraftServer server, Supplier<T> supplier) {
        CompletableFuture<T> future = new CompletableFuture<>();
        server.execute(() -> {
            try {
                future.complete(supplier.get());
            } catch (Throwable throwable) {
                future.completeExceptionally(throwable);
            }
        });
        return future;
    }
}
