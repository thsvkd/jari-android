package com.jari.app.updates;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

public final class OtaDownload {
    public static final long MAX_ARCHIVE_BYTES = 20L * 1024 * 1024;
    public static final long MAX_MANIFEST_BYTES = 1024L * 1024;
    private static final int MAX_REDIRECTS = 5;
    private static final int CONNECT_TIMEOUT_MS = 15_000;
    private static final int READ_TIMEOUT_MS = 30_000;

    private OtaDownload() {}

    public static String fetchText(URI uri) throws OtaException {
        return fetchText(uri, new OtaCancellation());
    }

    public static String fetchText(URI uri, OtaCancellation cancellation) throws OtaException {
        java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
        try (InputStream input = open(uri, MAX_MANIFEST_BYTES, cancellation)) {
            copyAndDigest(input, output, MAX_MANIFEST_BYTES, cancellation);
        } catch (IOException error) {
            throw new OtaException("Cannot read update manifest", error);
        }
        return new String(output.toByteArray(), StandardCharsets.UTF_8);
    }

    public static String toFile(URI uri, File destination) throws OtaException {
        return toFile(uri, destination, new OtaCancellation());
    }

    public static String toFile(URI uri, File destination, OtaCancellation cancellation) throws OtaException {
        try (InputStream input = open(uri, MAX_ARCHIVE_BYTES, cancellation);
             FileOutputStream file = new FileOutputStream(destination);
             BufferedOutputStream output = new BufferedOutputStream(file)) {
            String digest = copyAndDigest(input, output, MAX_ARCHIVE_BYTES, cancellation);
            output.flush();
            file.getFD().sync();
            return digest;
        } catch (IOException error) {
            throw new OtaException("Cannot download update archive", error);
        }
    }

    static String copyAndDigest(InputStream input, OutputStream output, long limit) throws OtaException {
        return copyAndDigest(input, output, limit, new OtaCancellation());
    }

    private static String copyAndDigest(InputStream input, OutputStream output, long limit, OtaCancellation cancellation) throws OtaException {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException impossible) {
            throw new OtaException("SHA-256 is unavailable", impossible);
        }
        long total = 0;
        byte[] buffer = new byte[16 * 1024];
        try {
            int count;
            while ((count = input.read(buffer)) != -1) {
                cancellation.throwIfCancelled();
                total += count;
                if (total > limit) throw new OtaException("Download exceeds the size limit");
                digest.update(buffer, 0, count);
                output.write(buffer, 0, count);
            }
        } catch (IOException error) {
            throw new OtaException("Cannot copy update download", error);
        }
        return hex(digest.digest());
    }

    private static InputStream open(URI start, long limit, OtaCancellation cancellation) throws OtaException {
        URI current = requireHttps(start);
        for (int redirect = 0; redirect <= MAX_REDIRECTS; redirect++) {
            cancellation.throwIfCancelled();
            HttpURLConnection connection = null;
            try {
                connection = (HttpURLConnection) new URL(current.toString()).openConnection();
                cancellation.attach(connection);
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
                connection.setReadTimeout(READ_TIMEOUT_MS);
                connection.setRequestProperty("Accept-Encoding", "identity");
                connection.connect();
            } catch (IOException error) {
                if (connection != null) {
                    connection.disconnect();
                    cancellation.detach(connection);
                }
                throw new OtaException("Cannot connect to update server", error);
            }
            int status;
            try {
                status = connection.getResponseCode();
            } catch (IOException error) {
                connection.disconnect();
                cancellation.detach(connection);
                throw new OtaException("Cannot read update response", error);
            }
            if (status >= 300 && status <= 399) {
                String location = connection.getHeaderField("Location");
                connection.disconnect();
                cancellation.detach(connection);
                if (location == null || redirect == MAX_REDIRECTS) throw new OtaException("Invalid update redirect");
                current = requireHttps(current.resolve(location));
                continue;
            }
            if (status != HttpURLConnection.HTTP_OK) {
                connection.disconnect();
                cancellation.detach(connection);
                throw new OtaException("Update server returned HTTP " + status);
            }
            long length = connection.getContentLengthLong();
            if (length > limit) {
                connection.disconnect();
                cancellation.detach(connection);
                throw new OtaException("Download exceeds the size limit");
            }
            try {
                return new DisconnectingInputStream(connection, cancellation);
            } catch (IOException error) {
                connection.disconnect();
                cancellation.detach(connection);
                throw new OtaException("Cannot open update response", error);
            }
        }
        throw new OtaException("Too many update redirects");
    }

    private static URI requireHttps(URI uri) throws OtaException {
        if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null || uri.getFragment() != null) {
            throw new OtaException("Update downloads require HTTPS");
        }
        return uri;
    }

    private static String hex(byte[] bytes) {
        char[] alphabet = "0123456789abcdef".toCharArray();
        char[] result = new char[bytes.length * 2];
        for (int index = 0; index < bytes.length; index++) {
            int value = bytes[index] & 255;
            result[index * 2] = alphabet[value >>> 4];
            result[index * 2 + 1] = alphabet[value & 15];
        }
        return new String(result);
    }

    private static final class DisconnectingInputStream extends BufferedInputStream {
        private final HttpURLConnection connection;
        private final OtaCancellation cancellation;

        DisconnectingInputStream(HttpURLConnection connection, OtaCancellation cancellation) throws IOException {
            super(connection.getInputStream());
            this.connection = connection;
            this.cancellation = cancellation;
        }

        @Override
        public void close() throws IOException {
            try {
                super.close();
            } finally {
                connection.disconnect();
                cancellation.detach(connection);
            }
        }
    }
}
