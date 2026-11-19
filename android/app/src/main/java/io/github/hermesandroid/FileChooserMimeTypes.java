package io.github.hermesandroid;

import java.util.LinkedHashSet;
import java.util.Locale;
import java.util.Set;
import java.util.function.Function;

/** HTML accept hints are not necessarily MIME types (for example, ".zip,.json"). */
final class FileChooserMimeTypes {
    private FileChooserMimeTypes() {}

    static String[] normalize(String[] acceptTypes, Function<String, String> extensionMime) {
        Set<String> types = new LinkedHashSet<>();
        if (acceptTypes == null) return new String[] {"*/*"};
        for (String group : acceptTypes) {
            if (group == null) continue;
            for (String raw : group.split(",")) {
                String type = raw.trim().toLowerCase(Locale.ROOT);
                if (type.isEmpty()) continue;
                if (type.startsWith(".")) {
                    String extension = type.substring(1);
                    type = extension.equals("zip") ? "application/zip" : extensionMime.apply(extension);
                    // An unknown hint must not hide otherwise selectable documents.
                    if (type == null || type.isEmpty()) return new String[] {"*/*"};
                }
                if (type.equals("*/*")) return new String[] {"*/*"};
                if (!type.matches("[a-z0-9!#$&^_.+\\-]+/(?:[a-z0-9!#$&^_.+\\-]+|\\*)")) {
                    return new String[] {"*/*"};
                }
                types.add(type);
                if (type.equals("application/zip") || type.equals("application/x-zip-compressed")) {
                    // DocumentsProviders disagree on ZIP metadata; octet-stream is common.
                    types.add("application/zip");
                    types.add("application/x-zip-compressed");
                    types.add("application/octet-stream");
                }
            }
        }
        return types.isEmpty() ? new String[] {"*/*"} : types.toArray(new String[0]);
    }
}
