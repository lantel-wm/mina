package mina.policy;

import java.util.Locale;

public enum RiskClass {
    REPLY_ONLY("reply_only"),
    READ_ONLY("read_only"),
    WORLD_LOW_RISK("world_low_risk"),
    ADMIN_MUTATION("admin_mutation"),
    EXPERIMENTAL_PRIVILEGED("experimental_privileged");

    private final String wireValue;

    RiskClass(String wireValue) {
        this.wireValue = wireValue;
    }

    public String wireValue() {
        return wireValue;
    }

    public static RiskClass fromWire(String raw) {
        if (raw == null) {
            return READ_ONLY;
        }

        String normalized = raw.trim().toLowerCase(Locale.ROOT);
        for (RiskClass riskClass : values()) {
            if (riskClass.wireValue.equals(normalized)) {
                return riskClass;
            }
        }
        throw new IllegalArgumentException("Unknown risk class: " + raw);
    }
}
