package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import org.junit.Test;

public class PackageDownloadTest {
    @Test
    public void copiesWithinLimitAndReturnsLowercaseSha256() throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();

        String digest = PackageDownload.copyAndDigest(
            new ByteArrayInputStream("hello".getBytes(StandardCharsets.UTF_8)), output, 5
        );

        assertEquals("hello", output.toString("UTF-8"));
        assertEquals("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", digest);
    }

    @Test
    public void rejectsOneBytePastTheLimit() {
        assertThrows(PackageUpdateException.class, () -> PackageDownload.copyAndDigest(
            new ByteArrayInputStream("123456".getBytes(StandardCharsets.UTF_8)),
            new ByteArrayOutputStream(),
            5
        ));
    }
}
