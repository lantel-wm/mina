package mina.capability;

import java.util.Map;

public record CapabilityResult(
        Map<String, Object> observations,
        String sideEffectSummary
) {
}
