package com.jari.app.updates;

public enum PackageUpdateDecision {
    CURRENT,
    AVAILABLE;

    public static PackageUpdateDecision forVersionCodes(int currentVersionCode, int offeredVersionCode)
        throws PackageUpdateException {
        if (offeredVersionCode < currentVersionCode) {
            throw new PackageUpdateException("Update manifest is older than the installed app");
        }
        return offeredVersionCode == currentVersionCode ? CURRENT : AVAILABLE;
    }
}
