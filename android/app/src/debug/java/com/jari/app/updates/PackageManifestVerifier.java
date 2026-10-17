package com.jari.app.updates;

import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.PublicKey;
import java.security.Signature;
import java.security.interfaces.RSAPublicKey;
import java.security.spec.X509EncodedKeySpec;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

public final class PackageManifestVerifier {
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Set<String> ENVELOPE_FIELDS = new HashSet<>(Arrays.asList("payload", "signature"));
    private static final Set<String> PAYLOAD_FIELDS = new HashSet<>(Arrays.asList(
        "schema", "version", "versionCode", "packageName", "url", "sha256"
    ));
    private final PublicKey publicKey;
    private final String expectedPackageName;

    public PackageManifestVerifier(String publicKeyBase64, String expectedPackageName) throws PackageUpdateException {
        this.expectedPackageName = expectedPackageName;
        try {
            publicKey = KeyFactory.getInstance("RSA").generatePublic(
                new X509EncodedKeySpec(PackageBase64.decode(publicKeyBase64))
            );
            if (!(publicKey instanceof RSAPublicKey) || ((RSAPublicKey) publicKey).getModulus().bitLength() < 2048) {
                throw new PackageUpdateException("Package update public key must be RSA with at least 2048 bits");
            }
        } catch (PackageUpdateException error) {
            throw error;
        } catch (Exception error) {
            throw new PackageUpdateException("Invalid package update public key", error);
        }
    }

    public PackageManifest verify(String envelopeJson) throws PackageUpdateException {
        Map<String, Object> envelope = PackageStrictJson.object(envelopeJson);
        exactFields(envelope, ENVELOPE_FIELDS, "manifest envelope");
        byte[] payload = PackageBase64.decode(string(envelope, "payload"));
        verifySignature(payload, PackageBase64.decode(string(envelope, "signature")));
        Map<String, Object> values = PackageStrictJson.object(decodeUtf8(payload));
        exactFields(values, PAYLOAD_FIELDS, "manifest payload");
        if (number(values, "schema") != 2) throw new PackageUpdateException("Unsupported manifest schema");
        String version = nonEmpty(string(values, "version"), "version");
        long rawVersionCode = number(values, "versionCode");
        if (rawVersionCode <= 0 || rawVersionCode > Integer.MAX_VALUE) {
            throw new PackageUpdateException("Invalid package version code");
        }
        String packageName = nonEmpty(string(values, "packageName"), "packageName");
        if (!expectedPackageName.equals(packageName)) throw new PackageUpdateException("Update package name does not match this app");
        URI url = httpsUri(string(values, "url"));
        String sha256 = string(values, "sha256");
        if (!SHA256.matcher(sha256).matches()) throw new PackageUpdateException("Invalid SHA-256 digest");
        return new PackageManifest(version, (int) rawVersionCode, packageName, url, sha256);
    }

    private void verifySignature(byte[] payload, byte[] signatureBytes) throws PackageUpdateException {
        try {
            Signature verifier = Signature.getInstance("SHA256withRSA");
            verifier.initVerify(publicKey);
            verifier.update(payload);
            if (!verifier.verify(signatureBytes)) throw new PackageUpdateException("Invalid manifest signature");
        } catch (PackageUpdateException error) {
            throw error;
        } catch (Exception error) {
            throw new PackageUpdateException("Cannot verify manifest signature", error);
        }
    }

    private static String decodeUtf8(byte[] payload) throws PackageUpdateException {
        try {
            return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(payload)).toString();
        } catch (CharacterCodingException error) {
            throw new PackageUpdateException("Manifest payload is not UTF-8", error);
        }
    }

    private static URI httpsUri(String raw) throws PackageUpdateException {
        try {
            URI uri = URI.create(raw);
            if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getHost().isEmpty()
                || uri.getUserInfo() != null || uri.getFragment() != null) {
                throw new PackageUpdateException("Update URL must be an absolute HTTPS URL");
            }
            return uri;
        } catch (IllegalArgumentException error) {
            throw new PackageUpdateException("Invalid update URL", error);
        }
    }

    private static void exactFields(Map<String, Object> values, Set<String> fields, String label) throws PackageUpdateException {
        if (!values.keySet().equals(fields)) throw new PackageUpdateException("Unexpected fields in " + label);
    }

    private static String string(Map<String, Object> values, String key) throws PackageUpdateException {
        Object value = values.get(key);
        if (!(value instanceof String)) throw new PackageUpdateException("Manifest field is not a string: " + key);
        return (String) value;
    }

    private static long number(Map<String, Object> values, String key) throws PackageUpdateException {
        Object value = values.get(key);
        if (!(value instanceof Long)) throw new PackageUpdateException("Manifest field is not an integer: " + key);
        return (long) value;
    }

    private static String nonEmpty(String value, String key) throws PackageUpdateException {
        if (value.isEmpty() || value.length() > 128 || !value.equals(value.trim())) {
            throw new PackageUpdateException("Invalid " + key);
        }
        return value;
    }
}
