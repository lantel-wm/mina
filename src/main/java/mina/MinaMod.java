package mina;

import mina.command.MinaCommand;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public final class MinaMod implements ModInitializer {
    public static final String MOD_ID = "mina";
    public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    @Override
    public void onInitialize() {
        MinaCommand.register();
        ServerLifecycleEvents.SERVER_STARTING.register(MinaRuntime.getInstance()::start);
        ServerLifecycleEvents.SERVER_STOPPING.register(MinaRuntime.getInstance()::stop);
        ServerPlayConnectionEvents.JOIN.register((handler, sender, server) ->
                MinaRuntime.getInstance().recordPlayerEvent("player_joined", handler.getPlayer()));
        ServerPlayConnectionEvents.DISCONNECT.register((handler, server) ->
                MinaRuntime.getInstance().recordPlayerEvent("player_left", handler.getPlayer()));
        LOGGER.info("Mina bootstrap registered.");
    }
}
