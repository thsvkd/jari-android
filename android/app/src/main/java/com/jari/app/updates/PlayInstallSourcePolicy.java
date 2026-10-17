package com.jari.app.updates;

public final class PlayInstallSourcePolicy {
    private static final String PLAY_STORE = "com.android.vending";

    private PlayInstallSourcePolicy() {}

    public static boolean isPlayInstalled(String installingPackageName) {
        return PLAY_STORE.equals(installingPackageName);
    }
}
