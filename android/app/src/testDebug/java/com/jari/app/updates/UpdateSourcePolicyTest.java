package com.jari.app.updates;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class UpdateSourcePolicyTest {
    @Test
    public void aPlayInstalledDebugBinaryNeverUsesTheExternalApkFeed() {
        assertEquals(UpdateSourcePolicy.UNSUPPORTED, UpdateSourcePolicy.forDebug("com.android.vending", true));
    }

    @Test
    public void aConfiguredDirectInstallUsesTheApkFeed() {
        assertEquals(UpdateSourcePolicy.APK, UpdateSourcePolicy.forDebug(null, true));
        assertEquals(UpdateSourcePolicy.APK, UpdateSourcePolicy.forDebug("com.android.packageinstaller", true));
    }

    @Test
    public void anUnconfiguredDirectInstallIsUnsupported() {
        assertEquals(UpdateSourcePolicy.UNSUPPORTED, UpdateSourcePolicy.forDebug(null, false));
    }
}
