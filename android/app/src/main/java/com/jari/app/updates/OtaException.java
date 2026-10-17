package com.jari.app.updates;

public final class OtaException extends Exception {
    public OtaException(String message) {
        super(message);
    }

    public OtaException(String message, Throwable cause) {
        super(message, cause);
    }
}
