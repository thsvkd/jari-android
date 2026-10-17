package com.jari.app.updates;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

public final class OtaArchive {
    public static final int DEFAULT_MAX_ENTRIES = 2_000;
    public static final long DEFAULT_MAX_EXPANDED_BYTES = 50L * 1024 * 1024;
    private static final int CENTRAL_HEADER = 0x02014b50;
    private static final int END_HEADER = 0x06054b50;
    private final int maxEntries;
    private final long maxExpandedBytes;

    public OtaArchive(int maxEntries, long maxExpandedBytes) {
        this.maxEntries = maxEntries;
        this.maxExpandedBytes = maxExpandedBytes;
    }

    public void extract(File archive, File target) throws OtaException {
        Set<String> centralNames = inspectCentralDirectory(archive);
        if (!target.isDirectory() && !target.mkdirs()) throw new OtaException("Cannot create update directory");
        String root;
        try {
            root = target.getCanonicalPath() + File.separator;
        } catch (IOException error) {
            throw new OtaException("Cannot resolve update directory", error);
        }
        Set<String> extracted = new HashSet<>();
        long expanded = 0;
        byte[] buffer = new byte[16 * 1024];
        try (ZipInputStream input = new ZipInputStream(new BufferedInputStream(new FileInputStream(archive)))) {
            ZipEntry entry;
            while ((entry = input.getNextEntry()) != null) {
                String name = validateName(entry.getName());
                if (!centralNames.contains(name) || !extracted.add(name)) throw new OtaException("Ambiguous ZIP entry: " + name);
                File destination = new File(target, name);
                if (!destination.getCanonicalPath().startsWith(root)) throw new OtaException("ZIP entry leaves update directory");
                if (entry.isDirectory()) {
                    if (!destination.isDirectory() && !destination.mkdirs()) throw new OtaException("Cannot create update directory entry");
                } else {
                    rejectExecutableName(name);
                    rejectFileDirectoryConflict(name, extracted);
                    File parent = destination.getParentFile();
                    if (!parent.isDirectory() && !parent.mkdirs()) throw new OtaException("Cannot create update parent directory");
                    byte[] header = new byte[8];
                    int headerLength = 0;
                    try (BufferedOutputStream output = new BufferedOutputStream(new FileOutputStream(destination))) {
                        int count;
                        while ((count = input.read(buffer)) != -1) {
                            expanded += count;
                            if (expanded > maxExpandedBytes) throw new OtaException("Update archive expands beyond the limit");
                            output.write(buffer, 0, count);
                            if (headerLength < header.length) {
                                int copied = Math.min(count, header.length - headerLength);
                                System.arraycopy(buffer, 0, header, headerLength, copied);
                                headerLength += copied;
                            }
                        }
                    }
                    rejectExecutableHeader(header, headerLength);
                }
                input.closeEntry();
            }
        } catch (OtaException error) {
            throw error;
        } catch (IOException error) {
            throw new OtaException("Cannot extract update archive", error);
        }
        if (!extracted.equals(centralNames)) throw new OtaException("ZIP directory does not match its entries");
        File index = new File(target, "index.html");
        if (!index.isFile()) throw new OtaException("Update archive has no index.html");
    }

    private Set<String> inspectCentralDirectory(File archive) throws OtaException {
        byte[] bytes;
        try {
            try (FileInputStream input = new FileInputStream(archive); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[16 * 1024];
                int count;
                while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                bytes = output.toByteArray();
            }
        } catch (IOException error) {
            throw new OtaException("Cannot read update archive", error);
        }
        int end = findEnd(bytes);
        int disk = littleEndianShort(bytes, end + 4);
        int centralDisk = littleEndianShort(bytes, end + 6);
        int entriesOnDisk = littleEndianShort(bytes, end + 8);
        int entries = littleEndianShort(bytes, end + 10);
        long centralSizeLong = littleEndianUnsignedInt(bytes, end + 12);
        long centralOffsetLong = littleEndianUnsignedInt(bytes, end + 16);
        int commentLength = littleEndianShort(bytes, end + 20);
        if (end + 22 + commentLength != bytes.length || disk != 0 || centralDisk != 0 || entriesOnDisk != entries
            || entries == 0xffff || centralSizeLong == 0xffffffffL || centralOffsetLong == 0xffffffffL
            || entries == 0 || entries > maxEntries || centralOffsetLong + centralSizeLong != end) {
            throw new OtaException("Unsupported or inconsistent ZIP directory");
        }
        int offset = (int) centralOffsetLong;
        int centralEnd = offset + (int) centralSizeLong;
        Set<String> names = new HashSet<>();
        for (int entryIndex = 0; entryIndex < entries; entryIndex++) {
            if (offset > centralEnd - 46 || littleEndianInt(bytes, offset) != CENTRAL_HEADER) {
                throw new OtaException("Invalid ZIP central directory");
            }
            int flags = littleEndianShort(bytes, offset + 8);
            int nameLength = littleEndianShort(bytes, offset + 28);
            int extraLength = littleEndianShort(bytes, offset + 30);
            int entryCommentLength = littleEndianShort(bytes, offset + 32);
            int next = offset + 46 + nameLength + extraLength + entryCommentLength;
            if (next > centralEnd || (flags & 1) != 0) throw new OtaException("Invalid or encrypted ZIP entry");
            byte[] nameBytes = new byte[nameLength];
            System.arraycopy(bytes, offset + 46, nameBytes, 0, nameLength);
            if ((flags & 0x800) == 0) {
                for (byte value : nameBytes) if ((value & 0x80) != 0) throw new OtaException("ZIP entry name must be UTF-8");
            }
            String name = validateName(new String(nameBytes, StandardCharsets.UTF_8));
            if (!names.add(name)) throw new OtaException("Duplicate ZIP entry: " + name);
            int platform = bytes[offset + 5] & 255;
            if (platform == 3) {
                int mode = littleEndianInt(bytes, offset + 38) >>> 16;
                int type = mode & 0170000;
                if (type != 0 && type != 0100000 && type != 0040000) throw new OtaException("ZIP contains a special or symbolic-link entry");
                if ((mode & 0111) != 0) throw new OtaException("ZIP contains an executable entry");
                if ((type == 0040000) != name.endsWith("/")) throw new OtaException("ZIP entry type is ambiguous");
            }
            offset = next;
        }
        if (offset != centralEnd) throw new OtaException("ZIP central directory size does not match");
        return names;
    }

