package com.jari.app.updates;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.content.pm.SigningInfo;
import android.os.Build;
import androidx.core.content.pm.PackageInfoCompat;
import java.io.File;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class ApkPackageVerifier {
    private ApkPackageVerifier() {}

    public static void verify(Context context, File apk, PackageManifest manifest) throws PackageUpdateException {
        PackageManager packages = context.getPackageManager();
        PackageInfo installed = installedPackage(packages, context.getPackageName());
        PackageInfo candidate = archivePackage(packages, apk);
        if (candidate == null) throw new PackageUpdateException("Downloaded file is not a readable APK");
        if (!manifest.packageName.equals(candidate.packageName)) {
            throw new PackageUpdateException("Downloaded APK package name does not match the manifest");
        }
        if (PackageInfoCompat.getLongVersionCode(candidate) != manifest.versionCode) {
            throw new PackageUpdateException("Downloaded APK version code does not match the manifest");
        }
        if (!manifest.version.equals(candidate.versionName)) {
            throw new PackageUpdateException("Downloaded APK version name does not match the manifest");
        }
        if (PackageInfoCompat.getLongVersionCode(candidate) <= PackageInfoCompat.getLongVersionCode(installed)) {
            throw new PackageUpdateException("Downloaded APK is not newer than the installed app");
        }
        SigningData installedSigning = signingData(installed);
        SigningData candidateSigning = signingData(candidate);
        if (!SigningLineage.matches(
            installedSigning.multiple, installedSigning.current, installedSigning.history,
            candidateSigning.multiple, candidateSigning.current, candidateSigning.history
        )) {
            throw new PackageUpdateException("Downloaded APK signing identity does not match the installed app");
        }
    }

    private static PackageInfo installedPackage(PackageManager packages, String packageName)
        throws PackageUpdateException {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                return packages.getPackageInfo(packageName, PackageManager.PackageInfoFlags.of(PackageManager.GET_SIGNING_CERTIFICATES));
            }
            int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? PackageManager.GET_SIGNING_CERTIFICATES
                : PackageManager.GET_SIGNATURES;
            return packages.getPackageInfo(packageName, flags);
        } catch (PackageManager.NameNotFoundException error) {
            throw new PackageUpdateException("Cannot inspect the installed app", error);
        }
    }

    @SuppressWarnings("deprecation")
    private static PackageInfo archivePackage(PackageManager packages, File apk) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return packages.getPackageArchiveInfo(
                apk.getAbsolutePath(), PackageManager.PackageInfoFlags.of(PackageManager.GET_SIGNING_CERTIFICATES)
            );
        }
        int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
            ? PackageManager.GET_SIGNING_CERTIFICATES
            : PackageManager.GET_SIGNATURES;
        return packages.getPackageArchiveInfo(apk.getAbsolutePath(), flags);
    }

    @SuppressWarnings("deprecation")
    private static SigningData signingData(PackageInfo info) throws PackageUpdateException {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            SigningInfo signing = info.signingInfo;
            if (signing == null) throw new PackageUpdateException("APK signing information is missing");
            boolean multiple = signing.hasMultipleSigners();
            Signature[] current = signing.getApkContentsSigners();
            Signature[] history = multiple ? current : signing.getSigningCertificateHistory();
            return new SigningData(multiple, fingerprints(current), fingerprints(history));
        }
        List<String> values = fingerprints(info.signatures);
        return new SigningData(values.size() > 1, values, values);
    }

    private static List<String> fingerprints(Signature[] signatures) throws PackageUpdateException {
        if (signatures == null || signatures.length == 0) return Collections.emptyList();
        List<String> values = new ArrayList<>(signatures.length);
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            for (Signature signature : signatures) values.add(hex(digest.digest(signature.toByteArray())));
        } catch (Exception error) {
            throw new PackageUpdateException("Cannot inspect APK signing identity", error);
        }
        return values;
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

    private static final class SigningData {
        final boolean multiple;
        final List<String> current;
        final List<String> history;

        SigningData(boolean multiple, List<String> current, List<String> history) {
            this.multiple = multiple;
            this.current = current;
            this.history = history;
        }
    }
}
