package com.jari.app.updates;

public final class PackageUpdateException extends Exception {
    public PackageUpdateException(String message) {
        super(message);
    }

    public PackageUpdateException(String message, Throwable cause) {
        super(message, cause);
    }
}
