package mina.integration.carpet;

import mina.context.GameContextCollector;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.Text;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import net.minecraft.world.RaycastContext;

import java.io.IOException;
import java.io.StringReader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;

public final class CarpetCapabilitySupport {
    private CarpetCapabilitySupport() {
    }

    public static BlockHitResult raycast(ServerPlayerEntity player, int reachBlocks) {
        Vec3d start = player.getEyePos();
        Vec3d end = start.add(player.getRotationVector().multiply(reachBlocks));
        return player.getEntityWorld().raycast(new RaycastContext(
                start,
                end,
                RaycastContext.ShapeType.OUTLINE,
                RaycastContext.FluidHandling.NONE,
                player
        ));
    }

    public static List<String> textLines(List<Text> lines) {
        List<String> values = new ArrayList<>();
        for (Text line : lines) {
            values.add(line.getString());
        }
        return values;
    }

    public static String joinLines(List<Text> lines) {
        return String.join("\n", textLines(lines));
    }

    @SuppressWarnings("unchecked")
    public static BlockPos parseBlockPos(Object raw) {
        if (raw instanceof Map<?, ?> map) {
            Integer x = toInt(map.get("x"));
            Integer y = toInt(map.get("y"));
            Integer z = toInt(map.get("z"));
            if (x != null && y != null && z != null) {
                return new BlockPos(x, y, z);
            }
        }

        if (raw instanceof String string) {
            String[] parts = string.trim().split("\\s+");
            if (parts.length == 3) {
                try {
                    return new BlockPos(Integer.parseInt(parts[0]), Integer.parseInt(parts[1]), Integer.parseInt(parts[2]));
                } catch (NumberFormatException ignored) {
                    return null;
                }
            }
        }

        return null;
    }

    @SuppressWarnings("unchecked")
    public static Vec3d parseVec(Object raw) {
        if (raw instanceof Map<?, ?> map) {
            Double x = toDouble(map.get("x"));
            Double y = toDouble(map.get("y"));
            Double z = toDouble(map.get("z"));
            if (x != null && y != null && z != null) {
                return new Vec3d(x, y, z);
            }
        }

        if (raw instanceof String string) {
            String[] parts = string.trim().split("\\s+");
            if (parts.length == 3) {
                try {
                    return new Vec3d(
                            Double.parseDouble(parts[0]),
                            Double.parseDouble(parts[1]),
                            Double.parseDouble(parts[2])
                    );
                } catch (NumberFormatException ignored) {
                    return null;
                }
            }
        }

        return null;
    }

    public static Map<String, Object> readPropertiesFile(Path path) {
        Properties properties = new Properties();

        try {
            properties.load(Files.newBufferedReader(path));
        } catch (IOException exception) {
            return Map.of("error", exception.getMessage());
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        for (String key : properties.stringPropertyNames()) {
            payload.put(key, properties.getProperty(key));
        }
        return payload;
    }

    private static Integer toInt(Object raw) {
        if (raw instanceof Number number) {
            return number.intValue();
        }
        return null;
    }

    private static Double toDouble(Object raw) {
        if (raw instanceof Number number) {
            return number.doubleValue();
        }
        return null;
    }
}
