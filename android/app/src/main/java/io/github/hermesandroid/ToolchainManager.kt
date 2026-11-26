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
                    if (!active.has(dependency)) missing.put(dependency)
                }
                val selected = active.optJSONObject(name)
                val stateName = when {
                    selected != null && activeEntryIsPresent(context, selected) -> "active"
                    selected != null -> "corrupt"
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
                    if (stateName == "corrupt") put("reason", "active entry is missing")
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
        val state = readObject(File(root(context), "state.json"))
        val facts = state.optJSONObject("facts") ?: readObject(File(root(context), "facts.json"))
        val packages = facts.optJSONObject("packages") ?: return emptyList()
        val output = ArrayList<String>()
        val names = packages.keys()
        while (names.hasNext()) {
            val packageFacts = packages.optJSONObject(names.next()) ?: continue
            val entries = packageFacts.optJSONArray(field) ?: continue
            for (i in 0 until entries.length()) {
                val relative = entries.optString(i)
                if (relative.isNotBlank()) output.add(File(root(context), relative).absolutePath)
            }
        }
        return output.distinct()
    }

    fun toolchainScript(manifest: JSONObject, root: File): File? =
        manifest.optString("toolchain_script")
            .takeIf { it.isNotBlank() && it != "null" }
            ?.let { File(root, it) }
            ?.takeIf { it.isFile }

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

    private fun activeEntryIsPresent(context: Context, selected: JSONObject): Boolean {
        val relative = selected.optString("entry")
        if (relative.isBlank() || relative.startsWith("/") || relative.contains("\\") || relative.split('/').contains("..")) {
            return false
        }
        return File(root(context), relative).isDirectory
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
