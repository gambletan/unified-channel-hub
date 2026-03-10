package io.github.gambletan.unifiedchannel.adapters;

import io.github.gambletan.unifiedchannel.*;

import java.util.concurrent.CompletableFuture;

/**
 * Twilio SMS adapter stub.
 * <p>
 * TODO: Implement using Twilio Java SDK.
 * - Initialize TwilioRestClient with accountSid and authToken
 * - Send SMS via Message.creator() with from/to phone numbers and body
 * - Receive inbound SMS via webhook endpoint (POST with From, Body, etc.)
 * - Support MMS by attaching media URLs to outbound messages
 * - Use StatusCallback for delivery receipts (queued, sent, delivered, failed)
 *
 * @see <a href="https://www.twilio.com/docs/sms/api">Twilio SMS API</a>
 */
public final class TwilioSMSAdapter extends AbstractAdapter {

    private final String accountSid;
    private final String authToken;
    private final String phoneNumber;

    public TwilioSMSAdapter(String accountSid, String authToken, String phoneNumber) {
        this.accountSid = accountSid;
        this.authToken = authToken;
        this.phoneNumber = phoneNumber;
    }

    @Override public String channelId() { return "twilio_sms"; }

    @Override
    public CompletableFuture<Void> connect() {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Twilio SMS adapter not yet implemented"));
    }

    @Override
    public CompletableFuture<Void> disconnect() {
        status = ChannelStatus.disconnected(channelId());
        return CompletableFuture.completedFuture(null);
    }

    @Override
    public CompletableFuture<Void> send(OutboundMessage message) {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Twilio SMS send not yet implemented"));
    }
}
