package io.github.gambletan.unifiedchannel.adapters;

import io.github.gambletan.unifiedchannel.*;

import java.util.concurrent.CompletableFuture;

/**
 * Twilio Voice adapter stub.
 * <p>
 * TODO: Implement using Twilio Java SDK.
 * - Initialize TwilioRestClient with accountSid and authToken
 * - Create outbound calls via Call.creator() with TwiML callback URL
 * - Handle inbound calls via TwiML webhook endpoint
 * - Support text-to-speech (Say verb) and audio playback (Play verb)
 * - Use StatusCallback for call state tracking (ringing, in-progress, completed)
 *
 * @see <a href="https://www.twilio.com/docs/voice/api">Twilio Voice API</a>
 */
public final class TwilioVoiceAdapter extends AbstractAdapter {

    private final String accountSid;
    private final String authToken;
    private final String phoneNumber;

    public TwilioVoiceAdapter(String accountSid, String authToken, String phoneNumber) {
        this.accountSid = accountSid;
        this.authToken = authToken;
        this.phoneNumber = phoneNumber;
    }

    @Override public String channelId() { return "twilio_voice"; }

    @Override
    public CompletableFuture<Void> connect() {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Twilio Voice adapter not yet implemented"));
    }

    @Override
    public CompletableFuture<Void> disconnect() {
        status = ChannelStatus.disconnected(channelId());
        return CompletableFuture.completedFuture(null);
    }

    @Override
    public CompletableFuture<Void> send(OutboundMessage message) {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Twilio Voice send not yet implemented"));
    }
}
