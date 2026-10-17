package com.jari.app.updates;

import java.util.UUID;

public final class OtaProcessCoordinator {
    public interface Work<T> {
        T run() throws Exception;
    }

    private long generation;
    private long applyingGeneration;
    private String bootId = UUID.randomUUID().toString();

    public synchronized void beginBoot() {
        generation++;
        rotateBoot();
    }

    public synchronized long registerPlugin() {
        generation++;
        return generation;
    }

    public synchronized void destroyPlugin(long pluginGeneration) {
        if (generation == pluginGeneration) generation++;
    }

    public synchronized boolean isActive(long pluginGeneration) {
        return generation == pluginGeneration;
    }

    public synchronized void requireActive(long pluginGeneration) throws OtaException {
        if (!isActive(pluginGeneration)) throw new OtaException("OTA plugin instance is no longer active");
    }

    public synchronized boolean tryBeginApply(long pluginGeneration) {
        if (!isActive(pluginGeneration) || applyingGeneration != 0) return false;
        applyingGeneration = pluginGeneration;
        return true;
    }

    public synchronized void endApply(long pluginGeneration) {
        if (applyingGeneration == pluginGeneration) applyingGeneration = 0;
    }

    public synchronized String bootId() {
        return bootId;
    }

    public synchronized String rotateBoot() {
        bootId = UUID.randomUUID().toString();
        return bootId;
    }

    public synchronized void assertReady(
        String providedBootId,
        boolean pending,
        String activePath,
        String serverPath
    ) throws OtaException {
        if (providedBootId == null || !bootId.equals(providedBootId)) {
            throw new OtaException("Update readiness belongs to an old page");
        }
        if (pending && (activePath == null || !activePath.equals(serverPath))) {
            throw new OtaException("Pending update is not the page currently being served");
        }
    }

    public synchronized <T> T runLocked(Work<T> work) throws Exception {
        return work.run();
    }
}
