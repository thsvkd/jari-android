package com.jari.app.updates;

public enum OtaUpdateDecision {
    CURRENT,
    AVAILABLE,
    INCOMPATIBLE,
    BLOCKED_REPLAY;

    public static OtaUpdateDecision forManifest(
        String nativeVersion,
        String requiredNativeVersion,
        long sequence,
        long activeSequence,
        long highestSequence
    ) {
        if (!nativeVersion.equals(requiredNativeVersion)) return INCOMPATIBLE;
        if (sequence == activeSequence) return CURRENT;
        if (sequence <= highestSequence) return BLOCKED_REPLAY;
        return AVAILABLE;
    }
}
