package mina.policy;

import java.util.Locale;

public enum PlayerRole {
    CONVERSATION("conversation"),
    READ_ONLY("read_only"),
    LOW_RISK("low_risk"),
    ADMIN("admin"),
    EXPERIMENTAL("experimental");

    private final String wireValue;

    PlayerRole(String wireValue) {
        this.wireValue = wireValue;
    }

    public String wireValue() {
        return wireValue;
    }

    public static PlayerRole fromConfig(String raw) {
        if (raw == null) {
            return READ_ONLY;
        }

        String normalized = raw.trim().toLowerCase(Locale.ROOT);
        for (PlayerRole role : values()) {
            if (role.wireValue.equals(normalized)) {
                return role;
            }
        }
        throw new IllegalArgumentException("Unknown role: " + raw);
    }
}
