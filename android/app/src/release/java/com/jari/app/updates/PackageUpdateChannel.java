package com.jari.app.updates;

import android.app.Activity;
import android.content.pm.InstallSourceInfo;
import android.content.pm.PackageManager;
import android.os.Build;
import androidx.activity.result.ActivityResult;
import com.getcapacitor.JSObject;
import com.getcapacitor.PluginCall;
import com.google.android.play.core.appupdate.AppUpdateManagerFactory;
import com.jari.app.AppUpdatesPlugin;
import com.jari.app.BuildConfig;
import java.util.concurrent.atomic.AtomicBoolean;

public final class PackageUpdateChannel {
    private final PlayUpdateController controller;
    private final AppUpdatesPlugin plugin;
    private final AtomicBoolean applying = new AtomicBoolean();

    public PackageUpdateChannel(AppUpdatesPlugin plugin) {
        this.plugin = plugin;
        controller = new PlayUpdateController(
            AppUpdateManagerFactory.create(plugin.getContext()),
            plugin.getActivity(),
            BuildConfig.VERSION_CODE
        );
    }

    public boolean supported() {
        return PlayInstallSourcePolicy.isPlayInstalled(installingPackageName());
    }

    public String source() {
        return supported() ? "play" : "unsupported";
    }

    public String reason() {
        return supported() ? null : "Google Play에서 설치한 앱만 Play 업데이트를 사용할 수 있어요.";
    }

    public void check(PluginCall call) {
        if (!supported()) {
            call.reject(reason());
            return;
        }
        controller.check(new PlayCallback(call, false));
    }

    public void apply(PluginCall call) {
        if (!supported()) {
            call.reject(reason());
            return;
        }
        if (!applying.compareAndSet(false, true)) {
            call.reject("업데이트를 이미 적용하고 있어요.");
            return;
        }
        controller.apply(new PlayCallback(call, true));
    }

    public void onUnknownSourcesResult(PluginCall call, ActivityResult ignored) {
        if (call != null) call.reject("Google Play 설치본에는 이 권한이 필요하지 않아요.");
    }

    public void onPackageInstallerResult(PluginCall call, ActivityResult ignored) {
        if (call != null) call.reject("Google Play 설치본에는 외부 APK 설치를 사용하지 않아요.");
    }

    public void onPlayUpdateResult(int resultCode) {
        controller.onActivityResult(resultCode);
    }

    public void onResume() {
        controller.onResume();
    }

    public void destroy() {
        applying.set(false);
        controller.destroy();
    }

    @SuppressWarnings("deprecation")
    private String installingPackageName() {
        PackageManager packages = plugin.getContext().getPackageManager();
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                InstallSourceInfo source = packages.getInstallSourceInfo(plugin.getContext().getPackageName());
                return source.getInstallingPackageName();
            }
            return packages.getInstallerPackageName(plugin.getContext().getPackageName());
        } catch (PackageManager.NameNotFoundException | SecurityException error) {
            return null;
        }
    }

    private final class PlayCallback implements PlayUpdateController.Callback {
        private final PluginCall call;
        private final boolean apply;

        PlayCallback(PluginCall call, boolean apply) {
            this.call = call;
            this.apply = apply;
        }

        @Override
        public void onCurrent() {
            call.resolve(checkResult("current", 0));
        }

        @Override
        public void onAvailable(int versionCode) {
            call.resolve(checkResult("available", versionCode));
        }

        @Override
        public void onResult(int resultCode) {
            applying.set(false);
            JSObject result = new JSObject();
            if (resultCode == Activity.RESULT_CANCELED) {
                result.put("status", "cancelled");
                call.resolve(result);
            } else if (resultCode == Activity.RESULT_OK) {
                result.put("status", "started");
                call.resolve(result);
            } else {
                call.reject("Google Play 업데이트가 완료되지 않았어요. (" + resultCode + ")");
            }
        }

        @Override
        public void onError(Exception error) {
            if (apply) applying.set(false);
            call.reject(apply
                ? "Google Play 업데이트를 시작하지 못했어요."
                : "Google Play 업데이트 정보를 확인하지 못했어요.", error);
        }
    }

    private static JSObject checkResult(String status, int versionCode) {
        JSObject result = new JSObject();
        result.put("status", status);
        if (versionCode > 0) result.put("versionCode", versionCode);
        return result;
    }
}
