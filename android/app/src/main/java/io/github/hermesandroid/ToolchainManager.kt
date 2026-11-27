package io.github.hermesandroid

import android.content.Context
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException

/** App-private bridge for the versioned Android toolchain catalog and facts. */
object ToolchainManager {
    private const val CATALOG_ASSET = "toolchain/catalog.json"
    private const val SCHEMA_VERSION = 1
    private const val ROOT_NAME = "tools"

    private data class SelectionStatus(val state: String, val reason: String = "")

    fun root(context: Context): File = File(context.filesDir, ROOT_NAME).apply { mkdirs() }

    /** Copy the APK-verified catalog into the durable app-private tool root. */
    fun ensureCatalog(context: Context): File? {
        return try {
            val destination = File(root(context), "catalog.json")
            val bytes = context.assets.open(CATALOG_ASSET).use { it.readBytes() }
            if (!destination.isFile || !destination.readBytes().contentEquals(bytes)) {
                val temporary = File(destination.parentFile, ".catalog-${System.nanoTime()}.tmp")
                FileOutputStream(temporary).use { it.write(bytes) }
                if (!temporary.renameTo(destination)) {
                    temporary.delete()
                    throw IOException("cannot activate toolchain catalog")
                }
            }
            destination
        } catch (_: IOException) {
            null
        }
    }

    fun inventoryJson(context: Context): JSONObject {
        val catalogFile = ensureCatalog(context)
        if (catalogFile == null) {
            return JSONObject().apply {
                put("schema_version", SCHEMA_VERSION)
                put("state", "unavailable")
                put("reason", "toolchain catalog is not bundled")
                put("target", hostJson(context))
                put("packages", JSONArray())
            }
        }
        return try {
            val catalog = JSONObject(catalogFile.readText(Charsets.UTF_8))
            val state = readObject(File(root(context), "state.json"))
            val active = state.optJSONObject("active") ?: readObject(File(root(context), "active.json"))
            val previous = state.optJSONObject("previous") ?: readObject(File(root(context), "previous.json"))
            val packages = JSONArray()
            val records = catalog.optJSONArray("packages") ?: JSONArray()
            for (i in 0 until records.length()) {
                val record = records.optJSONObject(i) ?: continue
                val name = record.optString("name")
                val dependencies = record.optJSONArray("dependencies") ?: JSONArray()
                val conflicts = record.optJSONArray("conflicts") ?: JSONArray()
                val missing = JSONArray()
                for (j in 0 until dependencies.length()) {
                    val dependency = dependencies.optString(j)
                    if (!activeTreeValid(context, catalog, records, dependency, active, HashSet())) {
                        missing.put(dependency)
                    }
                }
                val selected = active.optJSONObject(name)
                val selectedPresent = active.has(name)
                val selectedStatus = selectionStatus(context, catalog, name, record, selected, selectedPresent)
                val stateName = if (selectedPresent) {
                    if (selectedStatus.state != "active") selectedStatus.state
                    else if (missing.length() > 0) "blocked"
                    else "active"
                } else when {
                    !hostCompatible(context, catalog, record) -> "unsupported"
                    missing.length() > 0 -> "blocked"
                    else -> "available"
                }
                packages.put(JSONObject().apply {
                    put("name", name)
                    put("version", record.optString("version"))
                    put("state", stateName)
                    put("dependencies", dependencies)
                    put("conflicts", conflicts)
                    put("sha256", record.optJSONObject("artifact")?.optString("sha256", "") ?: "")
                    if (selectedPresent && selectedStatus.reason.isNotEmpty()) put("reason", selectedStatus.reason)
                    else if (stateName == "unsupported") put("reason", "host ABI/API/libc does not match")
                    else if (missing.length() > 0) put("reason", "dependency not installed: ${joinJson(missing)}")
                })
            }
            JSONObject().apply {
                put("schema_version", SCHEMA_VERSION)
                put("catalog_schema_version", catalog.optInt("schema_version", 0))
                put("catalog_id", catalog.optString("catalog_id"))
                put("state", "ready")
                put("target", catalog.optJSONObject("target") ?: hostJson(context))
                put("host", hostJson(context))
                put("repository", catalog.optJSONObject("repository") ?: JSONObject())
                put("root", root(context).absolutePath)
                put("active", active)
                put("previous", previous)
                put("packages", packages)
            }
        } catch (error: Exception) {
            JSONObject().apply {
                put("schema_version", SCHEMA_VERSION)
                put("state", "error")
                put("reason", "cannot read toolchain state: ${error.message ?: error.javaClass.simpleName}")
                put("target", hostJson(context))
                put("packages", JSONArray())
            }
        }
    }

