package com.jari.app;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.provider.Settings;
import android.net.Uri;
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
    private static final String KEY_ALIAS = "jari_session_aes_v1";
    private static final String PREFERENCES = "jari_secure_session";
    private static final String TOKEN_KEY = "token";
    private static final String VERSION = "v1";
    private static final int GCM_TAG_LENGTH = 128;
    private static final String KORAIL_PACKAGE = "com.korail.talk";
    private static final String KORAIL_BOOKED_TICKETS = "korailtalk://navigation?view=bookedTicket";

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

    @PluginMethod
    public void openGoogleBrowser(PluginCall call) {
        String raw = call.getString("url", "");
        Uri uri = Uri.parse(raw);
        if (!"https".equals(uri.getScheme()) || !"accounts.google.com".equals(uri.getHost())
                || uri.getUserInfo() != null || (uri.getPort() != -1 && uri.getPort() != 443)) {
            call.reject("구글 로그인 주소가 올바르지 않아요.");
            return;
        }
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, uri);
            intent.addCategory(Intent.CATEGORY_BROWSABLE);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
            call.resolve();
        } catch (android.content.ActivityNotFoundException error) {
            call.reject("구글 로그인을 열 브라우저가 없어요.");
        }
    }

    /**
     * Payment happens in Korail's own app on its "예약 승차권 조회 · 취소" screen, where the user
     * is usually signed in already. korailtalk://navigation is not a published contract (seen in
     * 코레일+ 7.0.0's DeepLinkActivity): without the app, or once a version drops the link, the
     * web page opens; a version that keeps the link but ignores the view opens on its own home.
     */
    @PluginMethod
    public void openKorailPayment(PluginCall call) {
        Uri web = Uri.parse(call.getString("webUrl", ""));
        String host = web.getHost() == null ? "" : web.getHost();
        boolean korail = host.equals("korail.com") || host.endsWith(".korail.com")
                || host.equals("letskorail.com") || host.endsWith(".letskorail.com");
        if (!"https".equals(web.getScheme()) || !korail || web.getUserInfo() != null
                || (web.getPort() != -1 && web.getPort() != 443)) {
            call.reject("코레일 결제 주소가 올바르지 않아요.");
            return;
        }
        JSObject result = new JSObject();
        try {
            Intent app = new Intent(Intent.ACTION_VIEW, Uri.parse(KORAIL_BOOKED_TICKETS));
            app.setPackage(KORAIL_PACKAGE);
            app.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(app);
            result.put("opened", "app");
            call.resolve(result);
            return;
        } catch (android.content.ActivityNotFoundException | SecurityException missing) {
            // Not installed, or a version without the link: the web page below.
        }
        try {
            Intent browser = new Intent(Intent.ACTION_VIEW, web);
            browser.addCategory(Intent.CATEGORY_BROWSABLE);
            browser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(browser);
            result.put("opened", "web");
            call.resolve(result);
        } catch (android.content.ActivityNotFoundException error) {
            call.reject("코레일 결제 화면을 열 앱이 없어요.");
        }
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
