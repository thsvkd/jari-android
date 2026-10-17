package com.jari.app.updates;

import java.net.URI;

public final class PackageManifest {
    public final String version;
    public final int versionCode;
    public final String packageName;
    public final URI url;
    public final String sha256;

    PackageManifest(String version, int versionCode, String packageName, URI url, String sha256) {
        this.version = version;
        this.versionCode = versionCode;
        this.packageName = packageName;
        this.url = url;
        this.sha256 = sha256;
    }
}
