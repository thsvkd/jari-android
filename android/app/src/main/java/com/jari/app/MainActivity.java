package com.jari.app;

import android.content.Intent;
import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.PluginHandle;
import com.jari.app.updates.LegacyOtaCleanup;

public class MainActivity extends BridgeActivity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        registerPlugin(SecureSessionPlugin.class);
        registerPlugin(AppUpdatesPlugin.class);
        LegacyOtaCleanup.run(this);
        super.onCreate(savedInstanceState);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == AppUpdatesPlugin.PLAY_UPDATE_REQUEST_CODE && getBridge() != null) {
            PluginHandle handle = getBridge().getPlugin("AppUpdates");
            if (handle != null && handle.getInstance() instanceof AppUpdatesPlugin) {
                ((AppUpdatesPlugin) handle.getInstance()).handlePlayUpdateResult(resultCode);
                return;
            }
        }
        super.onActivityResult(requestCode, resultCode, data);
    }
}
