package io.github.hermesandroid

import android.content.Context
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import java.util.zip.ZipInputStream

/**
 * Unpacks the relocatable Hermes runtime shipped in assets/payload/ into the app's
 * private files dir and fixes up what a zip cannot carry (symlinks, exec bits, and
 * the absolute install path baked into text files). See scripts/relocate_payload.py.
 */
class PayloadInstaller(private val context: Context) {

    /** The active slot only; user data lives elsewhere under filesDir/home. */
    val root: File
        get() = activeSlotId()?.let(::slotDir) ?: legacyRoot

    data class Recovery(val hadPendingActivation: Boolean, val restoredPayloadId: String?)

    enum class LegacyMigrationResult { NONE, MIGRATED, NO_TRUSTED_MANIFEST }

    private val filesDir: File get() = context.filesDir
    private val slotsDir: File get() = File(filesDir, "hermes-agent-slots")
    private val stateFile: File get() = File(filesDir, "hermes-agent.active.json")
    private val stateBackup: File get() = File(filesDir, "hermes-agent.active.json.bak")
    private val legacyRoot: File get() = File(filesDir, "hermes-agent")
    private val legacyMarker: File get() = File(filesDir, "hermes-agent.legacy-migration.json")

    fun hasPayload(): Boolean = try {
        context.assets.open(MANIFEST_ASSET).close()
        true
    } catch (_: IOException) {
        false
    }

    fun manifest(): JSONObject =
        context.assets.open(MANIFEST_ASSET).use { JSONObject(String(it.readBytes(), Charsets.UTF_8)) }

    fun installedId(): String? =
        readNoFollowText(root, ID_FILE)?.trim()

    fun installedManifest(): JSONObject? = try {
        readNoFollowText(root, MANIFEST_FILE)?.let { JSONObject(it) }
    } catch (_: Throwable) {
        null
    }

    fun activeSlotId(): String? = readState().optStringOrNull("active")

    fun previousGoodSlotId(): String? = readState().optStringOrNull("previous_good")

    fun rejectedPayloadId(): String? = readState().optStringOrNull("rejected")

    fun activationPending(): Boolean = readState().optBoolean("pending", false)

    fun activationState(): JSONObject = readState()

    fun legacyMigrationStatus(): String = try {
        readNoFollowText(filesDir, LEGACY_STATUS_FILE)?.let { JSONObject(it).optString("status") }.orEmpty()
    } catch (_: Throwable) {
        "unknown"
    }

    fun pruneStaleSlots() = pruneSlots()

    fun needsInstall(manifest: JSONObject): Boolean {
        val id = manifest.getString("payload_id")
        return installedId() != id || !validateSlot(root, manifest)
    }

    fun canReuseSlot(manifest: JSONObject): Boolean {
        val id = manifest.getString("payload_id")
        val current = activeSlotId()
        val candidate = slotDir(id)
        return current != id && previousGoodSlotId() == id && validateSlot(candidate, manifest)
    }

    /** A rejected APK keeps using its installed fallback, never its bundled ZIP. */
    fun selectedManifest(included: JSONObject): JSONObject {
        if (rejectedPayloadId() != included.getString("payload_id")) return included
        val restored = installedManifest() ?: throw IOException("没有可用的 previous-good manifest")
        if (restored.optString("payload_id") != activeSlotId() || needsInstall(restored)) {
            throw IOException("previous-good 运行环境不完整，拒绝用新 APK 的 ZIP 修补旧 slot")
        }
        return restored
    }

    private data class ContentEntry(
        val relative: String,
        val kind: Char,
        val file: File,
        val target: String? = null,
        val mode: Int = 0,
    )

    private fun validateSlot(slot: File, expectedManifest: JSONObject?): Boolean {
        return try {
            if (!slot.isDirectory || FileTools.isSymlink(slot)) false
            else {
                val id = readNoFollowText(slot, ID_FILE)?.trim() ?: return false
                val installed = readNoFollowText(slot, MANIFEST_FILE)?.let(::JSONObject) ?: return false
                if (installed.optString("payload_id") != id) return false
                if (expectedManifest != null && manifestDigest(installed) != manifestDigest(expectedManifest)) return false
                val integrity = readNoFollowText(slot, INTEGRITY_FILE)?.let(::JSONObject) ?: return false
                if (integrity.optInt("format", -1) != 1 || integrity.optString("payload_id") != id) return false
                if (integrity.optString("manifest_sha256") != manifestDigest(installed)) return false
                if (integrity.optString("content_sha256") != slotContentDigest(slot)) return false
                validateManifestEntries(slot, installed)
            }
        } catch (_: Throwable) {
            false
        }
    }

