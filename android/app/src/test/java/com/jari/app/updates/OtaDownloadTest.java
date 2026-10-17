package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.net.HttpURLConnection;
import java.net.URL;
import org.junit.Test;

public class OtaDownloadTest {
    @Test
    public void copiesWithinLimitAndReturnsLowercaseSha256() throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();

        String digest = OtaDownload.copyAndDigest(new ByteArrayInputStream("hello".getBytes(StandardCharsets.UTF_8)), output, 5);

        assertEquals("hello", output.toString("UTF-8"));
        assertEquals("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", digest);
    }

    @Test
    public void rejectsOneBytePastDownloadLimit() {
        assertThrows(OtaException.class, () -> OtaDownload.copyAndDigest(
            new ByteArrayInputStream("123456".getBytes(StandardCharsets.UTF_8)),
            new ByteArrayOutputStream(),
            5
        ));
    }

    @Test
    public void cancellationDisconnectsAnActiveRequest() throws Exception {
        FakeConnection connection = new FakeConnection();
        OtaCancellation cancellation = new OtaCancellation();
        cancellation.attach(connection);

        cancellation.cancel();

        assertEquals(true, connection.disconnected);
        assertThrows(OtaException.class, cancellation::throwIfCancelled);
    }

    private static final class FakeConnection extends HttpURLConnection {
        boolean disconnected;

        FakeConnection() throws Exception {
            super(new URL("https://updates.example/test"));
        }

        @Override public void disconnect() { disconnected = true; }
        @Override public boolean usingProxy() { return false; }
        @Override public void connect() {}
    }
}
