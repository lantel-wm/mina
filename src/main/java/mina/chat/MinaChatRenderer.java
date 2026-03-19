package mina.chat;

import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.MutableText;
import net.minecraft.text.Style;
import net.minecraft.text.StyleSpriteSource;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;

public final class MinaChatRenderer {
    private static final StyleSpriteSource.Font MINA_FONT = new StyleSpriteSource.Font(Identifier.ofVanilla("uniform"));

    private static final int USER_PREFIX_COLOR = 0x8B949E;
    private static final int USER_BODY_COLOR = 0xE6EDF3;
    private static final int MINA_PREFIX_COLOR = 0x51C4FF;
    private static final int MINA_BODY_COLOR = 0xB8F2FF;
    private static final int STATUS_COLOR = 0x8BD5CA;

    private MinaChatRenderer() {
    }

    public static void sendUserEcho(ServerCommandSource source, String message) {
        source.sendFeedback(() -> userEcho(message), false);
    }

    public static void sendProcessing(ServerCommandSource source) {
        source.sendFeedback(MinaChatRenderer::processing, false);
    }

    public static void sendReply(ServerPlayerEntity player, String message) {
        player.sendMessage(reply(message), false);
    }

    public static Text userEcho(String message) {
        return bubble(
                "You",
                USER_PREFIX_COLOR,
                body(message, USER_BODY_COLOR, Style.EMPTY)
        );
    }

    public static Text processing() {
        MutableText status = Text.literal("Reading your request and preparing a response...")
                .styled(style -> style.withColor(STATUS_COLOR).withItalic(true));
        return bubble("Mina", MINA_PREFIX_COLOR, status);
    }

    public static Text reply(String message) {
        MutableText body = body(
                indentMultiline(message),
                MINA_BODY_COLOR,
                Style.EMPTY.withFont(MINA_FONT)
        );

        return Text.empty()
                .append(header("Mina", MINA_PREFIX_COLOR))
                .append(Text.literal("\n"))
                .append(Text.literal("  "))
                .append(body);
    }

    private static MutableText bubble(String label, int labelColor, Text body) {
        return Text.empty()
                .append(header(label, labelColor))
                .append(Text.literal(" "))
                .append(body);
    }

    private static MutableText header(String label, int labelColor) {
        return Text.empty()
                .append(Text.literal("[")
                        .styled(style -> style.withColor(labelColor)))
                .append(Text.literal(label)
                        .styled(style -> style.withColor(labelColor).withBold(true)))
                .append(Text.literal("]")
                        .styled(style -> style.withColor(labelColor)));
    }

    private static MutableText body(String message, int color, Style baseStyle) {
        return Text.literal(message)
                .setStyle(baseStyle.withColor(color));
    }

    private static String indentMultiline(String message) {
        return message.replace("\n", "\n  ");
    }
}
