package com.jari.app;

import androidx.activity.result.ActivityResult;
import android.os.Handler;
import android.os.Looper;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.jari.app.updates.PackageUpdateChannel;

@CapacitorPlugin(name = "AppUpdates")
public final class AppUpdatesPlugin extends Plugin {
    public static final int PLAY_UPDATE_REQUEST_CODE = 4230;
    private final Handler main = new Handler(Looper.getMainLooper());
    private PackageUpdateChannel channel;

    @Override
    public void load() {
        channel = new PackageUpdateChannel(this);
    }

    @PluginMethod
    public void getStatus(PluginCall call) {
        // Capacitor installs the new document's reply proxy after dispatching
        // its first native call. Reply on the UI queue after that dispatch ends.
        main.post(() -> {
            JSObject result = new JSObject();
            result.put("supported", channel.supported());
            result.put("source", channel.source());
            result.put("currentVersion", BuildConfig.VERSION_NAME);
            result.put("versionCode", BuildConfig.VERSION_CODE);
            String reason = channel.reason();
            if (reason != null) result.put("reason", reason);
            call.resolve(result);
        });
    }

    @PluginMethod
    public void check(PluginCall call) {
        channel.check(call);
    }

    @PluginMethod
    public void apply(PluginCall call) {
        channel.apply(call);
    }

    public void handlePlayUpdateResult(int resultCode) {
        if (channel != null) channel.onPlayUpdateResult(resultCode);
    }

    @ActivityCallback
    private void unknownSourcesResult(PluginCall call, ActivityResult result) {
        channel.onUnknownSourcesResult(call, result);
    }

    @ActivityCallback
    private void packageInstallerResult(PluginCall call, ActivityResult result) {
        channel.onPackageInstallerResult(call, result);
    }

    @Override
    protected void handleOnResume() {
        if (channel != null) channel.onResume();
    }

    @Override
    protected void handleOnDestroy() {
        if (channel != null) channel.destroy();
    }
}
