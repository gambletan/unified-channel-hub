package io.github.gambletan.unifiedchannel.adapters;

import io.github.gambletan.unifiedchannel.*;

import java.util.concurrent.CompletableFuture;

/**
 * Home Assistant adapter stub.
 * <p>
 * TODO: Implement using Java WebSocket API + HttpClient for REST.
 * - Connect to WebSocket at ws://{url}/api/websocket for real-time events
 * - Authenticate with long-lived access token via auth message
 * - Subscribe to state_changed events for entity updates
 * - Use java.net.http.HttpClient for REST calls (GET /api/states, POST /api/services)
 * - Call services (light.turn_on, switch.toggle, etc.) via REST or WebSocket
 *
 * @see <a href="https://developers.home-assistant.io/docs/api/rest/">Home Assistant REST API</a>
 */
public final class HomeAssistantAdapter extends AbstractAdapter {

    private final String url;
    private final String accessToken;

    public HomeAssistantAdapter(String url, String accessToken) {
        this.url = url;
        this.accessToken = accessToken;
    }

    @Override public String channelId() { return "homeassistant"; }

    @Override
    public CompletableFuture<Void> connect() {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Home Assistant adapter not yet implemented"));
    }

    @Override
    public CompletableFuture<Void> disconnect() {
        status = ChannelStatus.disconnected(channelId());
        return CompletableFuture.completedFuture(null);
    }

    @Override
    public CompletableFuture<Void> send(OutboundMessage message) {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Home Assistant send not yet implemented"));
    }
}
