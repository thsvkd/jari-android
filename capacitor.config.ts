import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.teum.app",
  appName: "자리났다",
  webDir: "dist",
  // The bundled Vite assets are the only production web content. A live server
  // URL is deliberately not configured here.
  server: {
    androidScheme: "https",
    cleartext: false,
    allowNavigation: [],
  },
};

export default config;