    private fun validateManifestEntries(slot: File, manifest: JSONObject): Boolean {
        val symlinkPaths = mutableSetOf<String>()
        manifest.optJSONArray("symlinks")?.let { links ->
            for (i in 0 until links.length()) symlinkPaths += links.getJSONArray(i).getString(0)
        }
        val launcherObject = manifest.getJSONObject("launcher")
        val launcher = launcherObject.getString("python")
        val placeholders = manifest.getJSONObject("placeholders")
        if (placeholders.optString("root") != ROOT_PLACEHOLDER ||
            placeholders.optString("prefix") != PREFIX_PLACEHOLDER ||
            !validateDeclaredPath(slot, placeholders.getString("prefix_dir"), symlinkPaths)) return false
        for (key in arrayOf("python", "repo", "site")) {
            if (!validateDeclaredPath(slot, launcherObject.getString(key), symlinkPaths)) return false
        }
        for (key in arrayOf("ld_library_path", "path")) {
            val paths = manifest.optJSONArray(key) ?: continue
            for (i in 0 until paths.length()) {
                if (!validateDeclaredPath(slot, paths.getString(i), symlinkPaths)) return false
            }
        }
        for (key in arrayOf("node", "web_dist", "cert_file")) {
            val value = manifest.optString(key).takeIf { it.isNotBlank() && it != "null" } ?: continue
            if (!validateDeclaredPath(slot, value, symlinkPaths)) return false
        }
        val executablePaths = mutableSetOf<String>()
        manifest.optJSONArray("executables")?.let { paths ->
            for (i in 0 until paths.length()) executablePaths += paths.getString(i)
        }
        for (key in arrayOf("rewrite", "executables")) {
            val paths = manifest.optJSONArray(key) ?: continue
            for (i in 0 until paths.length()) {
                val relative = paths.getString(i)
                val file = safeChild(slot, relative)
                if (!FileTools.exists(file) || FileTools.isSymlink(file)) return false
                if (key == "executables" && !hasExecuteBit(file)) return false
            }
        }
        val launcherFile = if (launcher in symlinkPaths) safeLink(slot, launcher) else safeChild(slot, launcher)
        if (launcher !in executablePaths && !hasExecuteBit(launcherFile)) return false
        val links = manifest.optJSONArray("symlinks") ?: return true
        for (i in 0 until links.length()) {
            val pair = links.getJSONArray(i)
            val link = safeLink(slot, pair.getString(0))
            if (!FileTools.isSymlink(link)) return false
            val expected = resolveInstalled(pair.getString(1), slot, manifest)
            if (!validateSymlinkTarget(slot, pair.getString(0), expected)) return false
            if (Os.readlink(link.absolutePath) != expected) return false
        }
        return true
    }

    private fun validateDeclaredPath(root: File, relative: String, symlinkPaths: Set<String>): Boolean {
        if (relative.isBlank() || relative.startsWith('/') || relative.contains('\\') ||
            relative.split('/').any { it == ".." }) return false
        if (relative in symlinkPaths) {
            val link = safeLink(root, relative)
            if (!FileTools.isSymlink(link)) return false
            return validateSymlinkTarget(root, relative, Os.readlink(link.absolutePath))
        }
        return FileTools.exists(safeChild(root, relative))
    }

    private fun hasExecuteBit(file: File): Boolean = try {
        !FileTools.isSymlink(file) && (Os.lstat(file.absolutePath).st_mode and EXECUTE_BITS) != 0
    } catch (_: Throwable) {
        false
    }

    private fun validateSymlinkTarget(root: File, linkRelative: String, target: String): Boolean {
        if (target in ALLOWED_SYSTEM_SYMLINK_TARGETS) return true
        val link = safeLink(root, linkRelative)
        val parent = link.parentFile ?: return false
        val targetFile = if (target.startsWith('/')) File(target) else File(parent, target)
        val rootCanonical = root.canonicalFile
        val targetCanonical = targetFile.canonicalFile
        val rootPath = rootCanonical.path.trimEnd(File.separatorChar) + File.separator
        return targetCanonical.path == rootCanonical.path || targetCanonical.path.startsWith(rootPath)
    }

