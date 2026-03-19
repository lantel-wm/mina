package mina.bridge;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import mina.config.MinaConfig;

import java.io.IOException;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

public final class AgentServiceClient {
    private final MinaConfig config;
    private final HttpClient httpClient;
    private final Gson gson;

    public AgentServiceClient(MinaConfig config) {
        this.config = config;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(config.connectTimeout())
                .version(HttpClient.Version.HTTP_1_1)
                .build();
        this.gson = new GsonBuilder()
                .serializeNulls()
                .disableHtmlEscaping()
                .create();
    }

    public BridgeModels.TurnResponse startTurn(BridgeModels.TurnStartRequest requestBody) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(config.agentBaseUrl().resolve("/v1/agent/turns"))
                .timeout(config.requestTimeout())
                .version(HttpClient.Version.HTTP_1_1)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(gson.toJson(requestBody)))
                .build();

        return send(request);
    }

    public BridgeModels.TurnResponse resumeTurn(String continuationId, BridgeModels.TurnResumeRequest requestBody) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(config.agentBaseUrl().resolve("/v1/agent/turns/" + continuationId + "/resume"))
                .timeout(config.requestTimeout())
                .version(HttpClient.Version.HTTP_1_1)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(gson.toJson(requestBody)))
                .build();

        return send(request);
    }

    private BridgeModels.TurnResponse send(HttpRequest request) throws IOException, InterruptedException {
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

        if (response.statusCode() >= 400) {
            throw new IOException("Agent service request failed with status " + response.statusCode() + ": " + response.body());
        }

        return gson.fromJson(response.body(), BridgeModels.TurnResponse.class);
    }
}
