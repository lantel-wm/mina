package mina.execution;

import mina.chat.MinaChatRenderer;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class TurnCoordinatorConsoleTest {
    @Test
    void formatConsoleReplyIncludesPlayerAndBody() {
        String rendered = TurnCoordinator.formatConsoleReply(
                "Steve",
                new MinaChatRenderer.ReplyPresentation(
                        "",
                        "你好，我在。",
                        List.of(),
                        null,
                        MinaChatRenderer.ChipTone.MUTED
                )
        );

        assertEquals("[Mina -> Steve] 你好，我在。", rendered);
    }

    @Test
    void formatConsoleReplyIncludesTitleWhenPresent() {
        String rendered = TurnCoordinator.formatConsoleReply(
                "Alex",
                new MinaChatRenderer.ReplyPresentation(
                        "我替你看到的结果",
                        "附近没有威胁。",
                        List.of(),
                        null,
                        MinaChatRenderer.ChipTone.MUTED
                )
        );

        assertEquals("[Mina -> Alex] 我替你看到的结果 | 附近没有威胁。", rendered);
    }

    @Test
    void formatConsoleErrorIncludesPlayerAndMessage() {
        String rendered = TurnCoordinator.formatConsoleError("Steve", "刚刚这一步有点不对，我再处理也许会更稳。");

        assertEquals("[Mina -> Steve] ERROR | 刚刚这一步有点不对，我再处理也许会更稳。", rendered);
    }
}