    private fun slotContentDigest(root: File): String {
        val entries = ArrayList<ContentEntry>()
        collectContentEntries(root, root, "", entries)
        val digest = MessageDigest.getInstance("SHA-256")
        for (entry in entries.sortedBy { it.relative }) {
            when (entry.kind) {
                'D' -> digest.update("D\u0000${entry.mode.toString(8)}\u0000${entry.relative}\n".toByteArray(Charsets.UTF_8))
                'L' -> digest.update("L\u0000${entry.relative}\u0000${entry.target}\n".toByteArray(Charsets.UTF_8))
                'F' -> {
                    digest.update("F\u0000${entry.mode.toString(8)}\u0000${entry.relative}\u0000${entry.file.length()}\u0000".toByteArray(Charsets.UTF_8))
                    entry.file.inputStream().use { input ->
                        val buffer = ByteArray(256 * 1024)
                        while (true) {
                            val n = input.read(buffer)
                            if (n < 0) break
                            digest.update(buffer, 0, n)
                        }
                    }
                    digest.update('\n'.code.toByte())
                }
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
    }

    private fun collectContentEntries(root: File, current: File, relative: String, out: MutableList<ContentEntry>) {
        if (FileTools.isSymlink(current)) {
            out += ContentEntry(relative, 'L', current, Os.readlink(current.absolutePath))
            return
        }
        val mode = Os.lstat(current.absolutePath).st_mode and MODE_MASK
        if (current.isDirectory) {
            if (relative.isNotEmpty()) out += ContentEntry(relative, 'D', current, mode = mode)
            val children = current.listFiles() ?: throw IOException("无法读取 payload slot")
            for (child in children.sortedBy { it.name }) {
                if (relative.isEmpty() && child.name in METADATA_FILES) continue
                val childRelative = if (relative.isEmpty()) child.name else "$relative/${child.name}"
                collectContentEntries(root, child, childRelative, out)
            }
            return
        }
        if (!current.isFile) throw IOException("payload slot 包含不支持的文件类型")
        out += ContentEntry(relative, 'F', current, mode = mode)
    }

    private fun manifestDigest(manifest: JSONObject): String =
        sha256(canonicalJson(manifest).toByteArray(Charsets.UTF_8))

    private fun canonicalJson(value: Any?): String = when (value) {
        JSONObject.NULL, null -> "null"
        is JSONObject -> value.keys().asSequence().toList().sorted().joinToString("{", "}") { key ->
            JSONObject.quote(key) + ":" + canonicalJson(value.get(key))
        }
        is org.json.JSONArray -> (0 until value.length()).joinToString("[", "]") { canonicalJson(value.get(it)) }
        is String -> JSONObject.quote(value)
        is Number, is Boolean -> value.toString()
        else -> JSONObject.quote(value.toString())
    }

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun resolveInstalled(value: String, root: File, manifest: JSONObject): String {
        val placeholders = manifest.getJSONObject("placeholders")
        val prefix = "${root.absolutePath}/" + placeholders.optString("prefix_dir", "prefix")
        return value.replace(placeholders.getString("root"), root.absolutePath)
            .replace(placeholders.getString("prefix"), prefix)
    }

    private fun safeChild(root: File, relative: String): File {
        if (relative.isBlank() || relative.startsWith('/') || relative.contains('\\') ||
            relative.split('/').any { it == ".." }) throw IOException("unsafe payload path")
        val child = File(root, relative)
        ensureNoSymlinkAncestors(root, child.parentFile ?: root)
        val rootCanonical = root.canonicalFile
        val childCanonical = child.canonicalFile
        val rootPath = rootCanonical.path.trimEnd(File.separatorChar) + File.separator
        if (childCanonical.path != rootCanonical.path && !childCanonical.path.startsWith(rootPath)) {
            throw IOException("payload path escapes slot")
        }
        if (FileTools.isSymlink(child)) throw IOException("payload metadata path is symlink")
        return child
    }

    private fun safeLink(root: File, relative: String): File {
        if (relative.isBlank() || relative.startsWith('/') || relative.contains('\\') ||
            relative.split('/').any { it == ".." }) throw IOException("unsafe payload symlink")
        val link = File(root, relative)
        val parent = link.parentFile ?: throw IOException("payload symlink has no parent")
        ensureNoSymlinkAncestors(root, parent)
        ensureCanonicalInside(root, parent)
        return link
    }

    private fun ensureNoSymlinkAncestors(root: File, start: File) {
        val rootAbsolute = root.absoluteFile
        var cursor: File? = start.absoluteFile
        while (cursor != null) {
            if (FileTools.isSymlink(cursor!!)) throw IOException("payload path traverses symlink")
            if (cursor == rootAbsolute) return
            cursor = cursor.parentFile
        }
        throw IOException("payload path is outside root")
    }

    private fun ensureCanonicalInside(root: File, child: File) {
        if (FileTools.isSymlink(root)) throw IOException("payload root is symlink")
        val rootPath = root.canonicalFile.path.trimEnd(File.separatorChar) + File.separator
        val childPath = child.canonicalFile.path
        if (childPath != root.canonicalFile.path && !childPath.startsWith(rootPath)) {
            throw IOException("path escapes application data root")
        }
    }

    private fun readNoFollowText(root: File, relative: String): String? {
        val file = safeChild(root, relative)
        if (!FileTools.exists(file)) return null
        return file.readText(Charsets.UTF_8)
    }

    private fun readNoFollowBytes(root: File, relative: String): ByteArray? {
        val file = safeChild(root, relative)
        if (!FileTools.exists(file)) return null
        return file.readBytes()
    }

    private fun writeAtomicNoFollow(root: File, relative: String, content: String) {
        val file = safeChild(root, relative)
        if (FileTools.isSymlink(file)) throw IOException("refusing to replace symlink metadata")
        val parent = file.parentFile ?: throw IOException("metadata path has no parent")
        parent.mkdirs()
        val existingMode = if (FileTools.exists(file)) Os.lstat(file.absolutePath).st_mode and MODE_MASK else null
        val temp = File(parent, ".${file.name}.tmp-${UUID.randomUUID()}")
        if (FileTools.exists(temp)) throw IOException("metadata temp path already exists")
        FileOutputStream(temp).use { output ->
            output.write(content.toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        existingMode?.let {
            try {
                Os.chmod(temp.absolutePath, it)
            } catch (error: ErrnoException) {
                throw IOException("无法保留 payload metadata mode", error)
            }
        }
        if (FileTools.isSymlink(temp)) throw IOException("metadata temp path became symlink")
        if (FileTools.isSymlink(file)) throw IOException("metadata path became symlink")
        if (!temp.renameTo(file)) throw IOException("无法原子写入 payload metadata")
    }

    /** Convert only a legacy install carrying its own real manifest. */
    fun migrateLegacy(cancelled: () -> Boolean = { false }): LegacyMigrationResult {
        checkCancelled(cancelled)
        if (FileTools.exists(legacyMarker)) {
            finishLegacyMigration(cancelled)
            return LegacyMigrationResult.MIGRATED
        }
        if (activeSlotId() != null || !legacyRoot.isDirectory || FileTools.isSymlink(legacyRoot)) {
            return LegacyMigrationResult.NONE
        }
        val legacyManifest = legacyManifestOrNull()
        if (legacyManifest == null) {
            recordLegacyNoFallback()
            return LegacyMigrationResult.NO_TRUSTED_MANIFEST
        }
        val id = legacyManifest.optString("payload_id").takeIf { isSafeSlotId(it) }
        if (id == null) {
            recordLegacyNoFallback()
            return LegacyMigrationResult.NO_TRUSTED_MANIFEST
        }
        validateSlotId(id)
        if (FileTools.isSymlink(slotsDir)) throw IOException("payload slots root is symlink")
        slotsDir.mkdirs()
        if (!slotsDir.isDirectory) throw IOException("无法创建 payload slots root")
        val destination = slotDir(id)
        if (FileTools.exists(destination)) throw IOException("旧运行环境 slot 已存在，拒绝覆盖")
        writeLegacyMarker(id, legacyManifest)
        checkCancelled(cancelled)
        finishLegacyMigration(cancelled)
        return LegacyMigrationResult.MIGRATED
    }

    private fun recordLegacyNoFallback() {
        writeAtomicNoFollow(
            filesDir,
            LEGACY_STATUS_FILE,
            JSONObject()
                .put("format", 1)
                .put("status", "no-trusted-manifest")
                .put("source", "files/hermes-agent")
                .put("reason", "legacy install has no verifiable manifest or sidecar; current APK manifest is not used"),
        )
    }

    private fun legacyManifestOrNull(): JSONObject? {
        return try {
            val installedId = readNoFollowText(legacyRoot, ID_FILE)?.trim()
                ?.takeIf { isSafeSlotId(it) } ?: return null
            val sidecars = listOf(MANIFEST_FILE, "app/payload-manifest.json", "app/payload_manifest.json")
            val candidates = ArrayList<JSONObject>()
            for (relative in sidecars) {
                readNoFollowText(legacyRoot, relative)?.let { candidates += JSONObject(it) }
            }
            readNoFollowText(legacyRoot, "app/install-stamp.json")?.let { stampText ->
                val stamp = JSONObject(stampText)
                stamp.optJSONObject("payload_manifest")?.let(candidates::add)
                stamp.optJSONObject("manifest")?.let(candidates::add)
            }
            candidates.firstOrNull { manifest ->
                manifest.optString("payload_id") == installedId &&
                    manifest.has("placeholders") && manifest.has("launcher") &&
                    manifest.has("rewrite") && manifest.has("symlinks") &&
                    manifest.has("executables") && validateManifestEntries(legacyRoot, manifest)
            }
        } catch (_: Throwable) {
            null
        }
    }

    private fun writeLegacyMarker(id: String, legacyManifest: JSONObject) {
        writeAtomicNoFollow(
            filesDir,
            legacyMarker.name,
            JSONObject().put("format", 2).put("slot", id).put("manifest", legacyManifest).toString(),
        )
    }

    private fun finishLegacyMigration(cancelled: () -> Boolean) {
        checkCancelled(cancelled)
        val marker = try {
            JSONObject(readNoFollowText(filesDir, legacyMarker.name) ?: throw IOException("迁移标记不存在"))
        } catch (t: Throwable) {
            throw IOException("旧运行环境迁移标记损坏", t)
        }
        if (marker.optInt("format", -1) != 2) throw IOException("旧运行环境迁移标记版本无效")
        val id = marker.optString("slot")
        validateSlotId(id)
        val legacyManifest = marker.optJSONObject("manifest") ?: return
        val stateActive = activeSlotId()
        val destination = slotDir(id)
        if (stateActive != null) {
            if (stateActive == id && validateSlot(destination, legacyManifest)) {
                if (FileTools.isSymlink(legacyMarker)) throw IOException("迁移标记是 symlink")
                legacyMarker.delete()
                return
            }
            throw IOException("旧运行环境迁移与现有 active slot 冲突")
        }
        ensureCanonicalInside(filesDir, legacyRoot)
        ensureCanonicalInside(filesDir, destination)
        val legacyPath = legacyRoot.absolutePath
        if (FileTools.exists(legacyRoot) && FileTools.exists(destination)) {
            throw IOException("旧运行环境迁移同时存在两个源目录")
        }
        checkCancelled(cancelled)
        if (!FileTools.exists(destination) && !legacyRoot.renameTo(destination)) {
            throw IOException("无法迁移旧运行环境到 slot")
        }
        val priorState = readState()
        val migratedManifest: JSONObject
        try {
            rewriteLegacyPaths(destination, legacyPath, legacyManifest)
            migratedManifest = JSONObject(legacyManifest.toString()).put("legacy_migrated", true)
            writeAtomicNoFollow(destination, ID_FILE, id)
            writeAtomicNoFollow(destination, MANIFEST_FILE, migratedManifest.toString())
            writeAtomicNoFollow(
                destination,
                INTEGRITY_FILE,
                JSONObject().put("format", 1)
                    .put("payload_id", id)
                    .put("manifest_sha256", manifestDigest(migratedManifest))
                    .put("content_sha256", slotContentDigest(destination))
                    .toString(),
            )
            if (!validateSlot(destination, migratedManifest)) throw IOException("legacy slot integrity validation failed")
        } catch (_: Throwable) {
            val quarantine = File(filesDir, "hermes-agent-legacy-unusable-${UUID.randomUUID()}")
            if (FileTools.exists(destination)) destination.renameTo(quarantine)
            if (FileTools.isSymlink(legacyMarker)) throw IOException("迁移标记是 symlink")
            legacyMarker.delete()
            return
        }
        checkCancelled(cancelled)
        writeState(id, null, pending = false, generation = priorState.optLong("generation", 0L) + 1L)
        if (FileTools.isSymlink(legacyMarker)) throw IOException("迁移标记是 symlink")
        legacyMarker.delete()
    }

    private fun rewriteLegacyPaths(destination: File, legacyPath: String, manifest: JSONObject) {
        ensureCanonicalInside(filesDir, destination)
        val placeholders = manifest.getJSONObject("placeholders")
        val rootPh = placeholders.optString("root")
        val prefixPh = placeholders.optString("prefix")
        val prefixDir = "$destination/" + placeholders.optString("prefix_dir", "prefix")
        fun resolve(value: String): String {
            var result = value.replace(legacyPath, destination.absolutePath)
            if (rootPh.isNotEmpty()) result = result.replace(rootPh, destination.absolutePath)
            if (prefixPh.isNotEmpty()) result = result.replace(prefixPh, prefixDir)
            return result
        }
        val rewrite = manifest.optJSONArray("rewrite")
        if (rewrite != null) {
            for (i in 0 until rewrite.length()) {
                val relative = rewrite.getString(i)
                val oldBytes = readNoFollowBytes(destination, relative) ?: continue
                val oldText = String(oldBytes, Charsets.ISO_8859_1)
                val newText = oldText.replace(legacyPath, destination.absolutePath)
                if (newText != oldText) writeAtomicNoFollow(destination, relative, newText)
            }
        }
        val links = manifest.optJSONArray("symlinks")
        if (links != null) {
            for (i in 0 until links.length()) {
                val pair = links.getJSONArray(i)
                val link = safeLink(destination, pair.getString(0))
                val expected = resolve(pair.getString(1))
                val current = try { Os.readlink(link.absolutePath) } catch (_: Throwable) { null }
                if (current != expected) {
                    if (!validateSymlinkTarget(destination, pair.getString(0), expected)) {
                        throw IOException("legacy symlink target escapes payload destination")
                    }
                    if (FileTools.exists(link)) FileTools.deleteTree(link)
                    link.parentFile?.mkdirs()
                    ensureCanonicalInside(filesDir, link.parentFile ?: destination)
                    Os.symlink(expected, link.absolutePath)
                }
            }
        }
    }

    /** Recover an activation interrupted before the candidate became healthy. */
    fun recoverPending(beforeDiscard: (String) -> Unit): Recovery {
        if (!activationPending()) return Recovery(false, null)
        return Recovery(true, rollback(beforeDiscard))
    }

    /** Quiescence must succeed before state changes or failed-slot deletion. */
    fun rollback(beforeDiscard: (String) -> Unit, rejectFailed: Boolean = true): String? {
        val state = readState()
        if (!state.optBoolean("pending", false)) return activeSlotId()
        val previous = state.optStringOrNull("previous_good")
        val failed = state.optStringOrNull("active")
        val restored = previous?.takeIf { it != failed && validateSlot(slotDir(it), null) }
        val retained = failed?.takeIf {
            state.optBoolean("reused_previous_good", false) && it != restored && validateSlot(slotDir(it), null)
        }
        failed?.let { beforeDiscard(slotDir(it).absolutePath) }
        writeState(
            restored,
            retained,
            pending = false,
            generation = state.optLong("generation", 0L) + 1L,
            rejected = failed.takeIf { rejectFailed },
            reusedPreviousGood = false,
        )
        failed?.takeIf { it != restored && it != retained }?.let { FileTools.deleteTree(slotDir(it)) }
        pruneSlots()
        return restored
    }

    /** Mark the active candidate healthy while retaining previous-good for rollback. */
    fun markHealthy() {
        val state = readState()
        if (state.optBoolean("pending", false)) {
            writeState(
                state.optStringOrNull("active"),
                state.optStringOrNull("previous_good"),
                pending = false,
                generation = state.optLong("generation", 0L),
                rejected = null,
                reusedPreviousGood = false,
            )
        }
        pruneSlots()
    }

    /** Blocking; call from a worker thread. [progress] gets 0..1 and a short label. */
    class InstallCancelledException : IOException("payload installation cancelled")

    fun install(
        manifest: JSONObject,
        progress: (Float, String) -> Unit,
        cancelled: () -> Boolean = { false },
        beforeDiscard: (String) -> Unit = {},
    ) {
        val payloadId = manifest.getString("payload_id")
        validateSlotId(payloadId)
        if (payloadId == rejectedPayloadId()) throw IOException("refusing rejected payload: $payloadId")
        checkCancelled(cancelled)
        migrateLegacy(cancelled)
        verifyAssetDigest(manifest, cancelled)
        if (FileTools.isSymlink(slotsDir)) throw IOException("payload slots root is symlink")
        slotsDir.mkdirs()
        if (!slotsDir.isDirectory) throw IOException("无法创建 payload slots root")
        pruneSlots()
        val candidate = slotDir(payloadId)
        var current = activeSlotId()
        var previous = previousGoodSlotId()
        val activeId = current
        if (activeId != null) {
            val expectedActiveManifest = activeId.takeIf { it == payloadId }?.let { manifest }
            if (!validateSlot(slotDir(activeId), expectedActiveManifest)) {
                quarantineInvalidActive(activeId, beforeDiscard, rejected = activeId.takeIf { it == payloadId })
            }
            current = activeSlotId()
            previous = previousGoodSlotId()
        }
        if (previous == payloadId && !validateSlot(candidate, manifest)) {
            quarantineInvalidPreviousGood(payloadId, beforeDiscard)
            current = activeSlotId()
            previous = previousGoodSlotId()
        }
        if (current == payloadId && validateSlot(candidate, manifest)) {
            progress(1f, "运行环境已就绪")
            return
        }
        if (FileTools.exists(candidate) && previous == payloadId && current != payloadId) {
            checkCancelled(cancelled)
            activateExisting(payloadId, current, manifest, cancelled, reusedPreviousGood = true)
            progress(1f, "运行环境已就绪")
            return
        }
        if (FileTools.exists(candidate) && current != payloadId) FileTools.deleteTree(candidate)
        val staging = File(slotsDir, ".staging-$payloadId-${UUID.randomUUID()}")
        FileTools.deleteTree(staging)
        staging.mkdirs()
        if (!staging.isDirectory || FileTools.isSymlink(staging)) throw IOException("无法创建 payload staging")

        val total = manifest.optLong("total_bytes", 1L).coerceAtLeast(1L)
        var written = 0L
        var lastReport = 0L
        val buffer = ByteArray(256 * 1024)
        progress(0f, "解压运行环境")

        try {
            ZipInputStream(BufferedInputStream(context.assets.open(ZIP_ASSET), 1 shl 20)).use { zip ->
                while (true) {
                    checkCancelled(cancelled)
                    val entry = zip.nextEntry ?: break
                    val name = entry.name
                    checkEntryName(name)
                    val out = safeChild(staging, name)
                    if (entry.isDirectory) {
                        out.mkdirs()
                        continue
                    }
                    out.parentFile?.mkdirs()
                    FileOutputStream(out).use { fos ->
                        while (true) {
                            val n = zip.read(buffer)
                            if (n < 0) break
                            fos.write(buffer, 0, n)
                            written += n
                            checkCancelled(cancelled)
                        }
                    }
                    if (written - lastReport > 4L * 1024 * 1024) {
                        lastReport = written
                        progress((written.toFloat() / total).coerceAtMost(0.97f), "解压运行环境")
                    }
                }
            }

            // Paths inside the payload must point at the candidate's FINAL slot path.
            val finalRoot = candidate.absolutePath
            val placeholders = manifest.getJSONObject("placeholders")
            val rootPh = placeholders.getString("root")
            val prefixPh = placeholders.getString("prefix")
            val prefixDir = "$finalRoot/" + placeholders.optString("prefix_dir", "prefix")
            fun resolve(value: String) = value.replace(rootPh, finalRoot).replace(prefixPh, prefixDir)

            progress(0.97f, "配置运行环境")
            val rewrite = manifest.getJSONArray("rewrite")
            for (i in 0 until rewrite.length()) {
                checkCancelled(cancelled)
                val relative = rewrite.getString(i)
                val bytes = readNoFollowBytes(staging, relative) ?: continue
                val text = String(bytes, Charsets.ISO_8859_1)
                val fixed = resolve(text)
                if (fixed != text) writeAtomicNoFollow(staging, relative, fixed)
            }

            val links = manifest.getJSONArray("symlinks")
            for (i in 0 until links.length()) {
                checkCancelled(cancelled)
                val pair = links.getJSONArray(i)
                val link = safeLink(staging, pair.getString(0))
                val target = resolve(pair.getString(1))
                if (!validateSymlinkTarget(candidate, pair.getString(0), target)) {
                    throw IOException("symlink target escapes payload destination")
                }
                link.parentFile?.mkdirs()
                if (FileTools.exists(link)) FileTools.deleteTree(link)
                try {
                    Os.symlink(target, link.absolutePath)
                } catch (e: ErrnoException) {
                    throw IOException("symlink ${pair.getString(0)} -> $target failed: ${e.message}", e)
                }
            }

            val executables = manifest.getJSONArray("executables")
            for (i in 0 until executables.length()) {
                checkCancelled(cancelled)
                val file = safeChild(staging, executables.getString(i))
                if (FileTools.exists(file) && !FileTools.isSymlink(file)) {
                    try {
                        Os.chmod(file.absolutePath, "755".toInt(8))
                    } catch (_: ErrnoException) {
                        file.setExecutable(true, false)
                    }
                }
            }

            val launcher = safeChild(staging, manifest.getJSONObject("launcher").getString("python"))
            if (!FileTools.exists(launcher)) throw IOException("candidate launcher missing")
            writeAtomicNoFollow(staging, ID_FILE, payloadId)
            writeAtomicNoFollow(staging, MANIFEST_FILE, manifest.toString())
            writeAtomicNoFollow(
                staging,
                INTEGRITY_FILE,
                JSONObject().put("format", 1)
                    .put("payload_id", payloadId)
                    .put("manifest_sha256", manifestDigest(manifest))
                    .put("content_sha256", slotContentDigest(staging))
                    .toString(),
            )
            checkCancelled(cancelled)
            if (!staging.renameTo(candidate)) throw IOException("无法保存新的运行环境 slot")
            if (cancelled()) {
                FileTools.deleteTree(candidate)
                throw InstallCancelledException()
            }
            if (!validateSlot(candidate, manifest)) {
                FileTools.deleteTree(candidate)
                throw IOException("candidate slot integrity validation failed")
            }
            activateExisting(payloadId, current, manifest, cancelled)
            progress(1f, "运行环境已就绪")
        } catch (t: Throwable) {
            FileTools.deleteTree(staging)
            throw t
        }
    }

    private fun quarantineInvalidActive(
        payloadId: String,
        beforeDiscard: (String) -> Unit,
        rejected: String? = null,
    ) {
        val state = readState()
        if (state.optStringOrNull("active") != payloadId) return
        val previous = state.optStringOrNull("previous_good")
            ?.takeIf { validateSlot(slotDir(it), null) }
        beforeDiscard(slotDir(payloadId).absolutePath)
        writeState(
            previous,
            null,
            pending = false,
            generation = state.optLong("generation", 0L) + 1L,
            rejected = rejected,
        )
        FileTools.deleteTree(slotDir(payloadId))
        pruneSlots()
    }

    private fun quarantineInvalidPreviousGood(payloadId: String, beforeDiscard: (String) -> Unit) {
        val state = readState()
        if (state.optStringOrNull("previous_good") != payloadId) return
        beforeDiscard(slotDir(payloadId).absolutePath)
        writeState(
            state.optStringOrNull("active"),
            null,
            pending = false,
            generation = state.optLong("generation", 0L) + 1L,
            rejected = state.optStringOrNull("rejected"),
        )
        FileTools.deleteTree(slotDir(payloadId))
        pruneSlots()
    }

    private fun activateExisting(
        payloadId: String,
        current: String?,
        manifest: JSONObject,
        cancelled: () -> Boolean = { false },
        reusedPreviousGood: Boolean = false,
    ) {
        checkCancelled(cancelled)
        if (!validateSlot(slotDir(payloadId), manifest)) throw IOException("refusing to activate unverified candidate")
        val state = readState()
        val stateActive = state.optStringOrNull("active")
        if (stateActive != current) throw IOException("active slot changed during activation")
        if (stateActive != null && !validateSlot(slotDir(stateActive), null)) {
            throw IOException("refusing to retain unverified active slot")
        }
        val retained = state.optStringOrNull("previous_good")?.takeIf {
            it != payloadId && validateSlot(slotDir(it), null)
        }
        val previous = stateActive?.takeIf { it != payloadId } ?: retained
        writeState(
            payloadId,
            previous,
            pending = true,
            generation = state.optLong("generation", 0L) + 1L,
            rejected = null,
            reusedPreviousGood = reusedPreviousGood,
        )
        checkCancelled(cancelled)
    }

    private fun checkCancelled(cancelled: () -> Boolean) {
        if (cancelled()) throw InstallCancelledException()
    }

    private fun ensureSlotsRoot() {
        if (FileTools.isSymlink(slotsDir)) throw IOException("payload slots root is symlink")
        ensureCanonicalInside(filesDir, slotsDir)
    }

    private fun slotDir(id: String): File {
        ensureSlotsRoot()
        return File(slotsDir, id)
    }

    private fun validateSlotId(id: String) {
        if (!isSafeSlotId(id)) throw IOException("unsafe payload slot id")
    }

    private fun isSafeSlotId(id: String): Boolean =
        id.isNotBlank() && id != "." && id != ".." && !id.contains('/') && !id.contains('\\')

    private fun readState(): JSONObject {
        val candidates = listOf(stateFile, stateBackup)
        for (file in candidates) {
            if (FileTools.isSymlink(file)) throw IOException("运行环境状态文件是 symlink")
            if (!FileTools.exists(file)) continue
            try {
                return parseState(file)
            } catch (_: Throwable) {
                // Try the backup state before declaring no active slot.
            }
        }
        if (candidates.any { FileTools.exists(it) }) throw IOException("无法读取运行环境状态，拒绝删除或覆盖已有 slot")
        return JSONObject().put("format", 1).put("generation", 0).put("pending", false)
            .put("reused_previous_good", false)
    }

    private fun parseState(file: File): JSONObject {
        val value = JSONObject(readNoFollowText(filesDir, file.name) ?: throw IOException("state file missing"))
        if (value.getInt("format") != 1 || value.getLong("generation") < 0L ||
            value.get("pending") !is Boolean || !value.has("active") || !value.has("previous_good") ||
            (value.has("reused_previous_good") && value.get("reused_previous_good") !is Boolean)) {
            throw IOException("invalid payload state")
        }
        value.optStringOrNull("active")?.let(::validateSlotId)
        value.optStringOrNull("previous_good")?.let(::validateSlotId)
        value.optStringOrNull("rejected")?.let(::validateSlotId)
        return value
    }

    private fun writeState(
        active: String?,
        previousGood: String?,
        pending: Boolean,
        generation: Long,
        rejected: String? = null,
        reusedPreviousGood: Boolean = false,
    ) {
        active?.let(::validateSlotId)
        previousGood?.let(::validateSlotId)
        rejected?.let(::validateSlotId)
        filesDir.mkdirs()
        val value = JSONObject().apply {
            put("format", 1)
            put("generation", generation)
            put("active", active ?: JSONObject.NULL)
            put("previous_good", previousGood ?: JSONObject.NULL)
            put("pending", pending)
            put("rejected", rejected ?: JSONObject.NULL)
            put("reused_previous_good", reusedPreviousGood)
        }
        val temp = File(filesDir, "hermes-agent.active.json.tmp")
        if (FileTools.isSymlink(stateFile) || FileTools.isSymlink(stateBackup) || FileTools.isSymlink(temp)) {
            throw IOException("拒绝写入 symlink state metadata")
        }
        FileOutputStream(temp).use { output ->
            output.write(value.toString().toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        var prior: JSONObject? = null
        for (file in listOf(stateFile, stateBackup)) {
            if (FileTools.isSymlink(file) || !FileTools.exists(file)) continue
            try {
                prior = parseState(file)
                break
            } catch (_: Throwable) {
            }
        }
        prior?.let { writeAtomicNoFollow(filesDir, stateBackup.name, it.toString()) }
        if (FileTools.isSymlink(stateFile)) throw IOException("state path became symlink")
        if (!temp.renameTo(stateFile)) throw IOException("无法原子写入运行环境状态")
    }

    /** Keep only the active and previous-good slots; remove failed/stale candidates. */
    private fun pruneSlots() {
        val state = readState()
        ensureSlotsRoot()
        val keep = setOfNotNull(state.optStringOrNull("active"), state.optStringOrNull("previous_good"))
        slotsDir.listFiles()?.forEach { child ->
            if (child.name !in keep) {
                FileTools.deleteTree(child)
                if (FileTools.exists(child)) throw IOException("无法清理旧运行环境 slot：${child.name}")
            }
        }
    }

    private fun verifyAssetDigest(manifest: JSONObject, cancelled: () -> Boolean = { false }) {
        val expected = manifest.optJSONObject("payload")?.optString("zip_sha256").orEmpty()
        if (expected.isBlank()) return // format 1 APKs remain readable during upgrade.
        val digest = MessageDigest.getInstance("SHA-256")
        context.assets.open(ZIP_ASSET).use { input ->
            val buffer = ByteArray(256 * 1024)
            while (true) {
                checkCancelled(cancelled)
                val n = input.read(buffer)
                if (n < 0) break
                digest.update(buffer, 0, n)
            }
        }
        val actual = digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
        if (!actual.equals(expected, ignoreCase = true)) throw IOException("payload zip SHA-256 校验失败")
    }

    private fun checkEntryName(name: String) {
        if (name.startsWith("/") || name.contains('\\') || name.split('/').any { it == ".." }) {
            throw IOException("unsafe payload entry: $name")
        }
    }

    companion object {
        const val MANIFEST_ASSET = "payload/payload.json"
        const val ZIP_ASSET = "payload/payload.zip"
        private const val ID_FILE = ".payload_id"
        private const val MANIFEST_FILE = ".payload_manifest.json"
        private const val INTEGRITY_FILE = ".payload_integrity.json"
        private const val LEGACY_STATUS_FILE = "hermes-agent.legacy-status.json"
        private const val ROOT_PLACEHOLDER = "@@HERMES_ROOT@@"
        private const val PREFIX_PLACEHOLDER = "@@TERMUX_PREFIX@@"
        private const val MODE_MASK = 0x0fff
        private const val EXECUTE_BITS = 0x49
        private val ALLOWED_SYSTEM_SYMLINK_TARGETS = setOf("/system/bin/sh", "/system/bin/env")
        private val METADATA_FILES = setOf(ID_FILE, MANIFEST_FILE, INTEGRITY_FILE)
    }
}

private fun JSONObject.optStringOrNull(name: String): String? =
    if (!has(name) || isNull(name)) null else optString(name).takeIf { it.isNotBlank() }


object FileTools {
    /** lstat-based existence check that also sees dangling symlinks. */
    fun exists(file: File): Boolean = try {
        Os.lstat(file.absolutePath)
        true
    } catch (_: ErrnoException) {
        false
    }

    fun isSymlink(file: File): Boolean = try {
        OsConstants.S_ISLNK(Os.lstat(file.absolutePath).st_mode)
    } catch (_: ErrnoException) {
        false
    }

    /** Recursive delete that never follows symlinks (File.deleteRecursively would). */
    fun deleteTree(file: File) {
        if (!exists(file)) return
        if (!isSymlink(file) && file.isDirectory) {
            file.listFiles()?.forEach { deleteTree(it) }
        }
        file.delete()
    }
}
