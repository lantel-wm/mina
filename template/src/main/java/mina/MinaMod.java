package mina;

import net.fabricmc.api.ModInitializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class MinaMod implements ModInitializer {

    public static final Logger LOGGER = LoggerFactory.getLogger("mina");

    @Override
    public void onInitialize() {
        LOGGER.info("Mina initialized.");
    }
}
