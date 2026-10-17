import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "dev.thsvkd.jari",
  appName: "자리났다",
  webDir: "dist",
  // Production starts from bundled Vite assets. AppUpdates may select a signed,
  // verified local OTA directory; a live server URL is never configured here.
  server: {
    androidScheme: "https",
    cleartext: false,
    allowNavigation: [],
  },
};

export default config;
