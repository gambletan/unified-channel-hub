package io.github.gambletan.unifiedchannel.adapters;

import io.github.gambletan.unifiedchannel.*;

import java.util.concurrent.CompletableFuture;

/**
 * Email adapter stub.
 * <p>
 * TODO: Implement using Jakarta Mail (javax.mail).
 * - Use IMAP IDLE for real-time inbox polling (or periodic IMAP fetch)
 * - Connect to IMAP host for receiving, SMTP host for sending
 * - Authenticate with email/password (or OAuth2 for Gmail/Outlook)
 * - Parse MIME messages into InboundMessage (handle multipart, attachments)
 * - Send via SMTP with TLS, supporting HTML body and attachments
 *
 * @see <a href="https://jakartaee.github.io/mail-api/">Jakarta Mail API</a>
 */
public final class EmailAdapter extends AbstractAdapter {

    private final String emailAddress;
    private final String password;
    private final String imapHost;
    private final String smtpHost;

    public EmailAdapter(String emailAddress, String password, String imapHost, String smtpHost) {
        this.emailAddress = emailAddress;
        this.password = password;
        this.imapHost = imapHost;
        this.smtpHost = smtpHost;
    }

    @Override public String channelId() { return "email"; }

    @Override
    public CompletableFuture<Void> connect() {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Email adapter not yet implemented"));
    }

    @Override
    public CompletableFuture<Void> disconnect() {
        status = ChannelStatus.disconnected(channelId());
        return CompletableFuture.completedFuture(null);
    }

    @Override
    public CompletableFuture<Void> send(OutboundMessage message) {
        return CompletableFuture.failedFuture(new UnsupportedOperationException("Email send not yet implemented"));
    }
}
