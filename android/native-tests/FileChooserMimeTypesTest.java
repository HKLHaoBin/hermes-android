package io.github.hermesandroid;

import java.util.Arrays;
import java.util.concurrent.atomic.AtomicInteger;

/** Dependency-free JVM policy test; no APK/Gradle/SDK build required.
 * javac -d <scratch> app/src/main/java/io/github/hermesandroid/FileChooserMimeTypes.java native-tests/FileChooserMimeTypesTest.java
 * java -cp <scratch> io.github.hermesandroid.FileChooserMimeTypesTest
 */
public final class FileChooserMimeTypesTest {
    private static int checks;

    private static void expect(String[] input, String... expected) {
        String[] actual = FileChooserMimeTypes.normalize(input, extension -> {
            switch (extension) {
                case "json": return "application/json";
                case "jpg": return "image/jpeg";
                default: return null;
            }
        });
        if (!Arrays.equals(expected, actual)) {
            throw new AssertionError(Arrays.toString(input) + " => " + Arrays.toString(actual)
                + "; expected " + Arrays.toString(expected));
        }
        checks++;
    }

    public static void main(String[] args) {
        expect(null, "*/*");
        expect(new String[0], "*/*");
        expect(new String[] {"", "  ", null}, "*/*");
        expect(new String[] {" image/* "}, "image/*");
        expect(new String[] {"IMAGE/JPEG", "image/jpeg"}, "image/jpeg");
        expect(new String[] {".zip"}, "application/zip", "application/x-zip-compressed", "application/octet-stream");
        expect(new String[] {"application/x-zip-compressed"}, "application/x-zip-compressed", "application/zip", "application/octet-stream");
        expect(new String[] {".ZIP,.json", "image/*"}, "application/zip", "application/x-zip-compressed", "application/octet-stream", "application/json", "image/*");
        expect(new String[] {".jpg", "application/pdf"}, "image/jpeg", "application/pdf");
        expect(new String[] {"image/*", "*/*"}, "*/*");
        expect(new String[] {".unknown", "image/*"}, "*/*");
        expect(new String[] {"not-a-mime-type"}, "*/*");
        expect(new String[] {"text/plain; charset=utf-8"}, "*/*");
        expect(new String[] {"*/json"}, "*/*");
        expect(new String[] {"application/vnd.example+json"}, "application/vnd.example+json");
        AtomicInteger lookups = new AtomicInteger();
        String[] zip = FileChooserMimeTypes.normalize(new String[] {".zip"}, extension -> {
            lookups.incrementAndGet();
            return null;
        });
        if (lookups.get() != 0 || !zip[0].equals("application/zip")) {
            throw new AssertionError("ZIP must work without a platform MIME mapping");
        }
        checks++;
        System.out.println("PASS: " + checks + " file chooser MIME policy checks");
    }
}
