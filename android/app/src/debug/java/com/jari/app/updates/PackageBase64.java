package com.jari.app.updates;

import java.io.ByteArrayOutputStream;

final class PackageBase64 {
    private PackageBase64() {}

    static byte[] decode(String encoded) throws PackageUpdateException {
        if (encoded == null || encoded.isEmpty() || encoded.length() % 4 != 0) {
            throw new PackageUpdateException("Invalid base64 value");
        }
        ByteArrayOutputStream output = new ByteArrayOutputStream(encoded.length() * 3 / 4);
        for (int offset = 0; offset < encoded.length(); offset += 4) {
            int a = value(encoded.charAt(offset));
            int b = value(encoded.charAt(offset + 1));
            char third = encoded.charAt(offset + 2);
            char fourth = encoded.charAt(offset + 3);
            boolean last = offset + 4 == encoded.length();
            if (a < 0 || b < 0 || third == '=' && fourth != '=' || !last && (third == '=' || fourth == '=')) {
                throw new PackageUpdateException("Invalid base64 value");
            }
            int c = third == '=' ? 0 : value(third);
            int d = fourth == '=' ? 0 : value(fourth);
            if (c < 0 || d < 0) throw new PackageUpdateException("Invalid base64 value");
            output.write((a << 2) | (b >>> 4));
            if (third != '=') output.write(((b & 15) << 4) | (c >>> 2));
            if (fourth != '=') output.write(((c & 3) << 6) | d);
            if (third == '=' && (b & 15) != 0 || fourth == '=' && third != '=' && (c & 3) != 0) {
                throw new PackageUpdateException("Non-canonical base64 value");
            }
        }
        return output.toByteArray();
    }

    private static int value(char character) {
        if (character >= 'A' && character <= 'Z') return character - 'A';
        if (character >= 'a' && character <= 'z') return character - 'a' + 26;
        if (character >= '0' && character <= '9') return character - '0' + 52;
        if (character == '+') return 62;
        if (character == '/') return 63;
        return -1;
    }
}
