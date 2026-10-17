package com.jari.app.updates;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class OtaProcessCoordinatorTest {
    @Test
    public void stalePageCannotConfirmANewPendingBundle() throws Exception {
        OtaProcessCoordinator coordinator = new OtaProcessCoordinator();
        coordinator.beginBoot();
        String oldPageBootId = coordinator.bootId();

        coordinator.rotateBoot();

        assertThrows(OtaException.class, () -> coordinator.assertReady(
            oldPageBootId,
            true,
            "/private/bundle-2",
            "/private/bundle-2"
        ));
    }

    @Test
    public void pendingBundleCanOnlyBeConfirmedFromItsServedPath() throws Exception {
        OtaProcessCoordinator coordinator = new OtaProcessCoordinator();
        coordinator.beginBoot();
        String bootId = coordinator.bootId();

        assertThrows(OtaException.class, () -> coordinator.assertReady(
            bootId,
            true,
            "/private/bundle-2",
            "/private/bundle-1"
        ));
        coordinator.assertReady(bootId, true, "/private/bundle-2", "/private/bundle-2");
        coordinator.assertReady(bootId, false, null, "public");
    }

    @Test
    public void destroyedWorkerCannotPublishOrOverlapANewApply() throws Exception {
        OtaProcessCoordinator coordinator = new OtaProcessCoordinator();
        coordinator.beginBoot();
        long oldPlugin = coordinator.registerPlugin();
        assertTrue(coordinator.tryBeginApply(oldPlugin));

        coordinator.destroyPlugin(oldPlugin);
        long newPlugin = coordinator.registerPlugin();

        assertFalse(coordinator.isActive(oldPlugin));
        assertThrows(OtaException.class, () -> coordinator.requireActive(oldPlugin));
        assertFalse(coordinator.tryBeginApply(newPlugin));
        coordinator.endApply(oldPlugin);
        assertTrue(coordinator.tryBeginApply(newPlugin));
    }

    @Test
    public void hostBootInvalidatesOldPluginAndRotatesPageIdentity() {
        OtaProcessCoordinator coordinator = new OtaProcessCoordinator();
        coordinator.beginBoot();
        long oldPlugin = coordinator.registerPlugin();
        String oldBootId = coordinator.bootId();

        coordinator.beginBoot();

        assertFalse(coordinator.isActive(oldPlugin));
        assertNotEquals(oldBootId, coordinator.bootId());
    }
}
