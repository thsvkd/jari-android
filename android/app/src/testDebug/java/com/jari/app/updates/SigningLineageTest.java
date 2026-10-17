package com.jari.app.updates;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.Arrays;
import java.util.Collections;
import org.junit.Test;

public class SigningLineageTest {
    @Test
    public void acceptsTheSameSingleSignerAndANewerProvenLineage() {
        assertTrue(SigningLineage.matches(
            false, Collections.singletonList("b"), Arrays.asList("a", "b"),
            false, Collections.singletonList("b"), Arrays.asList("a", "b")
        ));
        assertTrue(SigningLineage.matches(
            false, Collections.singletonList("b"), Arrays.asList("a", "b"),
            false, Collections.singletonList("c"), Arrays.asList("a", "b", "c")
        ));
    }

    @Test
    public void rejectsOldUnprovenOrDifferentSingleSigners() {
        assertFalse(SigningLineage.matches(
            false, Collections.singletonList("b"), Arrays.asList("a", "b"),
            false, Collections.singletonList("a"), Collections.singletonList("a")
        ));
        assertFalse(SigningLineage.matches(
            false, Collections.singletonList("b"), Arrays.asList("a", "b"),
            false, Collections.singletonList("x"), Arrays.asList("a", "x")
        ));
    }

    @Test
    public void multiSignerPackagesRequireTheExactCurrentSignerSet() {
        assertTrue(SigningLineage.matches(
            true, Arrays.asList("a", "b"), Arrays.asList("a", "b"),
            true, Arrays.asList("b", "a"), Arrays.asList("b", "a")
        ));
        assertFalse(SigningLineage.matches(
            true, Arrays.asList("a", "b"), Arrays.asList("a", "b"),
            true, Arrays.asList("a", "c"), Arrays.asList("a", "c")
        ));
    }
}
