package mina.util;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

public final class JsonHelper {
    private static final Gson GSON = new GsonBuilder().serializeNulls().disableHtmlEscaping().create();

    private JsonHelper() {
    }

    public static String sha256(Object value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(GSON.toJson(stabilize(value)).getBytes(StandardCharsets.UTF_8));
            StringBuilder builder = new StringBuilder();
            for (int index = 0; index < 16 && index < bytes.length; index++) {
                builder.append(String.format("%02x", bytes[index]));
            }
            return builder.toString();
        } catch (Exception exception) {
            throw new IllegalStateException("Failed to hash JSON payload", exception);
        }
    }

    @SuppressWarnings("unchecked")
    private static Object stabilize(Object value) {
        if (value instanceof Map<?, ?> rawMap) {
            Map<String, Object> sorted = new TreeMap<>();
            for (Map.Entry<?, ?> entry : rawMap.entrySet()) {
                sorted.put(String.valueOf(entry.getKey()), stabilize(entry.getValue()));
            }
            return sorted;
        }

        if (value instanceof List<?> rawList) {
            List<Object> stableList = new ArrayList<>();
            for (Object item : rawList) {
                stableList.add(stabilize(item));
            }
            return stableList;
        }

        return value;
    }
}
