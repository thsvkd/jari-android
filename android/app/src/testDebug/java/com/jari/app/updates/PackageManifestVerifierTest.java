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

public class PackageManifestVerifierTest {
    private static KeyPair keyPair;

    @BeforeClass
    public static void generateKey() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(2048);
        keyPair = generator.generateKeyPair();
    }

    @Test
    public void acceptsAnExactlySignedSchemaTwoPackage() throws Exception {
        PackageManifest manifest = verifier().verify(envelope(payload(
            "4.23.0", 42300, "dev.thsvkd.jari", "https://updates.example/jari.apk"
        )));

        assertEquals("4.23.0", manifest.version);
        assertEquals(42300, manifest.versionCode);
        assertEquals("dev.thsvkd.jari", manifest.packageName);
        assertEquals("https://updates.example/jari.apk", manifest.url.toString());
    }

    @Test
    public void rejectsChangedSignaturesAndUnexpectedPayloadFields() throws Exception {
        String signed = envelope(payload("4.23.0", 42300, "dev.thsvkd.jari", "https://updates.example/jari.apk"));
        assertThrows(PackageUpdateException.class, () -> verifier().verify(signed.replace("\"payload\":\"e", "\"payload\":\"f")));

        String unknown = payload("4.23.0", 42300, "dev.thsvkd.jari", "https://updates.example/jari.apk")
            .replace("\"schema\":2", "\"schema\":2,\"extra\":true");
        assertThrows(PackageUpdateException.class, () -> verifier().verify(envelope(unknown)));
    }

    @Test
    public void rejectsWrongPackageUnsafeVersionAndUnsafeUrl() throws Exception {
        assertThrows(PackageUpdateException.class, () -> verifier().verify(envelope(payload(
            "4.23.0", 42300, "other.app", "https://updates.example/jari.apk"
        ))));
        assertThrows(PackageUpdateException.class, () -> verifier().verify(envelope(payload(
            "4.23.0", 0, "dev.thsvkd.jari", "https://updates.example/jari.apk"
        ))));
        assertThrows(PackageUpdateException.class, () -> verifier().verify(envelope(payload(
            "4.23.0", 42300, "dev.thsvkd.jari", "http://updates.example/jari.apk"
        ))));
        assertThrows(PackageUpdateException.class, () -> verifier().verify(envelope(payload(
            "4.23.0", 42300, "dev.thsvkd.jari", "https://user@updates.example/jari.apk"
        ))));
    }

    @Test
    public void rejectsRsaKeysSmallerThan2048Bits() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(1024);
        KeyPair weak = generator.generateKeyPair();

        assertThrows(PackageUpdateException.class, () -> new PackageManifestVerifier(
            Base64.getEncoder().encodeToString(weak.getPublic().getEncoded()),
            "dev.thsvkd.jari"
        ));
    }

    private PackageManifestVerifier verifier() throws PackageUpdateException {
        return new PackageManifestVerifier(
            Base64.getEncoder().encodeToString(keyPair.getPublic().getEncoded()),
            "dev.thsvkd.jari"
        );
    }

    private String envelope(String payload) throws Exception {
        byte[] bytes = payload.getBytes(StandardCharsets.UTF_8);
        Signature signer = Signature.getInstance("SHA256withRSA");
        signer.initSign(keyPair.getPrivate());
        signer.update(bytes);
        return "{\"payload\":\"" + Base64.getEncoder().encodeToString(bytes) + "\",\"signature\":\""
            + Base64.getEncoder().encodeToString(signer.sign()) + "\"}";
    }

    private String payload(String version, long versionCode, String packageName, String url) {
        return "{\"schema\":2,\"version\":\"" + version + "\",\"versionCode\":" + versionCode
            + ",\"packageName\":\"" + packageName + "\",\"url\":\"" + url
            + "\",\"sha256\":\"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\"}";
    }
}
