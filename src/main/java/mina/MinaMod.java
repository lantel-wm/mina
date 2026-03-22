package mina;

import mina.command.MinaCommand;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.entity.event.v1.ServerEntityCombatEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerEntityWorldChangeEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerLivingEntityEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerPlayerEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerEntityEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.minecraft.server.network.ServerPlayerEntity;
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
        ServerPlayerEvents.JOIN.register(player -> MinaRuntime.getInstance().onPlayerJoin(player));
        ServerPlayerEvents.LEAVE.register(player -> MinaRuntime.getInstance().onPlayerLeave(player));
        ServerPlayerEvents.AFTER_RESPAWN.register(
                (oldPlayer, newPlayer, alive) -> MinaRuntime.getInstance().onPlayerRespawn(oldPlayer, newPlayer, alive)
        );
        ServerLivingEntityEvents.AFTER_DAMAGE.register((entity, source, baseDamageTaken, damageTaken, blocked) -> {
            if (entity instanceof ServerPlayerEntity player) {
                MinaRuntime.getInstance().onPlayerAfterDamage(player, source, baseDamageTaken, damageTaken, blocked);
            }
        });
        ServerLivingEntityEvents.AFTER_DEATH.register((entity, source) -> {
            if (entity instanceof ServerPlayerEntity player) {
                MinaRuntime.getInstance().onPlayerDeath(player, source);
            }
        });
        ServerEntityWorldChangeEvents.AFTER_PLAYER_CHANGE_WORLD.register(
                (player, origin, destination) -> MinaRuntime.getInstance().onPlayerChangeWorld(player, origin, destination)
        );
        ServerEntityCombatEvents.AFTER_KILLED_OTHER_ENTITY.register((world, entity, killedEntity, damageSource) -> {
            if (entity instanceof ServerPlayerEntity player) {
                MinaRuntime.getInstance().onPlayerKilledEntity(player, killedEntity, damageSource);
            }
        });
        ServerEntityEvents.ENTITY_LOAD.register((entity, world) -> MinaRuntime.getInstance().onEntityLoad(entity, world));
        ServerEntityEvents.ENTITY_UNLOAD.register((entity, world) -> MinaRuntime.getInstance().onEntityUnload(entity, world));
        ServerEntityEvents.EQUIPMENT_CHANGE.register((livingEntity, slot, previousStack, currentStack) -> {
            if (livingEntity instanceof ServerPlayerEntity player) {
                MinaRuntime.getInstance().onPlayerEquipmentChange(player, slot, previousStack, currentStack);
            }
        });
        ServerTickEvents.END_SERVER_TICK.register(server -> MinaRuntime.getInstance().onServerTick(server));
        LOGGER.info("Mina bootstrap registered.");
    }
}
