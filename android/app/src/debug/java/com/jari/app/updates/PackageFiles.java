package com.jari.app.updates;

import android.content.Context;
import java.io.File;
import java.util.UUID;

public final class PackageFiles {
    private final File root;

    public PackageFiles(Context context) {
        this(context.getCacheDir(), UUID.randomUUID().toString());
    }

    PackageFiles(File cacheDirectory, String generation) {
        root = new File(new File(cacheDirectory, "package-updates"), generation);
    }

    public File prepareApk() throws PackageUpdateException {
        clear();
        if (!root.isDirectory() && !root.mkdirs()) {
            throw new PackageUpdateException("Cannot create package update cache");
        }
        return new File(root, "candidate.apk");
    }

    public void clear() {
        deleteRecursively(root);
    }

    private static void deleteRecursively(File target) {
        if (target == null || !target.exists()) return;
        File[] children = target.listFiles();
        if (children != null) for (File child : children) deleteRecursively(child);
        if (!target.delete() && target.exists()) target.deleteOnExit();
    }
}
