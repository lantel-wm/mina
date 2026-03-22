package mina.capability;

import mina.context.EnvironmentAssessmentProvider;
import mina.context.InteractableScanProvider;
import mina.context.PlayerStateProvider;
import mina.context.RecentEventTracker;
import mina.context.RiskStateProvider;
import mina.context.SocialStateProvider;
import mina.context.ThreatAssessmentProvider;
import mina.context.WorldStateProvider;
import mina.policy.PlayerRole;
import net.minecraft.server.network.ServerPlayerEntity;

import java.util.LinkedHashMap;
import java.util.Map;

public final class ServerDirectWorldReader implements DirectWorldReader {
    private final PlayerStateProvider playerStateProvider;
    private final WorldStateProvider worldStateProvider;
    private final ThreatAssessmentProvider threatAssessmentProvider;
    private final EnvironmentAssessmentProvider environmentAssessmentProvider;
    private final InteractableScanProvider interactableScanProvider;
    private final SocialStateProvider socialStateProvider;
    private final RiskStateProvider riskStateProvider;
    private final RecentEventTracker recentEventTracker;
    private final VanillaCommandBackend vanillaCommandBackend;
    private final CarpetObservationBackend carpetObservationBackend;

    public ServerDirectWorldReader(
            PlayerStateProvider playerStateProvider,
            WorldStateProvider worldStateProvider,
            ThreatAssessmentProvider threatAssessmentProvider,
            EnvironmentAssessmentProvider environmentAssessmentProvider,
            InteractableScanProvider interactableScanProvider,
            SocialStateProvider socialStateProvider,
            RiskStateProvider riskStateProvider,
            RecentEventTracker recentEventTracker,
            VanillaCommandBackend vanillaCommandBackend,
            CarpetObservationBackend carpetObservationBackend
    ) {
        this.playerStateProvider = playerStateProvider;
        this.worldStateProvider = worldStateProvider;
        this.threatAssessmentProvider = threatAssessmentProvider;
        this.environmentAssessmentProvider = environmentAssessmentProvider;
        this.interactableScanProvider = interactableScanProvider;
        this.socialStateProvider = socialStateProvider;
        this.riskStateProvider = riskStateProvider;
        this.recentEventTracker = recentEventTracker;
        this.vanillaCommandBackend = vanillaCommandBackend;
        this.carpetObservationBackend = carpetObservationBackend;
    }

    @Override
    public Map<String, Object> readPlayerState(ServerPlayerEntity player) {
        return playerStateProvider.collect(player, PlayerRole.READ_ONLY, recentEventTracker);
    }

    @Override
    public Map<String, Object> readScene(ServerPlayerEntity player) {
        Map<String, Object> interactables = readInteractables(player);
        Map<String, Object> threats = readThreats(player);
        Map<String, Object> environment = environmentAssessmentProvider.collect(player, interactables, threats);
        Map<String, Object> social = readSocial(player);
        Map<String, Object> riskState = riskStateProvider.collect(readPlayerState(player), threats, environment, social);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("location_kind", environment.get("location_kind"));
        payload.put("biome", environment.get("biome"));
        payload.put("hostile_summary", threats);
        payload.put("hazard_summary", environment.get("hazard_summary"));
        payload.put("terrain_safety", environment.get("terrain_safety"));
        payload.put("safe_spot_summary", environment.get("safe_spot_summary"));
        payload.put("worth_alerting", environment.get("worth_alerting"));
        payload.put("risk_state", riskState);
        payload.put("environment_summary", environment.get("summary"));
        payload.put(
                "summary",
                "%s Current scene risk is %s."
                        .formatted(environment.get("summary"), riskState.get("level"))
        );
        return payload;
    }

    @Override
    public Map<String, Object> readThreats(ServerPlayerEntity player) {
        return threatAssessmentProvider.collect(player);
    }

    @Override
    public Map<String, Object> readEnvironment(ServerPlayerEntity player) {
        return environmentAssessmentProvider.collect(player, readInteractables(player), readThreats(player));
    }

    @Override
    public Map<String, Object> readInteractables(ServerPlayerEntity player) {
        return interactableScanProvider.collect(player);
    }

    @Override
    public Map<String, Object> readInventory(ServerPlayerEntity player) {
        Map<String, Object> payload = new LinkedHashMap<>(playerStateProvider.collectInventory(player));
        payload.put("main_hand", mina.context.GameContextCollector.stackMap(player.getMainHandStack()));
        payload.put("off_hand", mina.context.GameContextCollector.stackMap(player.getOffHandStack()));
        payload.put("summary", "Inventory shortages: %s".formatted(payload.get("shortages")));
        return payload;
    }

    @Override
    public Map<String, Object> readPoi(ServerPlayerEntity player, Map<String, Object> arguments) {
        Map<String, Object> payload = new LinkedHashMap<>(vanillaCommandBackend.locate(player, arguments));
        payload.put("summary", "Located nearby world points of interest.");
        return payload;
    }

    @Override
    public Map<String, Object> readSocial(ServerPlayerEntity player) {
        return socialStateProvider.collect(player, recentEventTracker);
    }

    @Override
    public Map<String, Object> readEvents(ServerPlayerEntity player) {
        return Map.of(
                "recent_events", recentEventTracker.collect(player),
                "summary", "Read recent world continuity events."
        );
    }

    @Override
    public Map<String, Object> readAmbientTechnicalState(ServerPlayerEntity player) {
        return carpetObservationBackend.ambientSnapshot(player);
    }

    public Map<String, Object> readWorldState(ServerPlayerEntity player) {
        Map<String, Object> environment = readEnvironment(player);
        return worldStateProvider.collectWorld(player, String.valueOf(environment.get("location_kind")));
    }
}
