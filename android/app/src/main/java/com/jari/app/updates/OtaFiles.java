package com.jari.app.updates;

import android.content.Context;
import java.io.File;
import java.io.IOException;

public final class OtaFiles {
    private final File root;
    private final File bundles;

    public OtaFiles(Context context) {
        root = new File(context.getFilesDir(), "app-updates");
        bundles = new File(root, "bundles");
    }

    public File downloadFile(long sequence) throws OtaException {
        ensureRoot();
        return new File(root, "download-" + sequence + ".zip");
    }

    public String install(File archive, OtaManifest manifest) throws OtaException {
        ensureRoot();
        File staging = new File(root, "staging-" + manifest.sequence);
        File destination = new File(bundles, "bundle-" + manifest.sequence);
        deleteRecursively(staging);
        deleteRecursively(destination);
        if (!staging.mkdirs()) throw new OtaException("Cannot create update staging directory");
        try {
            new OtaArchive(OtaArchive.DEFAULT_MAX_ENTRIES, OtaArchive.DEFAULT_MAX_EXPANDED_BYTES).extract(archive, staging);
            if (!bundles.isDirectory() && !bundles.mkdirs()) throw new OtaException("Cannot create update bundles directory");
            if (!staging.renameTo(destination)) throw new OtaException("Cannot atomically install update bundle");
            return destination.getCanonicalPath();
        } catch (IOException error) {
            throw new OtaException("Cannot resolve installed update path", error);
        } finally {
            deleteRecursively(staging);
        }
    }

    public boolean isInstalled(String path) {
        if (path == null) return false;
        try {
            File candidate = new File(path);
            String rootPath = bundles.getCanonicalPath() + File.separator;
            return candidate.getCanonicalPath().startsWith(rootPath) && new File(candidate, "index.html").isFile();
        } catch (IOException error) {
            return false;
        }
    }

    public void delete(String path) {
        if (path == null) return;
        try {
            File target = new File(path);
            String rootPath = root.getCanonicalPath() + File.separator;
            if (!target.getCanonicalPath().startsWith(rootPath)) return;
            deleteRecursively(target);
        } catch (IOException ignored) {
            // A path that cannot be proven app-private is never touched.
        }
    }

    public void clearAll() {
        deleteRecursively(root);
    }

    private void deleteRecursively(File target) {
        if (target == null || !target.exists()) return;
        File[] children = target.listFiles();
        if (children != null) for (File child : children) deleteRecursively(child);
        if (!target.delete() && target.exists()) target.deleteOnExit();
    }

    private void ensureRoot() throws OtaException {
        if (!root.isDirectory() && !root.mkdirs()) throw new OtaException("Cannot create update storage");
    }
}
