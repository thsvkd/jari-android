package com.jari.app;

import android.os.Handler;
import android.os.Looper;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.jari.app.updates.OtaDownload;
import com.jari.app.updates.OtaCancellation;
import com.jari.app.updates.OtaException;
import com.jari.app.updates.OtaFiles;
import com.jari.app.updates.OtaManifest;
import com.jari.app.updates.OtaManifestVerifier;
import com.jari.app.updates.OtaProcessCoordinator;
import com.jari.app.updates.OtaRuntime;
import com.jari.app.updates.OtaState;
import com.jari.app.updates.OtaUpdateDecision;
import java.io.File;
import java.net.URI;

@CapacitorPlugin(name = "AppUpdates")
public final class AppUpdatesPlugin extends Plugin {
    private static final long READY_TIMEOUT_MS = 30_000;
    private static final String BUNDLED_ASSET_PATH = "public";
    private final Handler main = new Handler(Looper.getMainLooper());
    private final OtaCancellation cancellation = new OtaCancellation();
    private volatile OtaManifest candidate;
    private long generation;
    private Runnable watchdog;

    @Override
    public void load() {
        generation = coordinator().registerPlugin();
    }

    @PluginMethod
    public void getStatus(PluginCall call) {
        JSObject result = new JSObject();
        result.put("supported", supported());
        result.put("currentVersion", state().currentVersion(BuildConfig.VERSION_NAME));
        result.put("nativeVersion", BuildConfig.VERSION_NAME);
        result.put("bootId", coordinator().bootId());
        call.resolve(result);
    }

    @PluginMethod
    public void check(PluginCall call) {
        if (!supported()) {
            candidate = null;
            call.resolve(checkResult("current", null));
            return;
        }
        candidate = null;
        executor().execute(() -> {
            try {
                coordinator().requireActive(generation);
                OtaManifest manifest = new OtaManifestVerifier(BuildConfig.JARI_OTA_PUBLIC_KEY, BuildConfig.VERSION_NAME)
                    .verify(OtaDownload.fetchText(URI.create(BuildConfig.JARI_OTA_MANIFEST_URL), cancellation));
                coordinator().requireActive(generation);
                OtaState updateState = state();
                OtaUpdateDecision decision = OtaUpdateDecision.forManifest(
                    BuildConfig.VERSION_NAME,
                    manifest.nativeVersion,
                    manifest.sequence,
                    updateState.activeSequence(),
                    updateState.highestSequence()
                );
                candidate = decision == OtaUpdateDecision.AVAILABLE ? manifest : null;
                switch (decision) {
                    case INCOMPATIBLE:
                        call.resolve(checkResult("incompatible", manifest.version));
                        break;
                    case CURRENT:
                        call.resolve(checkResult("current", null));
                        break;
                    case BLOCKED_REPLAY:
                        call.reject("이 업데이트는 이전에 적용하지 못했어요. 새 버전을 기다려 주세요.");
                        break;
                    case AVAILABLE:
                        call.resolve(checkResult("available", manifest.version));
                        break;
                }
            } catch (Exception error) {
                candidate = null;
                if (coordinator().isActive(generation)) call.reject("업데이트 정보를 확인하지 못했어요.", error);
            }
        });
    }

    @PluginMethod
    public void apply(PluginCall call) {
        OtaManifest selected = candidate;
        if (!supported() || selected == null) {
            call.reject("먼저 업데이트를 확인해 주세요.");
            return;
        }
        if (state().isPendingReady()) {
            call.reject("이전 업데이트가 시작되는 중이에요.");
            return;
        }
        if (!coordinator().tryBeginApply(generation)) {
            call.reject("업데이트를 이미 적용하고 있어요.");
            return;
        }
        executor().execute(() -> applyCandidate(call, selected));
    }

    @PluginMethod
    public void ready(PluginCall call) {
        String bootId = call.getString("bootId");
        try {
            String previous = coordinator().runLocked(() -> {
                coordinator().requireActive(generation);
                OtaState updateState = state();
                coordinator().assertReady(
                    bootId,
                    updateState.isPendingReady(),
                    updateState.activePath(),
                    getBridge().getServerBasePath()
                );
                String discard = updateState.markReady();
                cancelWatchdog();
                return discard;
            });
            files().delete(previous);
            call.resolve();
        } catch (Exception error) {
            call.reject("업데이트 상태를 저장하지 못했어요.", error);
        }
    }

    private void applyCandidate(PluginCall call, OtaManifest selected) {
        OtaFiles files = files();
        File download = null;
        String installedPath = null;
        boolean pending = false;
        try {
            download = files.downloadFile(selected.sequence);
            files.delete(download.getAbsolutePath());
            coordinator().requireActive(generation);
            String digest = OtaDownload.toFile(selected.url, download, cancellation);
            if (!selected.sha256.equals(digest)) throw new OtaException("Downloaded update digest does not match");
            coordinator().requireActive(generation);
            installedPath = files.install(download, selected);
            String pathToPublish = installedPath;
            coordinator().runLocked(() -> {
                coordinator().requireActive(generation);
                cancellation.throwIfCancelled();
                coordinator().rotateBoot();
                state().beginApply(selected.version, selected.sequence, pathToPublish);
                return null;
            });
            pending = true;
            candidate = null;
            if (coordinator().isActive(generation)) call.resolve();
            String pathForReload = installedPath;
            main.post(() -> {
                if (!coordinator().isActive(generation)) return;
                scheduleWatchdog();
                getBridge().setServerBasePath(pathForReload);
            });
        } catch (Exception error) {
            if (!pending) files.delete(installedPath);
            if (coordinator().isActive(generation)) call.reject("업데이트를 적용하지 못했어요.", error);
        } finally {
            if (download != null) files.delete(download.getAbsolutePath());
            coordinator().endApply(generation);
        }
    }

    private void scheduleWatchdog() {
        cancelWatchdog();
        watchdog = () -> {
            try {
                OtaState.Boot rollback = coordinator().runLocked(() -> {
                    coordinator().requireActive(generation);
                    if (!state().isPendingReady()) return null;
                    coordinator().rotateBoot();
                    return state().rollbackPending();
                });
                if (rollback == null) return;
                files().delete(rollback.discardPath);
                if (files().isInstalled(rollback.path)) getBridge().setServerBasePath(rollback.path);
                else {
                    files().delete(state().clearActive());
                    getBridge().setServerAssetPath(BUNDLED_ASSET_PATH);
                }
            } catch (Exception ignored) {
                // Durable pending state remains and the next cold launch rolls it back.
            }
        };
        main.postDelayed(watchdog, READY_TIMEOUT_MS);
    }

    private void cancelWatchdog() {
        if (watchdog != null) {
            main.removeCallbacks(watchdog);
            watchdog = null;
        }
    }

    private boolean supported() {
        return !BuildConfig.JARI_OTA_MANIFEST_URL.isEmpty() && !BuildConfig.JARI_OTA_PUBLIC_KEY.isEmpty();
    }

    private OtaState state() {
        return OtaRuntime.state(getContext());
    }

    private OtaFiles files() {
        return new OtaFiles(getContext());
    }

    private OtaProcessCoordinator coordinator() {
        return OtaRuntime.coordinator();
    }

    private java.util.concurrent.ExecutorService executor() {
        return OtaRuntime.executor();
    }

    private static JSObject checkResult(String status, String version) {
        JSObject result = new JSObject();
        result.put("status", status);
        if (version != null) result.put("version", version);
        return result;
    }

    @Override
    protected void handleOnDestroy() {
        coordinator().destroyPlugin(generation);
        cancellation.cancel();
        cancelWatchdog();
    }
}
