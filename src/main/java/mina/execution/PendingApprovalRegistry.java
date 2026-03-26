package mina.execution;

import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

public final class PendingApprovalRegistry {
    private final ConcurrentMap<UUID, PendingApproval> approvals = new ConcurrentHashMap<>();

    public PendingApproval get(UUID playerId) {
        return approvals.get(playerId);
    }

    public PendingApproval put(UUID playerId, String threadId, String turnId, String approvalId, String effectSummary) {
        PendingApproval pending = new PendingApproval(playerId, threadId, turnId, approvalId, effectSummary);
        approvals.put(playerId, pending);
        return pending;
    }

    public void clear(UUID playerId) {
        approvals.remove(playerId);
    }

    public void clearAll() {
        approvals.clear();
    }

    public static final class PendingApproval {
        private final UUID playerId;
        private final String threadId;
        private final String turnId;
        private final String approvalId;
        private final String effectSummary;
        private final CompletableFuture<ApprovalDecision> decisionFuture = new CompletableFuture<>();

        private PendingApproval(UUID playerId, String threadId, String turnId, String approvalId, String effectSummary) {
            this.playerId = playerId;
            this.threadId = threadId;
            this.turnId = turnId;
            this.approvalId = approvalId;
            this.effectSummary = effectSummary;
        }

        public UUID playerId() {
            return playerId;
        }

        public String threadId() {
            return threadId;
        }

        public String turnId() {
            return turnId;
        }

        public String approvalId() {
            return approvalId;
        }

        public String effectSummary() {
            return effectSummary;
        }

        public CompletableFuture<ApprovalDecision> decisionFuture() {
            return decisionFuture;
        }
    }

    public record ApprovalDecision(boolean approved, String reason) {
    }
}
