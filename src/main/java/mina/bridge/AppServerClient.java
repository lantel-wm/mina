package mina.bridge;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import mina.config.MinaConfig;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicLong;

public final class AppServerClient implements AutoCloseable {
    private final MinaConfig config;
    private final HttpClient httpClient;
    private final Gson gson;
    private final AtomicLong nextId = new AtomicLong(1L);
    private final ConcurrentMap<Long, CompletableFuture<JsonObject>> pendingRequests = new ConcurrentHashMap<>();
    private final ConcurrentMap<String, TurnStream> turnStreams = new ConcurrentHashMap<>();
    private volatile WebSocket webSocket;
    private volatile boolean initialized;

    public AppServerClient(MinaConfig config) {
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

    public synchronized void ensureConnected() throws IOException, InterruptedException {
        if (webSocket != null) {
            return;
        }
        try {
            webSocket = httpClient.newWebSocketBuilder()
                    .connectTimeout(config.connectTimeout())
                    .buildAsync(websocketUri(), new Listener())
                    .get(config.requestTimeout().toMillis(), TimeUnit.MILLISECONDS);
        } catch (ExecutionException exception) {
            throw new IOException("Failed to connect to Mina app-server websocket", exception.getCause());
        } catch (TimeoutException exception) {
            throw new IOException("Timed out while connecting to Mina app-server websocket", exception);
        }
        if (!initialized) {
            sendRequest("initialize", Map.of());
            initialized = true;
        }
    }

    public void ensureThread(String threadId, String playerUuid, String playerName) throws IOException, InterruptedException {
        ensureConnected();
        try {
            AppServerModels.ThreadResumeParams params = new AppServerModels.ThreadResumeParams();
            params.thread_id = threadId;
            sendRequest("thread/resume", params);
        } catch (IOException exception) {
            AppServerModels.ThreadStartParams params = new AppServerModels.ThreadStartParams();
            params.thread_id = threadId;
            params.player_uuid = playerUuid;
            params.player_name = playerName;
            params.metadata = Map.of("source", "minecraft_mod");
            sendRequest("thread/start", params);
        }
    }

    public JsonObject unsubscribeThread(String threadId) throws IOException, InterruptedException {
        AppServerModels.ThreadUnsubscribeParams params = new AppServerModels.ThreadUnsubscribeParams();
        params.thread_id = threadId;
        return sendRequest("thread/unsubscribe", params);
    }

    public JsonObject updateThreadMetadata(String threadId, Map<String, Object> metadata) throws IOException, InterruptedException {
        AppServerModels.ThreadMetadataUpdateParams params = new AppServerModels.ThreadMetadataUpdateParams();
        params.thread_id = threadId;
        params.metadata = metadata;
        return sendRequest("thread/metadata/update", params);
    }

    public JsonObject shellCommand(String threadId, String command) throws IOException, InterruptedException {
        AppServerModels.ThreadShellCommandParams params = new AppServerModels.ThreadShellCommandParams();
        params.thread_id = threadId;
        params.command = command;
        return sendRequest("thread/shellCommand", params);
    }

    public TurnStream startTurn(AppServerModels.TurnStartParams params) throws IOException, InterruptedException {
        ensureConnected();
        TurnStream stream = new TurnStream(params.turn_id, this);
        if (turnStreams.putIfAbsent(params.turn_id, stream) != null) {
            throw new IOException("Turn already has an active stream: " + params.turn_id);
        }
        try {
            sendRequest("turn/start", params);
            return stream;
        } catch (IOException | InterruptedException exception) {
            turnStreams.remove(params.turn_id);
            throw exception;
        }
    }

    public JsonObject rollbackThread(AppServerModels.ThreadRollbackParams params) throws IOException, InterruptedException {
        return sendRequest("thread/rollback", params);
    }

    public JsonObject steerTurn(AppServerModels.TurnSteerParams params) throws IOException, InterruptedException {
        return sendRequest("turn/steer", params);
    }

    public JsonObject execCommand(AppServerModels.CommandExecParams params) throws IOException, InterruptedException {
        return sendRequest("command/exec", params);
    }

    public JsonObject execCommandWrite(AppServerModels.CommandExecWriteParams params) throws IOException, InterruptedException {
        return sendRequest("command/exec/write", params);
    }

    public JsonObject execCommandResize(AppServerModels.CommandExecResizeParams params) throws IOException, InterruptedException {
        return sendRequest("command/exec/resize", params);
    }

    public JsonObject execCommandTerminate(AppServerModels.CommandExecTerminateParams params) throws IOException, InterruptedException {
        return sendRequest("command/exec/terminate", params);
    }

    public void sendToolResult(AppServerModels.ToolResultParams params) throws IOException, InterruptedException {
        sendRequest("tool/result", params);
    }

    public void sendApprovalResponse(AppServerModels.ApprovalResponseParams params) throws IOException, InterruptedException {
        sendRequest("approval/respond", params);
    }

    public void interruptTurn(String threadId, String turnId) throws IOException, InterruptedException {
        sendRequest("turn/interrupt", Map.of("thread_id", threadId, "turn_id", turnId));
    }

    void closeTurnStream(String turnId) {
        turnStreams.remove(turnId);
    }

    @Override
    public synchronized void close() {
        if (webSocket != null) {
            webSocket.sendClose(WebSocket.NORMAL_CLOSURE, "shutdown");
            webSocket = null;
        }
        initialized = false;
        pendingRequests.clear();
        turnStreams.clear();
    }

    private URI websocketUri() {
        URI base = config.agentBaseUrl();
        String scheme = "https".equalsIgnoreCase(base.getScheme()) ? "wss" : "ws";
        String path = base.getPath() == null ? "" : base.getPath();
        if (path.endsWith("/")) {
            path = path.substring(0, path.length() - 1);
        }
        return URI.create(scheme + "://" + base.getHost() + ":" + base.getPort() + path + "/v1/app-server/ws");
    }

    private JsonObject sendRequest(String method, Object params) throws IOException, InterruptedException {
        ensureConnected();
        long id = nextId.getAndIncrement();
        CompletableFuture<JsonObject> future = new CompletableFuture<>();
        pendingRequests.put(id, future);
        JsonObject request = new JsonObject();
        request.addProperty("jsonrpc", "2.0");
        request.addProperty("id", id);
        request.addProperty("method", method);
        request.add("params", gson.toJsonTree(params));
        webSocket.sendText(gson.toJson(request), true);
        try {
            JsonObject response = future.get(config.requestTimeout().toMillis(), TimeUnit.MILLISECONDS);
            if (response.has("error") && response.get("error").isJsonObject()) {
                JsonObject error = response.getAsJsonObject("error");
                throw new IOException(error.has("message") ? error.get("message").getAsString() : "Unknown JSON-RPC error");
            }
            if (response.has("result") && response.get("result").isJsonObject()) {
                return response.getAsJsonObject("result");
            }
            return new JsonObject();
        } catch (ExecutionException exception) {
            throw new IOException("JSON-RPC request failed for method " + method, exception.getCause());
        } catch (TimeoutException exception) {
            throw new IOException("Timed out waiting for JSON-RPC response to " + method, exception);
        } finally {
            pendingRequests.remove(id);
        }
    }

    private void dispatchNotification(String method, JsonObject params) {
        String turnId = extractTurnId(params);
        if (turnId == null) {
            return;
        }
        TurnStream stream = turnStreams.get(turnId);
        if (stream == null) {
            return;
        }
        stream.enqueue(new AppServerEvent(method, params));
        if (Objects.equals(method, "turn/completed") || Objects.equals(method, "turn/failed")) {
            stream.markTerminal();
        }
    }

    private String extractTurnId(JsonObject params) {
        if (params == null) {
            return null;
        }
        if (params.has("turn_id")) {
            return params.get("turn_id").getAsString();
        }
        if (params.has("turn") && params.get("turn").isJsonObject()) {
            JsonObject turn = params.getAsJsonObject("turn");
            if (turn.has("turn_id")) {
                return turn.get("turn_id").getAsString();
            }
        }
        if (params.has("tool_call") && params.get("tool_call").isJsonObject()) {
            JsonObject toolCall = params.getAsJsonObject("tool_call");
            if (toolCall.has("turn_id")) {
                return toolCall.get("turn_id").getAsString();
            }
        }
        return null;
    }

    public record AppServerEvent(String method, JsonObject params) {
    }

    public static final class TurnStream {
        private final String turnId;
        private final AppServerClient client;
        private final BlockingQueue<AppServerEvent> events = new LinkedBlockingQueue<>();
        private volatile boolean terminal;

        private TurnStream(String turnId, AppServerClient client) {
            this.turnId = turnId;
            this.client = client;
        }

        void enqueue(AppServerEvent event) {
            events.offer(event);
        }

        void markTerminal() {
            terminal = true;
        }

        public AppServerEvent take(Duration timeout) throws InterruptedException, TimeoutException {
            AppServerEvent event = events.poll(timeout.toMillis(), TimeUnit.MILLISECONDS);
            if (event == null) {
                throw new TimeoutException("Timed out waiting for app-server turn event.");
            }
            return event;
        }

        public boolean isTerminal() {
            return terminal && events.isEmpty();
        }

        public void sendToolResult(AppServerModels.ToolResultParams params) throws IOException, InterruptedException {
            client.sendToolResult(params);
        }

        public void sendApprovalResponse(AppServerModels.ApprovalResponseParams params) throws IOException, InterruptedException {
            client.sendApprovalResponse(params);
        }

        public JsonObject steer(AppServerModels.TurnSteerParams params) throws IOException, InterruptedException {
            return client.steerTurn(params);
        }

        public void close() {
            client.closeTurnStream(turnId);
        }
    }

    private final class Listener implements WebSocket.Listener {
        private final StringBuilder textBuffer = new StringBuilder();

        @Override
        public void onOpen(WebSocket webSocket) {
            WebSocket.Listener.super.onOpen(webSocket);
            webSocket.request(1);
        }

        @Override
        public CompletableFuture<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            textBuffer.append(data);
            if (last) {
                handleMessage(textBuffer.toString());
                textBuffer.setLength(0);
            }
            webSocket.request(1);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletableFuture<?> onBinary(WebSocket webSocket, ByteBuffer data, boolean last) {
            webSocket.request(1);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletableFuture<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            pendingRequests.forEach((id, future) -> future.completeExceptionally(new IOException("WebSocket closed: " + reason)));
            pendingRequests.clear();
            turnStreams.clear();
            AppServerClient.this.webSocket = null;
            AppServerClient.this.initialized = false;
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            pendingRequests.forEach((id, future) -> future.completeExceptionally(error));
            pendingRequests.clear();
        }

        private void handleMessage(String rawMessage) {
            JsonObject payload = JsonParser.parseString(rawMessage).getAsJsonObject();
            if (payload.has("id")) {
                JsonElement idElement = payload.get("id");
                if (idElement != null && idElement.isJsonPrimitive() && idElement.getAsJsonPrimitive().isNumber()) {
                    long id = idElement.getAsLong();
                    CompletableFuture<JsonObject> future = pendingRequests.get(id);
                    if (future != null) {
                        future.complete(payload);
                    }
                }
                return;
            }
            if (payload.has("method") && payload.get("method").isJsonPrimitive()) {
                String method = payload.get("method").getAsString();
                JsonObject params = payload.has("params") && payload.get("params").isJsonObject()
                        ? payload.getAsJsonObject("params")
                        : new JsonObject();
                dispatchNotification(method, params);
            }
        }
    }
}