    fun pathEntries(context: Context, field: String): List<String> {
        val catalogFile = ensureCatalog(context) ?: return emptyList()
        val state = readObject(File(root(context), "state.json"))
        val active = state.optJSONObject("active") ?: readObject(File(root(context), "active.json"))
        val catalog = try {
            JSONObject(catalogFile.readText(Charsets.UTF_8))
        } catch (_: Exception) {
            return emptyList()
        }
        val records = catalog.optJSONArray("packages") ?: return emptyList()
        val output = ArrayList<String>()
        val names = active.keys()
        while (names.hasNext()) {
            val name = names.next()
            val record = findPackageRecord(records, name) ?: continue
            if (!activeTreeValid(context, catalog, records, name, active, HashSet())) continue
            val selected = active.optJSONObject(name) ?: continue
            val entry = selected.optString("entry")
            val entries = record.optJSONObject("exports")?.optJSONArray(field) ?: continue
            for (i in 0 until entries.length()) {
                val relative = entries.optString(i)
                if (relative.isBlank() || relative.startsWith("/") || relative.contains("\\") || relative.split('/').contains("..")) continue
                output.add(File(root(context), "$entry/$relative").absolutePath)
            }
        }
        return output.distinct()
    }

    fun toolchainScript(manifest: JSONObject, root: File): File? =
        manifest.optString("toolchain_script")
            .takeIf { it.isNotBlank() && it != "null" }
            ?.let { File(root, it) }
            ?.takeIf { it.isFile }

    private fun findPackageRecord(records: JSONArray, name: String): JSONObject? {
        for (i in 0 until records.length()) {
            val record = records.optJSONObject(i) ?: continue
            if (record.optString("name") == name) return record
        }
        return null
    }

