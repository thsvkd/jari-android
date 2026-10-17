package com.jari.app.updates;

import android.content.Context;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class OtaRuntime {
    public static final String PREFERENCES = "jari_app_updates";
    private static volatile OtaState instance;
    private static final OtaProcessCoordinator COORDINATOR = new OtaProcessCoordinator();
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    private OtaRuntime() {}

    public static OtaState state(Context context) {
        OtaState current = instance;
        if (current != null) return current;
        synchronized (OtaRuntime.class) {
            if (instance == null) {
                Context application = context.getApplicationContext();
                instance = new OtaState(new SharedPreferencesValues(
                    application.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
                ));
            }
            return instance;
        }
    }

    public static String binaryId() {
        return com.jari.app.BuildConfig.VERSION_CODE + ":" + com.jari.app.BuildConfig.VERSION_NAME;
    }

    public static OtaProcessCoordinator coordinator() {
        return COORDINATOR;
    }

    public static ExecutorService executor() {
        return EXECUTOR;
    }
}
