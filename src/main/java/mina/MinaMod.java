package mina;

import mina.command.MinaCommand;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
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
        LOGGER.info("Mina bootstrap registered.");
    }
}
