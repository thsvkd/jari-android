package com.jari.app.updates;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.nio.file.Files;
import org.junit.Test;

public class PackageFilesTest {
    @Test
    public void clearingOneChannelCannotDeleteAnotherChannelsCandidate() throws Exception {
        File cache = Files.createTempDirectory("package-files").toFile();
        PackageFiles first = new PackageFiles(cache, "first");
        PackageFiles second = new PackageFiles(cache, "second");
        File firstApk = first.prepareApk();
        File secondApk = second.prepareApk();
        assertTrue(firstApk.createNewFile());
        assertTrue(secondApk.createNewFile());
        assertNotEquals(firstApk.getParentFile(), secondApk.getParentFile());

        first.clear();

        assertFalse(firstApk.exists());
        assertTrue(secondApk.exists());
        second.clear();
    }
}
