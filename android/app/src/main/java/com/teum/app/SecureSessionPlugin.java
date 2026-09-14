package com.teum.app;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.provider.Settings;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Stores only AES-GCM ciphertext in private SharedPreferences. The AES key is
 * non-exportable and generated in Android Keystore; session plaintext never
 * reaches Capacitor Preferences or a WebView storage API.
 */
@CapacitorPlugin(name = "SecureSession")
public class SecureSessionPlugin extends Plugin {
    private static final String KEY_ALIAS = "teum_session_aes_v1";
    private static final String PREFERENCES = "teum_secure_session";
    private static final String TOKEN_KEY = "token";
    private static final String VERSION = "v1";
    private static final int GCM_TAG_LENGTH = 128;

    @PluginMethod
    public void read(PluginCall call) {
        JSObject result = new JSObject();
        String encodedToken = preferences().getString(TOKEN_KEY, null);
        if (encodedToken == null) {
            result.put("token", JSObject.NULL);
            call.resolve(result);
            return;
        }

        try {
            result.put("token", decrypt(encodedToken));
            call.resolve(result);
        } catch (GeneralSecurityException | IllegalArgumentException error) {
            // A reset/invalidated Keystore key must invalidate the session,
            // rather than leaving an unreadable token or falling back to text.
            preferences().edit().remove(TOKEN_KEY).apply();
            result.put("token", JSObject.NULL);
            call.resolve(result);
        }
    }

    @PluginMethod
    public void write(PluginCall call) {
        String token = call.getString("token");
        if (token == null || token.isEmpty()) {
            call.reject("A session token is required");
            return;
        }

        try {
            preferences().edit().putString(TOKEN_KEY, encrypt(token)).apply();
            call.resolve();
        } catch (GeneralSecurityException error) {
            call.reject("Secure session storage is unavailable", error);
        }
    }

    @PluginMethod
    public void clear(PluginCall call) {
        preferences().edit().remove(TOKEN_KEY).apply();
        call.resolve();
    }

    @PluginMethod
    public void pushConfigured(PluginCall call) {
        String packageName = getContext().getPackageName();
        int resourceId = getContext().getResources().getIdentifier("google_app_id", "string", packageName);
        boolean configured = resourceId != 0 && !getContext().getString(resourceId).trim().isEmpty();
        JSObject result = new JSObject();
        result.put("configured", configured);
        call.resolve(result);
    }

    @PluginMethod
    public void openNotificationSettings(PluginCall call) {
        Intent intent = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
        intent.putExtra(Settings.EXTRA_APP_PACKAGE, getContext().getPackageName());
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(intent);
        call.resolve();
    }

    private SharedPreferences preferences() {
        return getContext().getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
    }

    private String encrypt(String token) throws GeneralSecurityException {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] ciphertext = cipher.doFinal(token.getBytes(StandardCharsets.UTF_8));
        return VERSION
            + ":"
            + Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP)
            + ":"
            + Base64.encodeToString(ciphertext, Base64.NO_WRAP);
    }

    private String decrypt(String encodedToken) throws GeneralSecurityException {
        String[] components = encodedToken.split(":", -1);
        if (components.length != 3 || !VERSION.equals(components[0])) {
            throw new GeneralSecurityException("Unknown secure session format");
        }

        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(
            Cipher.DECRYPT_MODE,
            key(),
            new GCMParameterSpec(GCM_TAG_LENGTH, Base64.decode(components[1], Base64.NO_WRAP))
        );
        return new String(
            cipher.doFinal(Base64.decode(components[2], Base64.NO_WRAP)),
            StandardCharsets.UTF_8
        );
    }

    private SecretKey key() throws GeneralSecurityException {
        KeyStore keyStore = KeyStore.getInstance("AndroidKeyStore");
        try {
            keyStore.load(null);
        } catch (java.io.IOException error) {
            throw new GeneralSecurityException("Cannot load Android Keystore", error);
        }
        if (keyStore.containsAlias(KEY_ALIAS)) {
            return ((KeyStore.SecretKeyEntry) keyStore.getEntry(KEY_ALIAS, null)).getSecretKey();
        }

        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(
            new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
            )
                .setKeySize(256)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build()
        );
        return generator.generateKey();
    }
}
