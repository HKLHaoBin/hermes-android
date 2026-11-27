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
        File(root, ID_FILE).takeIf { it.isFile }?.readText()?.trim()

    fun installedManifest(): JSONObject? = try {
        File(root, MANIFEST_FILE).takeIf { it.isFile }?.let { JSONObject(it.readText()) }
    } catch (_: Throwable) {
        null
    }

    fun activeSlotId(): String? = readState().optStringOrNull("active")

    fun previousGoodSlotId(): String? = readState().optStringOrNull("previous_good")

    fun rejectedPayloadId(): String? = readState().optStringOrNull("rejected")

    fun activationPending(): Boolean = readState().optBoolean("pending", false)

    fun activationState(): JSONObject = readState()

    fun pruneStaleSlots() = pruneSlots()

    fun needsInstall(manifest: JSONObject): Boolean {
        val id = manifest.getString("payload_id")
        return installedId() != id || !File(root, manifest.getJSONObject("launcher").getString("python")).exists()
    }

    fun canReuseSlot(manifest: JSONObject): Boolean {
        val id = manifest.getString("payload_id")
        val current = activeSlotId()
        val candidate = slotDir(id)
        return current != id && previousGoodSlotId() == id && candidate.isDirectory &&
            File(candidate, ID_FILE).takeIf { it.isFile }?.readText()?.trim() == id &&
            File(candidate, manifest.getJSONObject("launcher").getString("python")).isFile
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

    /** Convert the pre-slot install into a usable fallback slot before any new APK update. */
    fun migrateLegacy(manifest: JSONObject) {
        if (legacyMarker.isFile) finishLegacyMigration(manifest)
        if (activeSlotId() != null || !legacyRoot.isDirectory) return
        val id = File(legacyRoot, ID_FILE).takeIf { it.isFile }?.readText()?.trim()
            ?.takeIf { isSafeSlotId(it) } ?: "legacy-${UUID.randomUUID()}"
        validateSlotId(id)
        slotsDir.mkdirs()
        val destination = slotDir(id)
        if (destination.exists()) throw IOException("旧运行环境 slot 已存在，拒绝覆盖")
        writeLegacyMarker(id)
        finishLegacyMigration(manifest)
    }

    private fun writeLegacyMarker(id: String) {
        val temp = File(filesDir, "hermes-agent.legacy-migration.json.tmp")
        FileOutputStream(temp).use { output ->
            output.write(JSONObject().put("format", 1).put("slot", id).toString().toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        if (!temp.renameTo(legacyMarker)) throw IOException("无法记录旧运行环境迁移状态")
    }

    private fun finishLegacyMigration(manifest: JSONObject) {
        val marker = try { JSONObject(legacyMarker.readText()) } catch (t: Throwable) {
            throw IOException("旧运行环境迁移标记损坏", t)
        }
        if (marker.optInt("format", -1) != 1) throw IOException("旧运行环境迁移标记版本无效")
        val id = marker.optString("slot")
        validateSlotId(id)
        val stateActive = activeSlotId()
        val destination = slotDir(id)
        if (stateActive != null) {
            if (stateActive == id && destination.isDirectory && File(destination, MANIFEST_FILE).isFile) {
                legacyMarker.delete()
                return
            }
            throw IOException("旧运行环境迁移与现有 active slot 冲突")
        }
        val legacyPath = legacyRoot.absolutePath
        if (legacyRoot.isDirectory && destination.exists()) throw IOException("旧运行环境迁移同时存在两个源目录")
        if (!destination.isDirectory && !legacyRoot.renameTo(destination)) {
            throw IOException("无法迁移旧运行环境到 slot")
        }
        val priorState = readState()
        rewriteLegacyPaths(destination, legacyPath, manifest)
        File(destination, ID_FILE).writeText(id)
        val legacyManifest = JSONObject(manifest.toString()).apply {
            put("payload_id", id)
            put("legacy_migrated", true)
            put("runtime", JSONObject().put("channel", "legacy").put("version", "unknown").put("source_commit", "unknown"))
            put("webui", JSONObject().put("channel", "legacy").put("tag", "unknown").put("commit", "unknown"))
            put("compatibility", JSONObject().put("status", "unknown"))
        }
        File(destination, MANIFEST_FILE).writeText(legacyManifest.toString())
        writeState(id, null, pending = false, generation = priorState.optLong("generation", 0L) + 1L)
        legacyMarker.delete()
    }

    private fun rewriteLegacyPaths(destination: File, legacyPath: String, manifest: JSONObject) {
        val placeholders = manifest.optJSONObject("placeholders")
        val rootPh = placeholders?.optString("root").orEmpty()
        val prefixPh = placeholders?.optString("prefix").orEmpty()
        val prefixDir = "$destination/" + placeholders?.optString("prefix_dir", "prefix")
        fun resolve(value: String): String {
            var result = value.replace(legacyPath, destination.absolutePath)
            if (rootPh.isNotEmpty()) result = result.replace(rootPh, destination.absolutePath)
            if (prefixPh.isNotEmpty()) result = result.replace(prefixPh, prefixDir)
            return result
        }
        val rewrite = manifest.optJSONArray("rewrite")
        if (rewrite != null) {
            for (i in 0 until rewrite.length()) {
                val file = File(destination, rewrite.getString(i))
                if (!file.isFile) continue
                val oldText = String(file.readBytes(), Charsets.ISO_8859_1)
                val newText = oldText.replace(legacyPath, destination.absolutePath)
                if (newText != oldText) file.writeBytes(newText.toByteArray(Charsets.ISO_8859_1))
            }
        }
        val links = manifest.optJSONArray("symlinks")
        if (links != null) {
            for (i in 0 until links.length()) {
                val pair = links.getJSONArray(i)
                val link = File(destination, pair.getString(0))
                val expected = resolve(pair.getString(1))
                val current = try { Os.readlink(link.absolutePath) } catch (_: Throwable) { null }
                if (current != expected) {
                    if (FileTools.exists(link)) FileTools.deleteTree(link)
                    link.parentFile?.mkdirs()
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
        val restored = previous?.takeIf { it != failed && slotDir(it).isDirectory }
        val retained = failed?.takeIf {
            state.optBoolean("reused_previous_good", false) && it != restored && slotDir(it).isDirectory
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
    ) {
        val payloadId = manifest.getString("payload_id")
        validateSlotId(payloadId)
        if (payloadId == rejectedPayloadId()) throw IOException("refusing rejected payload: $payloadId")
        checkCancelled(cancelled)
        migrateLegacy(manifest)
        verifyAssetDigest(manifest, cancelled)
        slotsDir.mkdirs()
        pruneSlots()
        val candidate = slotDir(payloadId)
        val current = activeSlotId()
        val previous = previousGoodSlotId()
        if (candidate.isDirectory && previous == payloadId && current != payloadId) {
            checkCancelled(cancelled)
            activateExisting(payloadId, current, cancelled, reusedPreviousGood = true)
            progress(1f, "运行环境已就绪")
            return
        }
        if (candidate.exists() && current != payloadId) FileTools.deleteTree(candidate)
        val staging = File(slotsDir, ".staging-$payloadId-${UUID.randomUUID()}")
        FileTools.deleteTree(staging)
        staging.mkdirs()

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
                    val out = File(staging, name)
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
                val file = File(staging, rewrite.getString(i))
                if (!file.isFile) continue
                val text = String(file.readBytes(), Charsets.ISO_8859_1)
                val fixed = resolve(text)
                if (fixed != text) file.writeBytes(fixed.toByteArray(Charsets.ISO_8859_1))
            }

            val links = manifest.getJSONArray("symlinks")
            for (i in 0 until links.length()) {
                checkCancelled(cancelled)
                val pair = links.getJSONArray(i)
                val link = File(staging, pair.getString(0))
                val target = resolve(pair.getString(1))
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
                val file = File(staging, executables.getString(i))
                if (file.isFile) {
                    try {
                        Os.chmod(file.absolutePath, "755".toInt(8))
                    } catch (_: ErrnoException) {
                        file.setExecutable(true, false)
                    }
                }
            }

            val launcher = File(staging, manifest.getJSONObject("launcher").getString("python"))
            if (!launcher.isFile) throw IOException("candidate launcher missing")
            File(staging, ID_FILE).writeText(payloadId)
            File(staging, MANIFEST_FILE).writeText(manifest.toString())
            checkCancelled(cancelled)
            if (!staging.renameTo(candidate)) throw IOException("无法保存新的运行环境 slot")
            if (cancelled()) {
                FileTools.deleteTree(candidate)
                throw InstallCancelledException()
            }
            activateExisting(payloadId, current, cancelled)
            progress(1f, "运行环境已就绪")
        } catch (t: Throwable) {
            FileTools.deleteTree(staging)
            throw t
        }
    }

    private fun activateExisting(
        payloadId: String,
        current: String?,
        cancelled: () -> Boolean = { false },
        reusedPreviousGood: Boolean = false,
    ) {
        checkCancelled(cancelled)
        val state = readState()
        val previous = current?.takeIf { it != payloadId } ?: state.optStringOrNull("previous_good")?.takeIf { it != payloadId }
        writeState(
            payloadId,
            previous,
            pending = true,
            generation = state.optLong("generation", 0L) + 1L,
            rejected = null,
            reusedPreviousGood = reusedPreviousGood,
        )
    }

    private fun checkCancelled(cancelled: () -> Boolean) {
        if (cancelled()) throw InstallCancelledException()
    }

    private fun slotDir(id: String): File = File(slotsDir, id)

    private fun validateSlotId(id: String) {
        if (!isSafeSlotId(id)) throw IOException("unsafe payload slot id")
    }

    private fun isSafeSlotId(id: String): Boolean =
        id.isNotBlank() && id != "." && id != ".." && !id.contains('/') && !id.contains('\\')

    private fun readState(): JSONObject {
        val candidates = listOf(stateFile, stateBackup)
        for (file in candidates) {
            if (!file.isFile) continue
            try {
                return parseState(file)
            } catch (_: Throwable) {
                // Try the backup state before declaring no active slot.
            }
        }
        if (candidates.any { it.exists() }) throw IOException("无法读取运行环境状态，拒绝删除或覆盖已有 slot")
        return JSONObject().put("format", 1).put("generation", 0).put("pending", false)
            .put("reused_previous_good", false)
    }

    private fun parseState(file: File): JSONObject {
        val value = JSONObject(file.readText())
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
        FileOutputStream(temp).use { output ->
            output.write(value.toString().toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        var prior: JSONObject? = null
        for (file in listOf(stateFile, stateBackup)) {
            if (!file.isFile) continue
            try {
                prior = parseState(file)
                break
            } catch (_: Throwable) {
            }
        }
        prior?.let { stateBackup.writeText(it.toString()) }
        if (!temp.renameTo(stateFile)) throw IOException("无法原子写入运行环境状态")
    }

    /** Keep only the active and previous-good slots; remove failed/stale candidates. */
    private fun pruneSlots() {
        val state = readState()
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

    private fun isSymlink(file: File): Boolean = try {
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