    private static int findEnd(byte[] bytes) throws OtaException {
        int minimum = Math.max(0, bytes.length - 22 - 0xffff);
        for (int offset = bytes.length - 22; offset >= minimum; offset--) {
            if (littleEndianInt(bytes, offset) == END_HEADER) return offset;
        }
        throw new OtaException("ZIP end directory is missing");
    }

    private static String validateName(String name) throws OtaException {
        if (name.isEmpty() || name.startsWith("/") || name.startsWith("\\") || name.indexOf('\\') >= 0 || name.indexOf('\0') >= 0) {
            throw new OtaException("Unsafe ZIP entry name");
        }
        String withoutTrailingSlash = name.endsWith("/") ? name.substring(0, name.length() - 1) : name;
        if (withoutTrailingSlash.isEmpty() || withoutTrailingSlash.startsWith("/") || withoutTrailingSlash.matches("^[A-Za-z]:.*")) {
            throw new OtaException("Unsafe ZIP entry name");
        }
        for (String segment : withoutTrailingSlash.split("/", -1)) {
            if (segment.isEmpty() || segment.equals(".") || segment.equals("..")) throw new OtaException("Ambiguous ZIP entry name");
        }
        return name;
    }

    private static void rejectFileDirectoryConflict(String name, Set<String> extracted) throws OtaException {
        int slash = name.indexOf('/');
        while (slash >= 0) {
            if (extracted.contains(name.substring(0, slash))) throw new OtaException("ZIP file collides with directory");
            slash = name.indexOf('/', slash + 1);
        }
        if (extracted.contains(name + "/")) throw new OtaException("ZIP file collides with directory");
    }

    private static void rejectExecutableName(String name) throws OtaException {
        String lower = name.toLowerCase(Locale.ROOT);
        String[] blocked = { ".apk", ".aab", ".dex", ".jar", ".so", ".class", ".exe" };
        for (String extension : blocked) if (lower.endsWith(extension)) throw new OtaException("ZIP contains an executable file name");
    }

    private static void rejectExecutableHeader(byte[] header, int length) throws OtaException {
        boolean elf = length >= 4 && header[0] == 0x7f && header[1] == 'E' && header[2] == 'L' && header[3] == 'F';
        boolean mz = length >= 2 && header[0] == 'M' && header[1] == 'Z';
        boolean dex = length >= 4 && header[0] == 'd' && header[1] == 'e' && header[2] == 'x' && header[3] == '\n';
        int magic = length >= 4 ? (header[0] & 255) << 24 | (header[1] & 255) << 16 | (header[2] & 255) << 8 | header[3] & 255 : 0;
        boolean mach = magic == 0xfeedface || magic == 0xfeedfacf || magic == 0xcefaedfe || magic == 0xcffaedfe
            || magic == 0xcafebabe || magic == 0xbebafeca;
        if (elf || mz || dex || mach) throw new OtaException("ZIP contains executable content");
    }

    private static int littleEndianShort(byte[] bytes, int offset) {
        return (bytes[offset] & 255) | ((bytes[offset + 1] & 255) << 8);
    }

    private static int littleEndianInt(byte[] bytes, int offset) {
        return littleEndianShort(bytes, offset) | littleEndianShort(bytes, offset + 2) << 16;
    }

    private static long littleEndianUnsignedInt(byte[] bytes, int offset) {
        return littleEndianInt(bytes, offset) & 0xffffffffL;
    }
}