    private fun selectionStatus(
        context: Context,
        catalog: JSONObject,
        name: String,
        record: JSONObject?,
        selected: JSONObject?,
        selectedPresent: Boolean = selected != null,
    ): SelectionStatus {
        if (!selectedPresent) return SelectionStatus("available")
        if (selected == null) return SelectionStatus("corrupt", "active selection is malformed")
        if (record == null) return SelectionStatus("corrupt", "active package is absent from catalog")
        val version = selected.optString("version")
        val catalogVersion = record.optString("version")
        if (version.isBlank()) return SelectionStatus("corrupt", "active version is missing")
        if (version != catalogVersion) {
            return SelectionStatus("outdated", "active version $version is outdated; catalog requires $catalogVersion")
        }
        val relative = selected.optString("entry")
        if (relative.isBlank() || relative.startsWith("/") || relative.contains("\\") || relative.split('/').contains("..")) {
            return SelectionStatus("corrupt", "active entry is unsafe")
        }
        val entry = File(root(context), relative)
        val packageRoot = File(root(context), "entries/$name")
        if (!entry.isDirectory || !entry.toPath().normalize().startsWith(packageRoot.toPath().normalize())) {
            return SelectionStatus("corrupt", "active entry is missing or outside package slot")
        }
        val manifestFile = File(entry, ".hermes-package.json")
        val manifest = try {
            if (!manifestFile.isFile) return SelectionStatus("corrupt", "package manifest is missing")
            JSONObject(manifestFile.readText(Charsets.UTF_8))
        } catch (_: Exception) {
            return SelectionStatus("corrupt", "package manifest is unreadable")
        }
        if (manifest.optInt("schema_version", 0) != 1 ||
            manifest.optString("name") != name ||
            manifest.optString("version") != version ||
            manifest.optString("target") != catalog.optJSONObject("target")?.optString("triple")
        ) {
            return SelectionStatus("corrupt", "package manifest does not match selection")
        }
        val expectedHash = record.optJSONObject("artifact")?.optString("sha256", "") ?: ""
        if (expectedHash.length != 64 || manifest.optString("artifact_sha256") != expectedHash) {
            return SelectionStatus("corrupt", "package manifest hash does not match catalog")
        }
        val declared = manifest.optJSONArray("entrypoints") ?: return SelectionStatus("corrupt", "package manifest entrypoints are missing")
        val catalogEntrypoints = record.optJSONArray("entrypoints") ?: JSONArray()
        if (!sameJsonArray(declared, catalogEntrypoints)) {
            return SelectionStatus("corrupt", "package manifest entrypoints do not match catalog")
        }
        for (i in 0 until declared.length()) {
            val entrypoint = declared.optString(i)
            if (entrypoint.isBlank() || entrypoint.startsWith("/") || entrypoint.contains("\\") || entrypoint.split('/').contains("..")) {
                return SelectionStatus("corrupt", "package manifest entrypoint is unsafe")
            }
            val file = File(entry, entrypoint)
            if (!file.isFile || !file.canExecute()) {
                return SelectionStatus("corrupt", "declared entrypoint is missing or not executable: $entrypoint")
            }
        }
        return SelectionStatus("active")
    }

    private fun sameJsonArray(left: JSONArray, right: JSONArray): Boolean {
        if (left.length() != right.length()) return false
        for (i in 0 until left.length()) {
            if (left.optString(i) != right.optString(i)) return false
        }
        return true
    }

    private fun activeTreeValid(
        context: Context,
        catalog: JSONObject,
        records: JSONArray,
        name: String,
        active: JSONObject,
        visiting: MutableSet<String>,
    ): Boolean {
        if (!visiting.add(name)) return false
        return try {
            val record = findPackageRecord(records, name) ?: return false
            val selectedPresent = active.has(name)
            val status = selectionStatus(context, catalog, name, record, active.optJSONObject(name), selectedPresent)
            if (status.state != "active") return false
            val dependencies = record.optJSONArray("dependencies") ?: JSONArray()
            for (i in 0 until dependencies.length()) {
                if (!activeTreeValid(context, catalog, records, dependencies.optString(i), active, visiting)) return false
            }
            true
        } finally {
            visiting.remove(name)
        }
    }

    private fun hostCompatible(context: Context, catalog: JSONObject, record: JSONObject): Boolean {
        val target = catalog.optJSONObject("target") ?: return false
        val abi = target.optString("abi")
        val minSdk = target.optInt("min_sdk", Int.MAX_VALUE)
        val architecture = record.optString("architecture")
        return (abi == Build.SUPPORTED_ABIS.firstOrNull()) &&
            Build.VERSION.SDK_INT >= minSdk &&
            (architecture == "aarch64" || architecture == "all")
    }

    private fun hostJson(context: Context): JSONObject = JSONObject().apply {
        put("abi", Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown")
        put("api", Build.VERSION.SDK_INT)
        put("libc", "bionic")
        put("root", root(context).absolutePath)
        put("freeBytes", root(context).usableSpace)
        put("writable", root(context).canWrite())
        put("executable", root(context).canExecute())
    }


    private fun readObject(file: File): JSONObject = try {
        if (file.isFile) JSONObject(file.readText(Charsets.UTF_8)) else JSONObject()
    } catch (_: Exception) {
        JSONObject()
    }

    private fun joinJson(values: JSONArray): String = buildList {
        for (i in 0 until values.length()) add(values.optString(i))
    }.joinToString(", ")
}
