package com.jari.app.updates;

import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.PublicKey;
import java.security.Signature;
import java.security.spec.X509EncodedKeySpec;
import java.security.interfaces.RSAPublicKey;
import java.util.Map;
import java.util.HashSet;
import java.util.Arrays;
import java.util.Set;
import java.util.regex.Pattern;

public final class OtaManifestVerifier {
    private static final long MAX_SAFE_INTEGER = 9_007_199_254_740_991L;
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Set<String> ENVELOPE_FIELDS = new HashSet<>(Arrays.asList("payload", "signature"));
    private static final Set<String> PAYLOAD_FIELDS = new HashSet<>(Arrays.asList("schema", "version", "sequence", "nativeVersion", "url", "sha256"));
    private final PublicKey publicKey;
    private final String nativeVersion;

    public OtaManifestVerifier(String publicKeyBase64, String nativeVersion) throws OtaException {
        this.nativeVersion = nativeVersion;
        try {
            publicKey = KeyFactory.getInstance("RSA").generatePublic(new X509EncodedKeySpec(OtaBase64.decode(publicKeyBase64)));
            if (!(publicKey instanceof RSAPublicKey) || ((RSAPublicKey) publicKey).getModulus().bitLength() < 2048) {
                throw new OtaException("OTA public key must be RSA with at least 2048 bits");
            }
        } catch (OtaException error) {
            throw error;
        } catch (Exception error) {
            throw new OtaException("Invalid OTA public key", error);
        }
    }

    public OtaManifest verify(String envelopeJson) throws OtaException {
        Map<String, Object> envelope = StrictJson.object(envelopeJson);
        exactFields(envelope, ENVELOPE_FIELDS, "manifest envelope");
        byte[] payload = OtaBase64.decode(string(envelope, "payload"));
        byte[] signature = OtaBase64.decode(string(envelope, "signature"));
        verifySignature(payload, signature);
        String payloadJson;
        try {
            payloadJson = StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(payload)).toString();
        } catch (CharacterCodingException error) {
            throw new OtaException("Manifest payload is not UTF-8", error);
        }
        Map<String, Object> values = StrictJson.object(payloadJson);
        exactFields(values, PAYLOAD_FIELDS, "manifest payload");
        if (number(values, "schema") != 1) throw new OtaException("Unsupported manifest schema");
        String version = nonEmpty(string(values, "version"), "version");
        long sequence = number(values, "sequence");
        if (sequence <= 0 || sequence > MAX_SAFE_INTEGER) throw new OtaException("Invalid update sequence");
        String requiredNativeVersion = nonEmpty(string(values, "nativeVersion"), "nativeVersion");
        URI url = httpsUri(string(values, "url"));
        String sha256 = string(values, "sha256");
        if (!SHA256.matcher(sha256).matches()) throw new OtaException("Invalid SHA-256 digest");
        return new OtaManifest(version, sequence, requiredNativeVersion, url, sha256);
    }

    private void verifySignature(byte[] payload, byte[] signatureBytes) throws OtaException {
        try {
            Signature verifier = Signature.getInstance("SHA256withRSA");
            verifier.initVerify(publicKey);
            verifier.update(payload);
            if (!verifier.verify(signatureBytes)) throw new OtaException("Invalid manifest signature");
        } catch (OtaException error) {
            throw error;
        } catch (Exception error) {
            throw new OtaException("Cannot verify manifest signature", error);
        }
    }

    private static URI httpsUri(String raw) throws OtaException {
        try {
            URI uri = URI.create(raw);
            if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getHost().isEmpty()
                || uri.getUserInfo() != null || uri.getFragment() != null) {
                throw new OtaException("Update URL must be an absolute HTTPS URL");
            }
            return uri;
        } catch (IllegalArgumentException error) {
            throw new OtaException("Invalid update URL", error);
        }
    }

    private static void exactFields(Map<String, Object> values, Set<String> fields, String label) throws OtaException {
        if (!values.keySet().equals(fields)) throw new OtaException("Unexpected fields in " + label);
    }

    private static String string(Map<String, Object> values, String key) throws OtaException {
        Object value = values.get(key);
        if (!(value instanceof String)) throw new OtaException("Manifest field is not a string: " + key);
        return (String) value;
    }

    private static long number(Map<String, Object> values, String key) throws OtaException {
        Object value = values.get(key);
        if (!(value instanceof Long)) throw new OtaException("Manifest field is not an integer: " + key);
        return (long) value;
    }

    private static String nonEmpty(String value, String key) throws OtaException {
        if (value.isEmpty() || value.length() > 128 || !value.equals(value.trim())) throw new OtaException("Invalid " + key);
        return value;
    }
}
