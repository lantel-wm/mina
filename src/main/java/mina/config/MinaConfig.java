package mina.config;

import mina.MinaMod;
import mina.policy.PlayerRole;

import java.net.URI;
import java.net.URISyntaxException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Duration;
import java.util.EnumMap;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.UUID;

public record MinaConfig(
        URI agentBaseUrl,
        Duration connectTimeout,
        Duration requestTimeout,
        int ioThreads,
        int maxAgentSteps,
        int maxBridgeActionsPerTurn,
        int maxContinuationDepth,
        int targetReachBlocks,
        int inventorySummaryLimit,
        int serverRuleSummaryLimit,
        boolean enableExperimentalCapabilities,
        boolean enableDynamicScripting,
        Path configFile,
        Map<UUID, PlayerRole> roleOverrides
) {
    private static final URI DEFAULT_AGENT_BASE_URL = URI.create("http://127.0.0.1:8787");

    public static MinaConfig load() {
        Path configFile = resolveConfigFile();
        Properties properties = new Properties();

        if (Files.exists(configFile)) {
            try (var input = Files.newInputStream(configFile)) {
                properties.load(input);
            } catch (Exception exception) {
                MinaMod.LOGGER.warn("Failed to load Mina config file {}", configFile, exception);
            }
        }

        return new MinaConfig(
                readUri("MINA_AGENT_BASE_URL", "mina.agent.base_url", properties, DEFAULT_AGENT_BASE_URL),
                Duration.ofMillis(readInt("MINA_AGENT_CONNECT_TIMEOUT_MS", "mina.agent.connect_timeout_ms", properties, 1_500)),
                Duration.ofMillis(readInt("MINA_AGENT_REQUEST_TIMEOUT_MS", "mina.agent.request_timeout_ms", properties, 25_000)),
                readInt("MINA_IO_THREADS", "mina.io_threads", properties, 4),
                readInt("MINA_MAX_AGENT_STEPS", "mina.max_agent_steps", properties, 8),
                readInt("MINA_MAX_BRIDGE_ACTIONS_PER_TURN", "mina.max_bridge_actions_per_turn", properties, 8),
                readInt("MINA_MAX_CONTINUATION_DEPTH", "mina.max_continuation_depth", properties, 8),
                readInt("MINA_TARGET_REACH_BLOCKS", "mina.target_reach_blocks", properties, 6),
                readInt("MINA_INVENTORY_SUMMARY_LIMIT", "mina.inventory_summary_limit", properties, 8),
                readInt("MINA_SERVER_RULE_SUMMARY_LIMIT", "mina.server_rule_summary_limit", properties, 12),
                readBoolean("MINA_ENABLE_EXPERIMENTAL", "mina.enable_experimental", properties, false),
                readBoolean("MINA_ENABLE_DYNAMIC_SCRIPTING", "mina.enable_dynamic_scripting", properties, false),
                configFile,
                readRoleOverrides(properties)
        );
    }

    private static Path resolveConfigFile() {
        String env = System.getenv("MINA_CONFIG_FILE");
        if (env != null && !env.isBlank()) {
            return Paths.get(env);
        }

        return Paths.get("config", "mina.properties");
    }

    private static URI readUri(String envKey, String propertyKey, Properties properties, URI defaultValue) {
        String raw = readString(envKey, propertyKey, properties, null);
        if (raw == null || raw.isBlank()) {
            return defaultValue;
        }

        try {
            return new URI(raw);
        } catch (URISyntaxException exception) {
            MinaMod.LOGGER.warn("Invalid URI for {} / {}: {}", envKey, propertyKey, raw);
            return defaultValue;
        }
    }

    private static int readInt(String envKey, String propertyKey, Properties properties, int defaultValue) {
        String raw = readString(envKey, propertyKey, properties, null);
        if (raw == null || raw.isBlank()) {
            return defaultValue;
        }

        try {
            return Integer.parseInt(raw.trim());
        } catch (NumberFormatException exception) {
            MinaMod.LOGGER.warn("Invalid integer for {} / {}: {}", envKey, propertyKey, raw);
            return defaultValue;
        }
    }

    private static boolean readBoolean(String envKey, String propertyKey, Properties properties, boolean defaultValue) {
        String raw = readString(envKey, propertyKey, properties, null);
        if (raw == null || raw.isBlank()) {
            return defaultValue;
        }

        return Boolean.parseBoolean(raw.trim());
    }

    private static String readString(String envKey, String propertyKey, Properties properties, String defaultValue) {
        String env = System.getenv(envKey);
        if (env != null && !env.isBlank()) {
            return env;
        }

        String property = properties.getProperty(propertyKey);
        if (property != null && !property.isBlank()) {
            return property;
        }

        return defaultValue;
    }

    private static Map<UUID, PlayerRole> readRoleOverrides(Properties properties) {
        Map<UUID, PlayerRole> overrides = new HashMap<>();

        for (String propertyName : properties.stringPropertyNames()) {
            if (!propertyName.startsWith("role.override.")) {
                continue;
            }

            String rawUuid = propertyName.substring("role.override.".length());

            try {
                UUID playerId = UUID.fromString(rawUuid);
                PlayerRole role = PlayerRole.fromConfig(properties.getProperty(propertyName));
                overrides.put(playerId, role);
            } catch (IllegalArgumentException exception) {
                MinaMod.LOGGER.warn("Ignoring invalid role override {}", propertyName);
            }
        }

        return Map.copyOf(overrides);
    }
}
