package com.jari.app;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.ServerPath;
import com.jari.app.updates.OtaFiles;
import com.jari.app.updates.OtaProcessCoordinator;
import com.jari.app.updates.OtaRuntime;
import com.jari.app.updates.OtaState;

public class MainActivity extends BridgeActivity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        registerPlugin(SecureSessionPlugin.class);
        registerPlugin(AppUpdatesPlugin.class);
        OtaFiles files = new OtaFiles(this);
        OtaState state = OtaRuntime.state(this);
        OtaProcessCoordinator coordinator = OtaRuntime.coordinator();
        OtaState.Boot boot;
        try {
            boot = coordinator.runLocked(() -> {
                coordinator.beginBoot();
                return state.prepareBoot(OtaRuntime.binaryId());
            });
        } catch (Exception impossible) {
            throw new IllegalStateException("Cannot prepare OTA boot state", impossible);
        }
        if (boot.resetStorage) files.clearAll();
        else files.delete(boot.discardPath);
        if (boot.path != null) {
            if (files.isInstalled(boot.path)) {
                bridgeBuilder.setServerPath(new ServerPath(ServerPath.PathType.BASE_PATH, boot.path));
            } else {
                files.delete(state.clearActive());
            }
        }
        super.onCreate(savedInstanceState);
    }
}
