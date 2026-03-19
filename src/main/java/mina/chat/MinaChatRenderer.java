package mina.chat;

import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.ClickEvent;
import net.minecraft.text.HoverEvent;
import net.minecraft.text.MutableText;
import net.minecraft.text.Style;
import net.minecraft.text.StyleSpriteSource;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;

import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

public final class MinaChatRenderer {
    private static final StyleSpriteSource.Font MINA_FONT = new StyleSpriteSource.Font(Identifier.ofVanilla("uniform"));

    private static final int USER_PREFIX_COLOR = 0x8FA3B8;
    private static final int USER_PREFIX_SHADOW = 0x243444;
    private static final int USER_BODY_COLOR = 0xE6EDF3;
    private static final int USER_BODY_SHADOW = 0x1C2833;
    private static final int MINA_PREFIX_COLOR = 0x51C4FF;
    private static final int MINA_PREFIX_SHADOW = 0x0D3656;
    private static final int MINA_TITLE_COLOR = 0xF3FBFF;
    private static final int MINA_TITLE_SHADOW = 0x18465B;
    private static final int MINA_BODY_COLOR = 0xD6F7FF;
    private static final int MINA_BODY_SHADOW = 0x12303A;
    private static final int STATUS_COLOR = 0x8BD5CA;
    private static final int STATUS_SHADOW = 0x21433D;
    private static final int ERROR_COLOR = 0xFF8B8B;
    private static final int ERROR_SHADOW = 0x4A1717;
    private static final int ERROR_BODY_COLOR = 0xFFE2E2;
    private static final int ERROR_BODY_SHADOW = 0x3A2020;
    private static final int CHIP_COLOR = 0xB7C6D3;
    private static final int CHIP_SHADOW = 0x243444;
    private static final ProcessingVariant[] PROCESSING_VARIANTS = new ProcessingVariant[]{
            new ProcessingVariant("少女祈祷中", "Mina 正在向星光低声祈愿，答案很快就会落下来。", "命运的线索正在慢慢显形，请再等我一下下。"),
            new ProcessingVariant("星盘推演中", "星盘还在缓缓转动，眼前的迹象正在一点点变清楚。", "我正在替你读懂这些征兆，马上就把结果说给你听。"),
            new ProcessingVariant("月影占卜中", "月影正落在牌面上，合适的答案快要浮现啦。", "让我再听一听夜色里的回响，很快就告诉你下一步。"),
            new ProcessingVariant("命运解读中", "别急，命运的低语已经传过来了，只差最后一点确认。", "散落的预兆正在拼起来，马上就能读出清楚的方向。"),
            new ProcessingVariant("星砂凝望中", "星砂还在指尖轻轻流动，Mina 正在捕捉最亮的那一道启示。", "我正在顺着这些细小的征兆往前看，很快就会给你回答。")
    };

    private MinaChatRenderer() {
    }

    public static void sendUserEcho(ServerCommandSource source, String message) {
        String playerName = source.getDisplayName().getString();
        source.sendFeedback(() -> userEcho(playerName, message), false);
    }

    public static void sendProcessing(ServerCommandSource source) {
        source.sendFeedback(MinaChatRenderer::processing, false);
    }

    public static void sendReply(ServerPlayerEntity player, ReplyPresentation presentation) {
        player.sendMessage(reply(presentation), false);
    }

    public static void sendActionTrace(ServerPlayerEntity player, ActionTracePresentation presentation) {
        player.sendMessage(actionTrace(presentation), false);
    }

    public static void sendErrorReply(ServerPlayerEntity player, String message) {
        player.sendMessage(error("Error", message), false);
    }

    public static Text commandError(String message) {
        return error("Mina", message);
    }

    public static Text userEcho(String playerName, String message) {
        String suggestedCommand = "/mina " + message;
        Style headerStyle = Style.EMPTY
                .withColor(USER_PREFIX_COLOR)
                .withShadowColor(USER_PREFIX_SHADOW)
                .withBold(true)
                .withHoverEvent(new HoverEvent.ShowText(Text.literal("Click to reuse this prompt.")))
                .withClickEvent(new ClickEvent.SuggestCommand(suggestedCommand))
                .withInsertion(suggestedCommand);

        return panel(
                badge(playerName, headerStyle),
                multilineBody(
                        message,
                        USER_BODY_COLOR,
                        USER_BODY_SHADOW,
                        Style.EMPTY,
                        "  "
                )
        );
    }

