package com.jari.app.updates;

import android.content.SharedPreferences;
import java.util.LinkedHashMap;
import java.util.Map;

public final class SharedPreferencesValues implements OtaState.Values {
    private final SharedPreferences preferences;
    private final Map<String, Object> changes = new LinkedHashMap<>();
    private boolean clear;

    public SharedPreferencesValues(SharedPreferences preferences) {
        this.preferences = preferences;
    }

    @Override
    public synchronized String getString(String key) {
        if (changes.containsKey(key)) return (String) changes.get(key);
        return clear ? null : preferences.getString(key, null);
    }

    @Override
    public synchronized long getLong(String key) {
        if (changes.containsKey(key)) return (long) changes.get(key);
        return clear ? 0 : preferences.getLong(key, 0);
    }

    @Override
    public synchronized boolean getBoolean(String key) {
        if (changes.containsKey(key)) return (boolean) changes.get(key);
        return !clear && preferences.getBoolean(key, false);
    }

    @Override
    public synchronized void putString(String key, String value) {
        changes.put(key, value);
    }

    @Override
    public synchronized void putLong(String key, long value) {
        changes.put(key, value);
    }

    @Override
    public synchronized void putBoolean(String key, boolean value) {
        changes.put(key, value);
    }

    @Override
    public synchronized void clear() {
        clear = true;
        changes.clear();
    }

    @Override
    public synchronized void commit() {
        SharedPreferences.Editor editor = preferences.edit();
        if (clear) editor.clear();
        for (Map.Entry<String, Object> change : changes.entrySet()) {
            Object value = change.getValue();
            if (value == null) editor.remove(change.getKey());
            else if (value instanceof String) editor.putString(change.getKey(), (String) value);
            else if (value instanceof Long) editor.putLong(change.getKey(), (long) value);
            else if (value instanceof Boolean) editor.putBoolean(change.getKey(), (boolean) value);
            else throw new IllegalStateException("Unsupported OTA state value");
        }
        boolean committed = editor.commit();
        clear = false;
        changes.clear();
        if (!committed) throw new IllegalStateException("Cannot persist OTA state");
    }
}
