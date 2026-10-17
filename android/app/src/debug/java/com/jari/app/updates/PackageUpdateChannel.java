package com.jari.app.updates;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.InstallSourceInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import androidx.activity.result.ActivityResult;
import androidx.core.content.FileProvider;
import com.getcapacitor.JSObject;
import com.getcapacitor.PluginCall;
import com.jari.app.AppUpdatesPlugin;
import com.jari.app.BuildConfig;
import java.io.File;
import java.net.URI;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public final class PackageUpdateChannel {
    private static final String MIME_APK = "application/vnd.android.package-archive";
    private final AppUpdatesPlugin plugin;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final AtomicBoolean applying = new AtomicBoolean();
    private final Object lifecycleLock = new Object();
    private final PackageFiles files;
    private boolean destroyed;
    private volatile PackageManifest candidate;
    private volatile PackageCancellation cancellation;
    private volatile File pendingApk;

    public PackageUpdateChannel(AppUpdatesPlugin plugin) {
        this.plugin = plugin;
        files = new PackageFiles(plugin.getContext());
    }

    public boolean supported() {
        return UpdateSourcePolicy.APK.equals(source());
    }

    public String source() {
        return UpdateSourcePolicy.forDebug(installingPackageName(), configured());
    }

    public String reason() {
        if (supported()) return null;
        if ("com.android.vending".equals(installingPackageName())) {
            return "Google Play 설치본은 Play용 앱에서 업데이트할 수 있어요.";
        }
        return "직접 설치용 업데이트 서버가 설정되지 않았어요.";
    }

    public void check(PluginCall call) {
        if (!supported()) {
            candidate = null;
            call.reject(reason());
            return;
        }
        candidate = null;
        executor.execute(() -> {
            PackageCancellation operation = new PackageCancellation();
            cancellation = operation;
            try {
                PackageManifest manifest = new PackageManifestVerifier(
                    BuildConfig.JARI_APK_UPDATE_PUBLIC_KEY,
                    plugin.getContext().getPackageName()
                ).verify(PackageDownload.fetchText(URI.create(BuildConfig.JARI_APK_UPDATE_MANIFEST_URL), operation));
                PackageUpdateDecision decision = PackageUpdateDecision.forVersionCodes(
                    BuildConfig.VERSION_CODE, manifest.versionCode
                );
                if (decision == PackageUpdateDecision.CURRENT) {
                    synchronized (lifecycleLock) {
                        if (!destroyed) call.resolve(checkResult("current", null));
                    }
                } else {
                    synchronized (lifecycleLock) {
                        if (destroyed) return;
                        candidate = manifest;
                        call.resolve(checkResult("available", manifest));
                    }
                }
            } catch (Exception error) {
                candidate = null;
                synchronized (lifecycleLock) {
                    if (!destroyed) call.reject("업데이트 정보를 확인하지 못했어요.", error);
                }
            } finally {
                if (cancellation == operation) cancellation = null;
            }
        });
    }

    public void apply(PluginCall call) {
        if (!isActive()) {
            call.reject("업데이트 기능을 사용할 수 없어요.");
            return;
        }
        PackageManifest selected = candidate;
        if (!supported() || selected == null) {
            call.reject("먼저 업데이트를 확인해 주세요.");
            return;
        }
        if (!applying.compareAndSet(false, true)) {
            call.reject("업데이트를 이미 적용하고 있어요.");
            return;
        }
        candidate = null;
        executor.execute(() -> prepareInstaller(call, selected));
    }

    public void onUnknownSourcesResult(PluginCall call, ActivityResult ignored) {
        if (call == null || !applying.get() || pendingApk == null) {
            finishWithoutReply();
            return;
        }
        if (!canInstallPackages()) {
            finish(call, "cancelled", null);
            return;
        }
        launchInstaller(call);
    }

    public void onPackageInstallerResult(PluginCall call, ActivityResult result) {
        if (call == null) {
            finishWithoutReply();
            return;
        }
        if (result.getResultCode() == Activity.RESULT_CANCELED) {
            finish(call, "cancelled", null);
        } else if (result.getResultCode() == Activity.RESULT_OK) {
            finish(call, "started", null);
        } else {
            finish(call, null, new PackageUpdateException("Package installer returned " + result.getResultCode()));
        }
    }

    public void onPlayUpdateResult(int ignored) {}

    public void onResume() {}

    public void destroy() {
        PackageCancellation active;
        synchronized (lifecycleLock) {
            destroyed = true;
            candidate = null;
            pendingApk = null;
            applying.set(false);
            active = cancellation;
        }
        if (active != null) active.cancel();
        executor.shutdownNow();
        files.clear();
    }

    private void prepareInstaller(PluginCall call, PackageManifest selected) {
        PackageCancellation operation = new PackageCancellation();
        cancellation = operation;
        try {
            File apk = files.prepareApk();
            String digest = PackageDownload.toFile(selected.url, apk, operation);
            if (!selected.sha256.equals(digest)) {
                throw new PackageUpdateException("Downloaded APK digest does not match the manifest");
            }
            ApkPackageVerifier.verify(plugin.getContext(), apk, selected);
            synchronized (lifecycleLock) {
                if (destroyed) {
                    files.clear();
                    return;
                }
                pendingApk = apk;
            }
            plugin.getActivity().runOnUiThread(() -> {
                if (!isActive() || pendingApk != apk) return;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !canInstallPackages()) {
                    launchUnknownSourcesSettings(call);
                } else {
                    launchInstaller(call);
                }
            });
        } catch (Exception error) {
            finish(call, null, error);
        } finally {
            if (cancellation == operation) cancellation = null;
        }
    }

    private void launchUnknownSourcesSettings(PluginCall call) {
        try {
            Intent intent = new Intent(
                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:" + plugin.getContext().getPackageName())
            );
            plugin.startActivityForResult(call, intent, "unknownSourcesResult");
        } catch (Exception error) {
            finish(call, null, error);
        }
    }

    private void launchInstaller(PluginCall call) {
        File apk = pendingApk;
        if (apk == null) {
            finish(call, null, new PackageUpdateException("Verified APK is no longer available"));
            return;
        }
        try {
            Uri uri = FileProvider.getUriForFile(
                plugin.getContext(),
                plugin.getContext().getPackageName() + ".packageupdates",
                apk
            );
            Intent intent = new Intent(Intent.ACTION_INSTALL_PACKAGE)
                .setDataAndType(uri, MIME_APK)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                .putExtra(Intent.EXTRA_RETURN_RESULT, true);
            plugin.startActivityForResult(call, intent, "packageInstallerResult");
        } catch (Exception error) {
            finish(call, null, error);
        }
    }

    private boolean canInstallPackages() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.O
            || plugin.getContext().getPackageManager().canRequestPackageInstalls();
    }

    private boolean configured() {
        return !BuildConfig.JARI_APK_UPDATE_MANIFEST_URL.isEmpty()
            && !BuildConfig.JARI_APK_UPDATE_PUBLIC_KEY.isEmpty();
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

    private void finish(PluginCall call, String status, Exception error) {
        files.clear();
        synchronized (lifecycleLock) {
            pendingApk = null;
            applying.set(false);
            if (destroyed) return;
            if (error != null) {
                call.reject("업데이트를 적용하지 못했어요.", error);
                return;
            }
            JSObject result = new JSObject();
            result.put("status", status);
            call.resolve(result);
        }
    }

    private void finishWithoutReply() {
        synchronized (lifecycleLock) {
            pendingApk = null;
            applying.set(false);
        }
        files.clear();
    }

    private boolean isActive() {
        synchronized (lifecycleLock) {
            return !destroyed;
        }
    }

    private static JSObject checkResult(String status, PackageManifest manifest) {
        JSObject result = new JSObject();
        result.put("status", status);
        if (manifest != null) {
            result.put("version", manifest.version);
            result.put("versionCode", manifest.versionCode);
        }
        return result;
    }
}
