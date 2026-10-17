package com.jari.app.updates;

import java.util.LinkedHashMap;
import java.util.Map;

final class StrictJson {
    private final String source;
    private int offset;

    private StrictJson(String source) {
        this.source = source;
    }

    static Map<String, Object> object(String source) throws OtaException {
        StrictJson parser = new StrictJson(source);
        Map<String, Object> result = parser.readObject();
        parser.whitespace();
        if (parser.offset != source.length()) throw new OtaException("Trailing JSON content");
        return result;
    }

    private Map<String, Object> readObject() throws OtaException {
        expect('{');
        Map<String, Object> result = new LinkedHashMap<>();
        whitespace();
        if (take('}')) return result;
        while (true) {
            whitespace();
            String key = readString();
            if (result.containsKey(key)) throw new OtaException("Duplicate JSON field: " + key);
            whitespace();
            expect(':');
            whitespace();
            result.put(key, readValue());
            whitespace();
            if (take('}')) return result;
            expect(',');
        }
    }

    private Object readValue() throws OtaException {
        if (offset >= source.length()) throw new OtaException("Missing JSON value");
        char current = source.charAt(offset);
        if (current == '"') return readString();
        if (current == '-' || current >= '0' && current <= '9') return readInteger();
        if (source.startsWith("true", offset)) { offset += 4; return Boolean.TRUE; }
        if (source.startsWith("false", offset)) { offset += 5; return Boolean.FALSE; }
        if (source.startsWith("null", offset)) { offset += 4; return null; }
        throw new OtaException("Unsupported JSON value");
    }

    private Long readInteger() throws OtaException {
        int start = offset;
        if (take('-') && offset == source.length()) throw new OtaException("Invalid JSON number");
        if (take('0')) {
            if (offset < source.length() && Character.isDigit(source.charAt(offset))) throw new OtaException("Invalid JSON number");
        } else {
            if (offset >= source.length() || source.charAt(offset) < '1' || source.charAt(offset) > '9') throw new OtaException("Invalid JSON number");
            while (offset < source.length() && Character.isDigit(source.charAt(offset))) offset++;
        }
        try {
            return Long.parseLong(source.substring(start, offset));
        } catch (NumberFormatException error) {
            throw new OtaException("JSON integer is out of range", error);
        }
    }

    private String readString() throws OtaException {
        expect('"');
        StringBuilder result = new StringBuilder();
        while (offset < source.length()) {
            char current = source.charAt(offset++);
            if (current == '"') return result.toString();
            if (current < 0x20) throw new OtaException("Control character in JSON string");
            if (current != '\\') { result.append(current); continue; }
            if (offset >= source.length()) throw new OtaException("Invalid JSON escape");
            char escape = source.charAt(offset++);
            switch (escape) {
                case '"': case '\\': case '/': result.append(escape); break;
                case 'b': result.append('\b'); break;
                case 'f': result.append('\f'); break;
                case 'n': result.append('\n'); break;
                case 'r': result.append('\r'); break;
                case 't': result.append('\t'); break;
                case 'u': result.append(readUnicode()); break;
                default: throw new OtaException("Invalid JSON escape");
            }
        }
        throw new OtaException("Unterminated JSON string");
    }

    private char readUnicode() throws OtaException {
        if (offset + 4 > source.length()) throw new OtaException("Invalid Unicode escape");
        int value = 0;
        for (int index = 0; index < 4; index++) {
            int digit = Character.digit(source.charAt(offset++), 16);
            if (digit < 0) throw new OtaException("Invalid Unicode escape");
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

    private void expect(char wanted) throws OtaException {
        if (!take(wanted)) throw new OtaException("Expected '" + wanted + "' in JSON");
    }
}
