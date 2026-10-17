package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class OtaUpdateDecisionTest {
    @Test
    public void reportsCurrentOnlyWhenManifestMatchesActiveSequence() {
        assertEquals(OtaUpdateDecision.CURRENT, OtaUpdateDecision.forManifest("4.21.0", "4.21.0", 22, 22, 23));
    }

    @Test
    public void blocksASequenceThatPreviouslyFailedAndRolledBack() {
        assertEquals(OtaUpdateDecision.BLOCKED_REPLAY, OtaUpdateDecision.forManifest("4.21.0", "4.21.0", 23, 22, 23));
        assertEquals(OtaUpdateDecision.BLOCKED_REPLAY, OtaUpdateDecision.forManifest("4.21.0", "4.21.0", 21, 22, 23));
    }

    @Test
    public void distinguishesAvailableAndIncompatibleUpdates() {
        assertEquals(OtaUpdateDecision.AVAILABLE, OtaUpdateDecision.forManifest("4.21.0", "4.21.0", 24, 22, 23));
        assertEquals(OtaUpdateDecision.INCOMPATIBLE, OtaUpdateDecision.forManifest("4.21.0", "4.22.0", 24, 22, 23));
    }
}