    public static Text processing() {
        ProcessingVariant variant = randomProcessingVariant();
        MutableText status = Text.literal(variant.hoverText())
                .styled(style -> style.withColor(STATUS_COLOR).withShadowColor(STATUS_SHADOW).withItalic(true));
        return Text.empty()
                .append(badge("Mina", Style.EMPTY.withColor(MINA_PREFIX_COLOR).withShadowColor(MINA_PREFIX_SHADOW).withBold(true)))
                .append(Text.literal(" "))
                .append(statusChip(variant.label(), status))
                .append(Text.literal("\n"))
                .append(indentedHint(
                        variant.hint(),
                        STATUS_COLOR,
                        STATUS_SHADOW
                ));
    }

    public static Text reply(ReplyPresentation presentation) {
        MutableText root = Text.empty()
                .append(Text.empty()
                        .append(badge("Mina", Style.EMPTY.withColor(MINA_PREFIX_COLOR).withShadowColor(MINA_PREFIX_SHADOW).withBold(true)))
                        .append(Text.literal(" "))
                        .append(copyChip(presentation.body())));

        if (!presentation.title().isBlank()) {
            root.append(Text.literal("\n"));
            root.append(titleLine(presentation.title()));
        }

        if (!presentation.body().isBlank()) {
            root.append(Text.literal("\n"));
            root.append(multilineBody(
                    presentation.body(),
                    MINA_BODY_COLOR,
                    MINA_BODY_SHADOW,
                    Style.EMPTY.withFont(MINA_FONT),
                    "    "
            ));
        }

        if (!presentation.secondary().isEmpty()) {
            root.append(Text.literal("\n"));
            root.append(secondaryLine(presentation.secondary()));
        }

        if (presentation.note() != null && !presentation.note().isBlank()) {
            root.append(Text.literal("\n"));
            root.append(indentedHint(
                    presentation.note(),
                    palette(presentation.noteTone()).textColor(),
                    palette(presentation.noteTone()).shadowColor()
            ));
        }

        return root;
    }

    public static Text actionTrace(ActionTracePresentation presentation) {
        MutableText root = Text.empty()
                .append(badge("Mina", Style.EMPTY.withColor(MINA_PREFIX_COLOR).withShadowColor(MINA_PREFIX_SHADOW).withBold(true)))
                .append(Text.literal(" "))
                .append(chip(presentation.statusLabel(), presentation.statusTone()))
                .append(Text.literal("\n"))
                .append(Text.empty()
                        .append(Text.literal("  "))
                        .append(Text.literal(presentation.title())
                                .styled(style -> style.withColor(MINA_TITLE_COLOR).withShadowColor(MINA_TITLE_SHADOW).withBold(true))));

        if (presentation.detail() != null && !presentation.detail().isBlank()) {
            root.append(Text.literal("\n"));
            root.append(indentedHint(
                    presentation.detail(),
                    MINA_BODY_COLOR,
                    MINA_BODY_SHADOW
            ));
        }

        if (!presentation.secondary().isEmpty()) {
            root.append(Text.literal("\n"));
            root.append(secondaryLine(presentation.secondary()));
        }

        return root;
    }

    private static Text error(String label, String message) {
        return panel(
                badge(label, Style.EMPTY.withColor(ERROR_COLOR).withShadowColor(ERROR_SHADOW).withBold(true)),
                multilineBody(
                        message,
                        ERROR_BODY_COLOR,
                        ERROR_BODY_SHADOW,
                        Style.EMPTY,
                        "  "
                )
        );
    }

    private static MutableText panel(Text header, Text body) {
        return Text.empty()
                .append(header)
                .append(Text.literal("\n"))
                .append(body);
    }

    private static MutableText badge(String label, Style labelStyle) {
        return Text.empty()
                .append(Text.literal("[")
                        .setStyle(labelStyle.withBold(false)))
                .append(Text.literal(label)
                        .setStyle(labelStyle))
                .append(Text.literal("]")
                        .setStyle(labelStyle.withBold(false)));
    }

    private static MutableText copyChip(String message) {
        Style chipStyle = Style.EMPTY
                .withColor(CHIP_COLOR)
                .withShadowColor(CHIP_SHADOW)
                .withHoverEvent(new HoverEvent.ShowText(Text.literal("点击复制 Mina 的回复。")))
                .withClickEvent(new ClickEvent.CopyToClipboard(message));
        return badge("复制", chipStyle);
    }

