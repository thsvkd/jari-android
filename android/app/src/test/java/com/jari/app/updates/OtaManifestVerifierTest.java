package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.Signature;
import java.util.Base64;
import org.junit.BeforeClass;
import org.junit.Test;

public class OtaManifestVerifierTest {
    private static KeyPair keyPair;

    @BeforeClass
    public static void generateKey() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(2048);
        keyPair = generator.generateKeyPair();
    }

    @Test
    public void acceptsSignedPayloadWithExactNativeVersion() throws Exception {
        String payload = payload(23, "4.21.0", "https://updates.example/app.zip");

        OtaManifest manifest = verifier().verify(envelope(payload));

        assertEquals("4.21.0-web.23", manifest.version);
        assertEquals(23L, manifest.sequence);
        assertEquals("4.21.0", manifest.nativeVersion);
        assertEquals("https://updates.example/app.zip", manifest.url.toString());
    }

    @Test
    public void rejectsPayloadChangedAfterSigning() throws Exception {
        String signed = envelope(payload(23, "4.21.0", "https://updates.example/app.zip"));

        assertThrows(OtaException.class, () -> verifier().verify(signed.replace("\"payload\":\"e", "\"payload\":\"f")));
    }

    @Test
    public void preservesTargetNativeVersionButRejectsUnsafeSequenceAndUrl() throws Exception {
        assertEquals("4.22.0", verifier().verify(envelope(payload(23, "4.22.0", "https://updates.example/app.zip"))).nativeVersion);
        assertThrows(OtaException.class, () -> verifier().verify(envelope(payload(0, "4.21.0", "https://updates.example/app.zip"))));
        assertThrows(OtaException.class, () -> verifier().verify(envelope(payload(9_007_199_254_740_992L, "4.21.0", "https://updates.example/app.zip"))));
        assertThrows(OtaException.class, () -> verifier().verify(envelope(payload(23, "4.21.0", "http://updates.example/app.zip"))));
    }

    @Test
    public void rejectsUnknownOrDuplicatePayloadFields() throws Exception {
        String unknown = payload(23, "4.21.0", "https://updates.example/app.zip").replace("\"schema\":1", "\"schema\":1,\"extra\":true");
        String duplicate = payload(23, "4.21.0", "https://updates.example/app.zip").replace("\"schema\":1", "\"schema\":1,\"schema\":1");

        assertThrows(OtaException.class, () -> verifier().verify(envelope(unknown)));
        assertThrows(OtaException.class, () -> verifier().verify(envelope(duplicate)));
    }

    @Test
    public void rejectsRsaKeysSmallerThan2048Bits() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(1024);
        KeyPair weak = generator.generateKeyPair();

        assertThrows(OtaException.class, () -> new OtaManifestVerifier(
            Base64.getEncoder().encodeToString(weak.getPublic().getEncoded()),
            "4.21.0"
        ));
    }

    private OtaManifestVerifier verifier() throws OtaException {
        return new OtaManifestVerifier(Base64.getEncoder().encodeToString(keyPair.getPublic().getEncoded()), "4.21.0");
    }

    private String envelope(String payload) throws Exception {
        byte[] bytes = payload.getBytes(StandardCharsets.UTF_8);
        Signature signer = Signature.getInstance("SHA256withRSA");
        signer.initSign(keyPair.getPrivate());
        signer.update(bytes);
        return "{\"payload\":\"" + Base64.getEncoder().encodeToString(bytes) + "\",\"signature\":\""
            + Base64.getEncoder().encodeToString(signer.sign()) + "\"}";
    }

    private String payload(long sequence, String nativeVersion, String url) {
        return "{\"schema\":1,\"version\":\"4.21.0-web.23\",\"sequence\":" + sequence
            + ",\"nativeVersion\":\"" + nativeVersion + "\",\"url\":\"" + url
            + "\",\"sha256\":\"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\"}";
    }
}
