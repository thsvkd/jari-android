package com.jari.app.updates;

import android.content.Context;
import com.getcapacitor.plugin.WebView;
import java.io.File;

public final class LegacyOtaCleanup {
    private static final String LEGACY_PREFERENCES = "jari_app_updates";

    private LegacyOtaCleanup() {}

    public static void run(Context context) {
        context.getSharedPreferences(LEGACY_PREFERENCES, Context.MODE_PRIVATE).edit().clear().apply();
        context.getSharedPreferences(WebView.WEBVIEW_PREFS_NAME, Context.MODE_PRIVATE)
            .edit().remove(WebView.CAP_SERVER_PATH).apply();
        deleteRecursively(new File(context.getFilesDir(), "app-updates"));
    }

    private static void deleteRecursively(File target) {
        if (target == null || !target.exists()) return;
        File[] children = target.listFiles();
        if (children != null) for (File child : children) deleteRecursively(child);
        if (!target.delete() && target.exists()) target.deleteOnExit();
    }
}
