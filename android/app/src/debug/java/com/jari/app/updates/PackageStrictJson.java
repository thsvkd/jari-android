package com.jari.app.updates;

import java.util.LinkedHashMap;
import java.util.Map;

final class PackageStrictJson {
    private final String source;
    private int offset;

    private PackageStrictJson(String source) {
        this.source = source;
    }

    static Map<String, Object> object(String source) throws PackageUpdateException {
        PackageStrictJson parser = new PackageStrictJson(source);
        Map<String, Object> result = parser.readObject();
        parser.whitespace();
        if (parser.offset != source.length()) throw new PackageUpdateException("Trailing JSON content");
        return result;
    }

    private Map<String, Object> readObject() throws PackageUpdateException {
        expect('{');
        Map<String, Object> result = new LinkedHashMap<>();
        whitespace();
        if (take('}')) return result;
        while (true) {
            whitespace();
            String key = readString();
            if (result.containsKey(key)) throw new PackageUpdateException("Duplicate JSON field: " + key);
            whitespace();
            expect(':');
            whitespace();
            result.put(key, readValue());
            whitespace();
            if (take('}')) return result;
            expect(',');
        }
    }

    private Object readValue() throws PackageUpdateException {
        if (offset >= source.length()) throw new PackageUpdateException("Missing JSON value");
        char current = source.charAt(offset);
        if (current == '"') return readString();
        if (current == '-' || current >= '0' && current <= '9') return readInteger();
        if (source.startsWith("true", offset)) { offset += 4; return Boolean.TRUE; }
        if (source.startsWith("false", offset)) { offset += 5; return Boolean.FALSE; }
        if (source.startsWith("null", offset)) { offset += 4; return null; }
        throw new PackageUpdateException("Unsupported JSON value");
    }

    private Long readInteger() throws PackageUpdateException {
        int start = offset;
        if (take('-') && offset == source.length()) throw new PackageUpdateException("Invalid JSON number");
        if (take('0')) {
            if (offset < source.length() && Character.isDigit(source.charAt(offset))) {
                throw new PackageUpdateException("Invalid JSON number");
            }
        } else {
            if (offset >= source.length() || source.charAt(offset) < '1' || source.charAt(offset) > '9') {
                throw new PackageUpdateException("Invalid JSON number");
            }
            while (offset < source.length() && Character.isDigit(source.charAt(offset))) offset++;
        }
        try {
            return Long.parseLong(source.substring(start, offset));
        } catch (NumberFormatException error) {
            throw new PackageUpdateException("JSON integer is out of range", error);
        }
    }

    private String readString() throws PackageUpdateException {
        expect('"');
        StringBuilder result = new StringBuilder();
        while (offset < source.length()) {
            char current = source.charAt(offset++);
            if (current == '"') return result.toString();
            if (current < 0x20) throw new PackageUpdateException("Control character in JSON string");
            if (current != '\\') { result.append(current); continue; }
            if (offset >= source.length()) throw new PackageUpdateException("Invalid JSON escape");
            char escape = source.charAt(offset++);
            switch (escape) {
                case '"': case '\\': case '/': result.append(escape); break;
                case 'b': result.append('\b'); break;
                case 'f': result.append('\f'); break;
                case 'n': result.append('\n'); break;
                case 'r': result.append('\r'); break;
                case 't': result.append('\t'); break;
                case 'u': result.append(readUnicode()); break;
                default: throw new PackageUpdateException("Invalid JSON escape");
            }
        }
        throw new PackageUpdateException("Unterminated JSON string");
    }

    private char readUnicode() throws PackageUpdateException {
        if (offset + 4 > source.length()) throw new PackageUpdateException("Invalid Unicode escape");
        int value = 0;
        for (int index = 0; index < 4; index++) {
            int digit = Character.digit(source.charAt(offset++), 16);
            if (digit < 0) throw new PackageUpdateException("Invalid Unicode escape");
            value = value * 16 + digit;
        }
        return (char) value;
    }

    private void whitespace() {
        while (offset < source.length()) {
            char current = source.charAt(offset);
            if (current != ' ' && current != '\n' && current != '\r' && current != '\t') return;
            offset++;
        }
    }

    private boolean take(char wanted) {
        if (offset < source.length() && source.charAt(offset) == wanted) { offset++; return true; }
        return false;
    }

    private void expect(char wanted) throws PackageUpdateException {
        if (!take(wanted)) throw new PackageUpdateException("Expected '" + wanted + "' in JSON");
    }
}
