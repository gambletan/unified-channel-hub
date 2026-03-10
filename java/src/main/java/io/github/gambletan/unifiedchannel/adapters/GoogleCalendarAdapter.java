package io.github.gambletan.unifiedchannel.adapters;

import io.github.gambletan.unifiedchannel.*;

import java.util.concurrent.CompletableFuture;

/**
 * Google Calendar adapter stub.
 * <p>
 * TODO: Implement using Google API Client Library for Java.
 * - Load service account or OAuth2 credentials from credentialsPath
 * - Build Calendar service via Calendar.Builder with HttpTransport and JsonFactory
 * - Poll for upcoming events using Events.list() with timeMin/timeMax
 * - Create events via Events.insert() with summary, description, start/end times
 * - Use push notifications (watch) for real-time event change callbacks
 *
 * @see <a href="https://developers.google.com/calendar/api/guides/overview">Google Calendar API</a>
 */
public final class GoogleCalendarAdapter extends AbstractAdapter {

    private final String credentialsPath;
    private final String calendarId;

    public GoogleCalendarAdapter(String credentialsPath, String calendarId) {
        this.credentialsPath = credentialsPath;
        this.calendarId = calendarId;
    }

    @Override public String channelId() { return "google_calendar"; }

    @Override
    public CompletableFuture<Void> connect() {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Google Calendar adapter not yet implemented"));
    }

    @Override
    public CompletableFuture<Void> disconnect() {
        status = ChannelStatus.disconnected(channelId());
        return CompletableFuture.completedFuture(null);
    }

    @Override
    public CompletableFuture<Void> send(OutboundMessage message) {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Google Calendar send not yet implemented"));
    }
}
