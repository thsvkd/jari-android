package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import java.util.zip.CRC32;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class OtaArchiveTest {
    @Rule public TemporaryFolder temporaryFolder = new TemporaryFolder();

    @Test
    public void extractsAFlatWebBundleContainingIndex() throws Exception {
        File zip = writeZip(entries("index.html", "<main>ready</main>", "assets/app.js", "ok"));
        File target = temporaryFolder.newFolder("target");

        new OtaArchive(10, 1024).extract(zip, target);

        assertEquals("<main>ready</main>", new String(Files.readAllBytes(new File(target, "index.html").toPath()), StandardCharsets.UTF_8));
        assertEquals("ok", new String(Files.readAllBytes(new File(target, "assets/app.js").toPath()), StandardCharsets.UTF_8));
    }

    @Test
    public void rejectsTraversalAbsoluteDuplicateAndMissingIndexEntries() throws Exception {
        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(writeZip(entries("../escape", "x", "index.html", "ok")), temporaryFolder.newFolder("a")));
        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(writeZip(entries("/escape", "x", "index.html", "ok")), temporaryFolder.newFolder("b")));
        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(writeDuplicateZip(), temporaryFolder.newFolder("c")));
        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(writeZip(entries("app.js", "x")), temporaryFolder.newFolder("d")));
        assertTrue(!new File(temporaryFolder.getRoot(), "escape").exists());
    }

    @Test
    public void rejectsEntryCountAndExpandedSizeLimits() throws Exception {
        assertThrows(OtaException.class, () -> new OtaArchive(1, 1024).extract(writeZip(entries("index.html", "x", "app.js", "y")), temporaryFolder.newFolder("count")));
        assertThrows(OtaException.class, () -> new OtaArchive(10, 4).extract(writeZip(entries("index.html", "12345")), temporaryFolder.newFolder("size")));
    }

    @Test
    public void rejectsUnixSymbolicLinks() throws Exception {
        byte[] zip = zipBytes(entries("index.html", "ok", "link", "target"));
        markCentralEntryAsSymlink(zip, "link");
        File file = temporaryFolder.newFile("symlink.zip");
        Files.write(file.toPath(), zip);

        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(file, temporaryFolder.newFolder("symlink")));
    }

    @Test
    public void doesNotTreatCentralDirectoryMagicInsideAssetAsZipMetadata() throws Exception {
        byte[] content = new byte[] { 'P', 'K', 1, 2, 0, 0, 0, 0, 0, 0 };
        File file = writeBinaryZip("index.html", content);
        File target = temporaryFolder.newFolder("embedded-magic");

        new OtaArchive(10, 1024).extract(file, target);

        assertEquals(content.length, new File(target, "index.html").length());
    }

    @Test
    public void rejectsExecutableNamesHeadersAndUnixExecuteBits() throws Exception {
        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(writeZip(entries("index.html", "ok", "payload.dex", "x")), temporaryFolder.newFolder("dex")));
        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(writeBinaryZip("index.html", new byte[] { 0x7f, 'E', 'L', 'F' }), temporaryFolder.newFolder("elf")));

        byte[] zip = zipBytes(entries("index.html", "ok", "run", "text"));
        markCentralEntryMode(zip, "run", 0100755);
        File executable = temporaryFolder.newFile("executable.zip");
        Files.write(executable.toPath(), zip);
        assertThrows(OtaException.class, () -> new OtaArchive(10, 1024).extract(executable, temporaryFolder.newFolder("mode")));
    }

    private File writeZip(Map<String, String> entries) throws Exception {
        File file = temporaryFolder.newFile();
        Files.write(file.toPath(), zipBytes(entries));
        return file;
    }

    private File writeDuplicateZip() throws Exception {
        byte[] bytes = zipBytes(entries("index.html", "ok", "same-name-a", "x", "same-name-b", "y"));
        byte[] before = "same-name-b".getBytes(StandardCharsets.UTF_8);
        byte[] after = "same-name-a".getBytes(StandardCharsets.UTF_8);
        for (int offset = 0; offset <= bytes.length - before.length; offset++) {
            boolean match = true;
            for (int index = 0; index < before.length; index++) match &= bytes[offset + index] == before[index];
            if (match) System.arraycopy(after, 0, bytes, offset, after.length);
        }
        File file = temporaryFolder.newFile();
        Files.write(file.toPath(), bytes);
        return file;
    }

    private byte[] zipBytes(Map<String, String> entries) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (ZipOutputStream output = new ZipOutputStream(bytes)) {
            for (Map.Entry<String, String> entry : entries.entrySet()) {
                output.putNextEntry(new ZipEntry(entry.getKey()));
                output.write(entry.getValue().getBytes(StandardCharsets.UTF_8));
                output.closeEntry();
            }
        }
        return bytes.toByteArray();
    }

    private File writeBinaryZip(String name, byte[] content) throws Exception {
        File file = temporaryFolder.newFile();
        try (ZipOutputStream output = new ZipOutputStream(new FileOutputStream(file))) {
            ZipEntry entry = new ZipEntry(name);
            CRC32 crc = new CRC32();
            crc.update(content);
            entry.setMethod(ZipEntry.STORED);
            entry.setSize(content.length);
            entry.setCompressedSize(content.length);
            entry.setCrc(crc.getValue());
            output.putNextEntry(entry);
            output.write(content);
            output.closeEntry();
        }
        return file;
    }

    private Map<String, String> entries(String... values) {
        Map<String, String> entries = new LinkedHashMap<>();
        for (int index = 0; index < values.length; index += 2) entries.put(values[index], values[index + 1]);
        return entries;
    }

    private void markCentralEntryAsSymlink(byte[] zip, String wantedName) {
        markCentralEntryMode(zip, wantedName, 0120000);
    }

    private void markCentralEntryMode(byte[] zip, String wantedName, int unixMode) {
        for (int index = 0; index <= zip.length - 46; index++) {
            if (littleEndianInt(zip, index) != 0x02014b50) continue;
            int nameLength = littleEndianShort(zip, index + 28);
            String name = new String(zip, index + 46, nameLength, StandardCharsets.UTF_8);
            if (!wantedName.equals(name)) continue;
            zip[index + 5] = 3; // Unix platform in "version made by".
            int mode = unixMode << 16;
            for (int byteIndex = 0; byteIndex < 4; byteIndex++) zip[index + 38 + byteIndex] = (byte) (mode >>> (8 * byteIndex));
            return;
        }
        throw new AssertionError("central entry not found");
    }

    private int littleEndianShort(byte[] bytes, int offset) {
        return (bytes[offset] & 255) | ((bytes[offset + 1] & 255) << 8);
    }

    private int littleEndianInt(byte[] bytes, int offset) {
        return littleEndianShort(bytes, offset) | (littleEndianShort(bytes, offset + 2) << 16);
    }
}
