package com.jari.app.updates;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class SigningLineage {
    private SigningLineage() {}

    public static boolean matches(
        boolean installedMultiple,
        List<String> installedCurrent,
        List<String> installedHistory,
        boolean candidateMultiple,
        List<String> candidateCurrent,
        List<String> candidateHistory
    ) {
        if (installedCurrent.isEmpty() || candidateCurrent.isEmpty()) return false;
        if (installedMultiple || candidateMultiple) {
            return installedMultiple && candidateMultiple
                && set(installedCurrent).equals(set(candidateCurrent));
        }
        if (installedCurrent.size() != 1 || candidateCurrent.size() != 1) return false;
        Set<String> installedLineage = set(installedHistory);
        Set<String> candidateLineage = set(candidateHistory);
        return candidateLineage.containsAll(installedLineage)
            && candidateLineage.contains(installedCurrent.get(0))
            && candidateLineage.contains(candidateCurrent.get(0));
    }

    private static Set<String> set(List<String> values) {
        return new HashSet<>(values);
    }
}
