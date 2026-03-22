package mina.context;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class RiskStateProvider {
    public Map<String, Object> collect(
            Map<String, Object> playerState,
            Map<String, Object> threats,
            Map<String, Object> environment,
            Map<String, Object> social
    ) {
        return assess(
                playerState,
                threats,
                environment,
                social
        );
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> assess(
            Map<String, Object> playerState,
            Map<String, Object> threats,
            Map<String, Object> environment,
            Map<String, Object> social
    ) {
        Map<String, Object> coreStatus = (Map<String, Object>) playerState.getOrDefault("core_status", Map.of());
        Map<String, Object> damageState = (Map<String, Object>) playerState.getOrDefault("recent_damage_state", Map.of());
        Map<String, Object> hazardSummary = (Map<String, Object>) environment.getOrDefault("hazard_summary", Map.of());

        float health = ((Number) coreStatus.getOrDefault("health", 20.0F)).floatValue();
        int hunger = ((Number) coreStatus.getOrDefault("hunger", 20)).intValue();
        int hostileCount = ((Number) threats.getOrDefault("hostile_count", 0)).intValue();
        int explosiveCount = ((Number) threats.getOrDefault("explosive_count", 0)).intValue();
        boolean recentlyHurt = (Boolean) damageState.getOrDefault("recently_hurt", false);
        boolean longInDanger = (Boolean) damageState.getOrDefault("long_in_danger", false);
        boolean isAlone = (Boolean) social.getOrDefault("is_alone", true);

        List<String> hazards = new ArrayList<>();
        Object hazardList = hazardSummary.get("hazards");
        if (hazardList instanceof List<?> values) {
            for (Object value : values) {
                hazards.add(String.valueOf(value));
            }
        }

        int score = 0;
        List<String> reasons = new ArrayList<>();
        if (health <= 4.0F) {
            score += 3;
            reasons.add("critical_health");
        } else if (health <= 8.0F) {
            score += 2;
            reasons.add("low_health");
        }
        if (hunger <= 6) {
            score += 1;
            reasons.add("low_hunger");
        }
        if (hostileCount > 0) {
            score += hostileCount >= 3 ? 2 : 1;
            reasons.add("nearby_hostiles");
        }
        if (explosiveCount > 0) {
            score += 2;
            reasons.add("nearby_explosives");
        }
        if (!hazards.isEmpty()) {
            score += 2;
            reasons.add("environmental_hazard");
        }
        if (recentlyHurt) {
            score += 1;
            reasons.add("recent_damage");
        }
        if (longInDanger) {
            score += 1;
            reasons.add("prolonged_danger");
        }
        if (isAlone && (hostileCount > 0 || health <= 8.0F)) {
            score += 1;
            reasons.add("alone_under_pressure");
        }

        String level;
        if (score >= 7) {
            level = "critical";
        } else if (score >= 4) {
            level = "high";
        } else if (score >= 2) {
            level = "moderate";
        } else {
            level = "low";
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("level", level);
        payload.put("reasons", reasons);
        payload.put("highest_threat", threats.get("nearest_threat"));
        payload.put("immediate_action_needed", "critical".equals(level) || (!hazards.isEmpty() && health <= 8.0F));
        payload.put("confidence", 0.85);
        return payload;
    }
}