    private static MutableText statusChip(String label, Text hoverText) {
        Style chipStyle = Style.EMPTY
                .withColor(STATUS_COLOR)
                .withShadowColor(STATUS_SHADOW)
                .withHoverEvent(new HoverEvent.ShowText(hoverText));
        return badge(label, chipStyle);
    }

    private static MutableText titleLine(String title) {
        return Text.empty()
                .append(Text.literal("  "))
                .append(Text.literal(title)
                        .styled(style -> style.withColor(MINA_TITLE_COLOR).withShadowColor(MINA_TITLE_SHADOW).withBold(true)));
    }

    private static MutableText secondaryLine(List<SecondaryChip> chips) {
        MutableText line = Text.empty().append(Text.literal("  "));
        for (int index = 0; index < chips.size(); index++) {
            if (index > 0) {
                line.append(Text.literal(" "));
            }
            line.append(chip(chips.get(index).label(), chips.get(index).tone()));
        }
        return line;
    }

    private static MutableText chip(String label, ChipTone tone) {
        TonePalette palette = palette(tone);
        return badge(label, Style.EMPTY.withColor(palette.textColor()).withShadowColor(palette.shadowColor()));
    }

    private static MutableText multilineBody(
            String message,
            int bodyColor,
            int bodyShadow,
            Style baseStyle,
            String linePrefix
    ) {
        String normalized = message == null ? "" : message;
        String[] lines = normalized.split("\\R", -1);
        MutableText body = Text.empty();
        Style lineStyle = baseStyle.withColor(bodyColor).withShadowColor(bodyShadow);

        for (int index = 0; index < lines.length; index++) {
            if (index > 0) {
                body.append(Text.literal("\n"));
            }

            body.append(Text.literal(linePrefix));
            if (lines[index].isEmpty()) {
                body.append(Text.literal(" "));
                continue;
            }

            body.append(Text.literal(lines[index]).setStyle(lineStyle));
        }

        return body;
    }

    private static MutableText indentedHint(String message, int color, int shadowColor) {
        return Text.empty()
                .append(Text.literal("  "))
                .append(Text.literal(message)
                        .styled(style -> style.withColor(color).withShadowColor(shadowColor).withItalic(true)));
    }

    private static ProcessingVariant randomProcessingVariant() {
        return PROCESSING_VARIANTS[ThreadLocalRandom.current().nextInt(PROCESSING_VARIANTS.length)];
    }

    private static TonePalette palette(ChipTone tone) {
        return switch (tone) {
            case SUCCESS -> new TonePalette(0x7BE0A5, 0x173A2A);
            case INFO -> new TonePalette(0x8BD5FF, 0x17344A);
            case WARNING -> new TonePalette(0xFFD37A, 0x4A3210);
            case ERROR -> new TonePalette(ERROR_COLOR, ERROR_SHADOW);
            case MUTED -> new TonePalette(CHIP_COLOR, CHIP_SHADOW);
        };
    }

    private record TonePalette(int textColor, int shadowColor) {
    }

    private record ProcessingVariant(String label, String hoverText, String hint) {
    }

    public enum ChipTone {
        SUCCESS,
        INFO,
        WARNING,
        ERROR,
        MUTED
    }

    public record SecondaryChip(String label, ChipTone tone) {
    }

    public record ReplyPresentation(
            String title,
            String body,
            List<SecondaryChip> secondary,
            String note,
            ChipTone noteTone
    ) {
        public ReplyPresentation {
            title = title == null ? "" : title;
            body = body == null ? "" : body;
            secondary = secondary == null ? List.of() : List.copyOf(secondary);
            noteTone = noteTone == null ? ChipTone.MUTED : noteTone;
        }
    }

    public record ActionTracePresentation(
            String statusLabel,
            ChipTone statusTone,
            String title,
            String detail,
            List<SecondaryChip> secondary
    ) {
        public ActionTracePresentation {
            statusLabel = statusLabel == null ? "" : statusLabel;
            statusTone = statusTone == null ? ChipTone.MUTED : statusTone;
            title = title == null ? "" : title;
            detail = detail == null ? "" : detail;
            secondary = secondary == null ? List.of() : List.copyOf(secondary);
        }
    }
}
