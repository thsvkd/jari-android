package com.jari.app.updates;

public final class OtaState {
    public interface Values {
        String getString(String key);
        long getLong(String key);
        boolean getBoolean(String key);
        void putString(String key, String value);
        void putLong(String key, long value);
        void putBoolean(String key, boolean value);
        void clear();
        void commit();
    }

    public static final class Boot {
        public final String path;
        public final String discardPath;
        public final boolean resetStorage;

        Boot(String path, String discardPath, boolean resetStorage) {
            this.path = path;
            this.discardPath = discardPath;
            this.resetStorage = resetStorage;
        }
    }

    private final Values values;

    public OtaState(Values values) {
        this.values = values;
    }

    public synchronized Boot prepareBoot(String binary) {
        if (!binary.equals(values.getString("binary"))) {
            String discard = values.getString("activePath");
            String previous = values.getString("previousPath");
            values.clear();
            values.putString("binary", binary);
            values.commit();
            return new Boot(null, discard != null ? discard : previous, true);
        }
        if (isPendingReady()) return rollbackPending();
        return new Boot(values.getString("activePath"), null, false);
    }

    public synchronized void beginApply(String version, long sequence, String path) {
        long highest = highestSequence();
        values.putString("previousVersion", values.getString("activeVersion"));
        values.putString("previousPath", values.getString("activePath"));
        values.putLong("previousSequence", values.getLong("activeSequence"));
        values.putString("activeVersion", version);
        values.putString("activePath", path);
        values.putLong("activeSequence", sequence);
        values.putLong("highestSequence", Math.max(highest, sequence));
        values.putBoolean("pendingReady", true);
        values.commit();
    }

    public synchronized String markReady() {
        if (!isPendingReady()) return null;
        String discard = values.getString("previousPath");
        clearPrevious();
        values.putBoolean("pendingReady", false);
        values.commit();
        return discard;
    }

    public synchronized Boot rollbackPending() {
        if (!isPendingReady()) return new Boot(values.getString("activePath"), null, false);
        String discard = values.getString("activePath");
        String previousVersion = values.getString("previousVersion");
        String previousPath = values.getString("previousPath");
        long previousSequence = values.getLong("previousSequence");
        values.putString("activeVersion", previousVersion);
        values.putString("activePath", previousPath);
        values.putLong("activeSequence", previousSequence);
        clearPrevious();
        values.putBoolean("pendingReady", false);
        values.commit();
        return new Boot(previousPath, discard, false);
    }

    public synchronized String currentVersion(String nativeVersion) {
        String active = values.getString("activeVersion");
        return active == null ? nativeVersion : active;
    }

    public synchronized long highestSequence() {
        return values.getLong("highestSequence");
    }

    public synchronized long activeSequence() {
        return values.getLong("activeSequence");
    }

    public synchronized String activePath() {
        return values.getString("activePath");
    }

    public synchronized boolean isPendingReady() {
        return values.getBoolean("pendingReady");
    }

    public synchronized String clearActive() {
        String discard = values.getString("activePath");
        values.putString("activeVersion", null);
        values.putString("activePath", null);
        values.putLong("activeSequence", 0);
        clearPrevious();
        values.putBoolean("pendingReady", false);
        values.commit();
        return discard;
    }

    private void clearPrevious() {
        values.putString("previousVersion", null);
        values.putString("previousPath", null);
        values.putLong("previousSequence", 0);
    }
}
