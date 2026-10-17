package com.jari.app.updates;

import java.net.HttpURLConnection;

public final class OtaCancellation {
    private boolean cancelled;
    private HttpURLConnection connection;

    public synchronized void attach(HttpURLConnection next) throws OtaException {
        if (cancelled) {
            next.disconnect();
            throw new OtaException("Update operation was cancelled");
        }
        connection = next;
    }

    public synchronized void detach(HttpURLConnection completed) {
        if (connection == completed) connection = null;
    }

    public synchronized void cancel() {
        cancelled = true;
        if (connection != null) connection.disconnect();
        connection = null;
    }

    public synchronized void throwIfCancelled() throws OtaException {
        if (cancelled || Thread.currentThread().isInterrupted()) throw new OtaException("Update operation was cancelled");
    }
}
