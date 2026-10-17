package com.jari.app.updates;

public final class UpdateSourcePolicy {
    public static final String APK = "apk";
    public static final String UNSUPPORTED = "unsupported";
    private static final String PLAY_STORE = "com.android.vending";

    private UpdateSourcePolicy() {}

    public static String forDebug(String installingPackageName, boolean configured) {
        if (PLAY_STORE.equals(installingPackageName) || !configured) return UNSUPPORTED;
        return APK;
    }
}
