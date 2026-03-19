package mina.command;

import com.mojang.brigadier.Command;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;
import mina.MinaRuntime;
import mina.chat.MinaChatRenderer;
import com.mojang.brigadier.exceptions.CommandSyntaxException;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.text.Text;

public final class MinaCommand {
    private MinaCommand() {
    }

    public static void register() {
        CommandRegistrationCallback.EVENT.register((dispatcher, registryAccess, environment) -> {
            if (!environment.dedicated) {
                return;
            }

            dispatcher.register(
                    CommandManager.literal("mina")
                            .then(CommandManager.argument("message", StringArgumentType.greedyString())
                                    .executes(MinaCommand::execute))
            );
        });
    }

    private static int execute(CommandContext<ServerCommandSource> context) throws CommandSyntaxException {
        ServerCommandSource source = context.getSource();
        String message = StringArgumentType.getString(context, "message").trim();

        if (message.isEmpty()) {
            source.sendError(Text.literal("Usage: /mina <message>"));
            return 0;
        }

        if (!MinaRuntime.getInstance().isStarted()) {
            source.sendError(Text.literal("Mina runtime is not started yet."));
            return 0;
        }

        if (MinaRuntime.getInstance().turnCoordinator().submitTurn(source, message, () -> {
            MinaChatRenderer.sendUserEcho(source, message);
            MinaChatRenderer.sendProcessing(source);
        })) {
            return Command.SINGLE_SUCCESS;
        }

        return 0;
    }
}
