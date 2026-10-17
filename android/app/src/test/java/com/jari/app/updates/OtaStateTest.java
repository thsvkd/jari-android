package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.HashMap;
import java.util.Map;
import org.junit.Test;

public class OtaStateTest {
    @Test
    public void aChangedApkClearsOtaAndReturnsToBundledAssets() {
        MemoryValues values = activeState();
        OtaState state = new OtaState(values);

        OtaState.Boot boot = state.prepareBoot("42200:4.22.0");

        assertNull(boot.path);
        assertEquals("4.22.0", state.currentVersion("4.22.0"));
        assertFalse(values.contains("activePath"));
        assertEquals(0L, state.highestSequence());
    }

    @Test
    public void aColdLaunchRollsBackAnUnconfirmedBundleAndKeepsReplayFloor() {
        MemoryValues values = activeState();
        values.putBoolean("pendingReady", true);
        values.putString("previousVersion", "4.21.0-web.22");
        values.putString("previousPath", "/private/bundle-22");
        values.putLong("previousSequence", 22);
        OtaState state = new OtaState(values);

        OtaState.Boot boot = state.prepareBoot("42100:4.21.0");

        assertEquals("/private/bundle-22", boot.path);
        assertEquals("/private/bundle-23", boot.discardPath);
        assertEquals(23L, state.highestSequence());
        assertFalse(state.isPendingReady());
    }

    @Test
    public void readyCommitsCandidateAndDiscardsPreviousBundle() {
        MemoryValues values = new MemoryValues();
        values.putString("binary", "42100:4.21.0");
        values.putString("activeVersion", "4.21.0-web.22");
        values.putString("activePath", "/private/bundle-22");
        values.putLong("activeSequence", 22);
        OtaState state = new OtaState(values);

        state.beginApply("4.21.0-web.23", 23, "/private/bundle-23");
        String discard = state.markReady();

        assertEquals("/private/bundle-22", discard);
        assertEquals("4.21.0-web.23", state.currentVersion("4.21.0"));
        assertEquals(23L, state.highestSequence());
        assertFalse(state.isPendingReady());
    }

    @Test
    public void watchdogRollbackUsesTheSameDurableTransitionAsColdBoot() {
        MemoryValues values = new MemoryValues();
        values.putString("binary", "42100:4.21.0");
        OtaState state = new OtaState(values);
        state.beginApply("4.21.0-web.23", 23, "/private/bundle-23");

        OtaState.Boot rollback = state.rollbackPending();

        assertNull(rollback.path);
        assertEquals("/private/bundle-23", rollback.discardPath);
        assertEquals(23L, state.highestSequence());
        assertFalse(state.isPendingReady());
    }

    private MemoryValues activeState() {
        MemoryValues values = new MemoryValues();
        values.putString("binary", "42100:4.21.0");
        values.putString("activeVersion", "4.21.0-web.23");
        values.putString("activePath", "/private/bundle-23");
        values.putLong("activeSequence", 23);
        values.putLong("highestSequence", 23);
        return values;
    }

    private static final class MemoryValues implements OtaState.Values {
        private final Map<String, Object> data = new HashMap<>();
        public String getString(String key) { return (String) data.get(key); }
        public long getLong(String key) { Object value = data.get(key); return value == null ? 0 : (long) value; }
        public boolean getBoolean(String key) { return Boolean.TRUE.equals(data.get(key)); }
        public void putString(String key, String value) { if (value == null) data.remove(key); else data.put(key, value); }
        public void putLong(String key, long value) { data.put(key, value); }
        public void putBoolean(String key, boolean value) { data.put(key, value); }
        public void clear() { data.clear(); }
        public void commit() {}
        boolean contains(String key) { return data.containsKey(key); }
    }
}
