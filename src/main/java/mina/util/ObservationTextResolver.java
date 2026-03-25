package mina.util;

import com.google.gson.Gson;
import com.google.gson.reflect.TypeToken;
import mina.MinaMod;
import net.minecraft.block.BlockState;
import net.minecraft.entity.Entity;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;

import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.Reader;
import java.lang.reflect.Type;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

public final class ObservationTextResolver {
    private static final Gson GSON = new Gson();
    private static final Type MAP_TYPE = new TypeToken<Map<String, String>>() {}.getType();

    private final String language;
    private final Map<String, String> translations;

    public ObservationTextResolver(String requestedLanguage) {
        this.language = normalizeLanguage(requestedLanguage);
        this.translations = loadTranslations(this.language);
    }

    public String language() {
        return language;
    }

    public String blockName(BlockState blockState) {
        return translate(blockState.getBlock().getTranslationKey(), Registries.BLOCK.getId(blockState.getBlock()).toString());
    }

    public String itemName(ItemStack stack) {
        if (stack == null || stack.isEmpty()) {
            return null;
        }
        return translate(stack.getItem().getTranslationKey(), Registries.ITEM.getId(stack.getItem()).toString());
    }

    public String entityName(Entity entity) {
        if (entity instanceof PlayerEntity) {
            return entity.getName().getString();
        }
        return entityTypeName(entity.getType());
    }

    public String entityTypeName(EntityType<?> entityType) {
        return translate(entityType.getTranslationKey(), Registries.ENTITY_TYPE.getId(entityType).toString());
    }

    public String translationKeyName(String translationKey, String fallbackId) {
        return translate(translationKey, fallbackId);
    }

    private String translate(String translationKey, String fallbackId) {
        String translated = translations.get(translationKey);
        if (translated != null && !translated.isBlank()) {
            return translated;
        }
        return fallbackLabel(fallbackId);
    }

    private static String normalizeLanguage(String requestedLanguage) {
        String normalized = requestedLanguage == null ? "" : requestedLanguage.trim().toLowerCase(Locale.ROOT);
        return normalized.isBlank() ? "en_us" : normalized;
    }

    private static Map<String, String> loadTranslations(String language) {
        String resourcePath = "mina/lang/observation_" + language + ".json";
        InputStream stream = MinaMod.class.getClassLoader().getResourceAsStream(resourcePath);
        if (stream == null && !"en_us".equals(language)) {
            MinaMod.LOGGER.warn("Observation language {} not bundled. Falling back to en_us.", language);
            stream = MinaMod.class.getClassLoader().getResourceAsStream("mina/lang/observation_en_us.json");
        }
        if (stream == null) {
            throw new IllegalStateException("Missing bundled observation language resource for " + language);
        }
        try (Reader reader = new InputStreamReader(stream, StandardCharsets.UTF_8)) {
            Map<String, String> parsed = GSON.fromJson(reader, MAP_TYPE);
            return parsed == null ? Map.of() : Map.copyOf(new LinkedHashMap<>(parsed));
        } catch (Exception exception) {
            throw new IllegalStateException("Failed to load bundled observation language resource for " + language, exception);
        }
    }

    private static String fallbackLabel(String fallbackId) {
        String source = fallbackId == null ? "" : fallbackId;
        String path = source.contains(":") ? source.substring(source.indexOf(':') + 1) : source;
        if (path.isBlank()) {
            return "Unknown";
        }
        String[] parts = path.split("[_./]+");
        StringBuilder builder = new StringBuilder();
        for (String part : parts) {
            if (part == null || part.isBlank()) {
                continue;
            }
            if (builder.length() > 0) {
                builder.append(' ');
            }
            builder.append(Character.toUpperCase(part.charAt(0)));
            if (part.length() > 1) {
                builder.append(part.substring(1));
            }
        }
        return builder.length() == 0 ? "Unknown" : builder.toString();
    }
}
