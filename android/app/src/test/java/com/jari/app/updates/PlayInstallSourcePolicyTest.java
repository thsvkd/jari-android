package com.jari.app.updates;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PlayInstallSourcePolicyTest {
    @Test
    public void acceptsOnlyGooglePlayAsInstallerOfRecord() {
        assertTrue(PlayInstallSourcePolicy.isPlayInstalled("com.android.vending"));
        assertFalse(PlayInstallSourcePolicy.isPlayInstalled(null));
        assertFalse(PlayInstallSourcePolicy.isPlayInstalled("com.example.installer"));
    }
}
