package com.jari.app.updates;

import java.net.URI;

public final class OtaManifest {
    public final String version;
    public final long sequence;
    public final String nativeVersion;
    public final URI url;
    public final String sha256;

    OtaManifest(String version, long sequence, String nativeVersion, URI url, String sha256) {
        this.version = version;
        this.sequence = sequence;
        this.nativeVersion = nativeVersion;
        this.url = url;
        this.sha256 = sha256;
    }
}
