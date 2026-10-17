package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import org.junit.Test;

public class PackageUpdateDecisionTest {
    @Test
    public void reportsOnlyStrictlyNewerPackagesAsAvailable() throws Exception {
        assertEquals(PackageUpdateDecision.CURRENT, PackageUpdateDecision.forVersionCodes(42201, 42201));
        assertEquals(PackageUpdateDecision.AVAILABLE, PackageUpdateDecision.forVersionCodes(42201, 42300));
    }

    @Test
    public void rejectsAStaleManifestInsteadOfCallingItCurrent() {
        assertThrows(PackageUpdateException.class, () -> PackageUpdateDecision.forVersionCodes(42201, 42200));
    }
}
