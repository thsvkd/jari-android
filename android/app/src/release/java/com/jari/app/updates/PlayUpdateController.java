package com.jari.app.updates;

import android.app.Activity;
import com.google.android.play.core.appupdate.AppUpdateInfo;
import com.google.android.play.core.appupdate.AppUpdateManager;
import com.google.android.play.core.appupdate.AppUpdateOptions;
import com.google.android.play.core.install.model.AppUpdateType;
import com.google.android.play.core.install.model.UpdateAvailability;
import com.jari.app.AppUpdatesPlugin;

final class PlayUpdateController {
    interface Callback {
        void onCurrent();
        void onAvailable(int versionCode);
        void onResult(int resultCode);
        void onError(Exception error);
    }

    private static final AppUpdateOptions IMMEDIATE = AppUpdateOptions.newBuilder(AppUpdateType.IMMEDIATE).build();
    private final AppUpdateManager updates;
    private final Activity activity;
    private final int currentVersionCode;
    private volatile boolean checkedAvailable;
    private volatile boolean destroyed;
    private volatile Callback pendingCallback;

    PlayUpdateController(AppUpdateManager updates, Activity activity, int currentVersionCode) {
        this.updates = updates;
        this.activity = activity;
        this.currentVersionCode = currentVersionCode;
    }

    void check(Callback callback) {
        checkedAvailable = false;
        updates.getAppUpdateInfo()
            .addOnSuccessListener(info -> {
                if (destroyed) return;
                try {
                    if (info.updateAvailability() == UpdateAvailability.UPDATE_NOT_AVAILABLE) {
                        callback.onCurrent();
                        return;
                    }
                    if (info.updateAvailability() != UpdateAvailability.UPDATE_AVAILABLE
                        && info.updateAvailability() != UpdateAvailability.DEVELOPER_TRIGGERED_UPDATE_IN_PROGRESS) {
                        throw new PackageUpdateException("Google Play update availability is unknown");
                    }
                    if (!info.isUpdateTypeAllowed(AppUpdateType.IMMEDIATE)) {
                        throw new PackageUpdateException("Google Play immediate update is not allowed");
                    }
                    PackageUpdateDecision decision = PackageUpdateDecision.forVersionCodes(
                        currentVersionCode, info.availableVersionCode()
                    );
                    if (decision == PackageUpdateDecision.CURRENT) {
                        callback.onCurrent();
                        return;
                    }
                    checkedAvailable = true;
                    callback.onAvailable(info.availableVersionCode());
                } catch (Exception error) {
                    callback.onError(error);
                }
            })
            .addOnFailureListener(error -> {
                if (!destroyed) callback.onError(error);
            });
    }

    void apply(Callback callback) {
        if (!checkedAvailable) {
            callback.onError(new PackageUpdateException("Check for an update before applying it"));
            return;
        }
        // AppUpdateInfo is single-use. Every attempt obtains a fresh instance.
        updates.getAppUpdateInfo()
            .addOnSuccessListener(info -> {
                if (!destroyed) start(callback, info);
            })
            .addOnFailureListener(error -> {
                if (!destroyed) callback.onError(error);
            });
    }

    void onResume() {
        updates.getAppUpdateInfo().addOnSuccessListener(info -> {
            if (destroyed) return;
            if (info.updateAvailability() == UpdateAvailability.DEVELOPER_TRIGGERED_UPDATE_IN_PROGRESS) {
                try {
                    updates.startUpdateFlowForResult(
                        info, activity, IMMEDIATE, AppUpdatesPlugin.PLAY_UPDATE_REQUEST_CODE
                    );
                } catch (Exception ignored) {
                    // The next foreground entry obtains another fresh AppUpdateInfo and retries.
                }
            }
        });
    }

    private void start(Callback callback, AppUpdateInfo info) {
        if (!isAvailable(info)
            && info.updateAvailability() != UpdateAvailability.DEVELOPER_TRIGGERED_UPDATE_IN_PROGRESS) {
            callback.onError(new PackageUpdateException("No applicable Google Play update"));
            return;
        }
        checkedAvailable = false;
        pendingCallback = callback;
        try {
            boolean started = updates.startUpdateFlowForResult(
                info, activity, IMMEDIATE, AppUpdatesPlugin.PLAY_UPDATE_REQUEST_CODE
            );
            if (!started) {
                pendingCallback = null;
                callback.onError(new PackageUpdateException("Google Play update flow did not start"));
            }
        } catch (Exception error) {
            pendingCallback = null;
            callback.onError(error);
        }
    }

    private boolean isAvailable(AppUpdateInfo info) {
        return info.updateAvailability() == UpdateAvailability.UPDATE_AVAILABLE
            && info.isUpdateTypeAllowed(IMMEDIATE);
    }

    void onActivityResult(int resultCode) {
        if (destroyed) return;
        Callback callback = pendingCallback;
        pendingCallback = null;
        if (callback != null) callback.onResult(resultCode);
    }

    void destroy() {
        destroyed = true;
        checkedAvailable = false;
        pendingCallback = null;
    }
}
