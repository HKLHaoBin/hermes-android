package io.github.hermesandroid

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.PowerManager
import org.json.JSONObject
import kotlin.math.abs

object AgentJobNotification {
    fun show(context: Context, record: JobRecord) {
        val appContext = context.applicationContext
        val manager = appContext.getSystemService(NotificationManager::class.java)
        manager.notify(
            record.notificationId ?: record.jobId,
            notificationId(record.jobId),
            notification(appContext, record),
        )
    }

    fun cancel(context: Context, record: JobRecord) {
        context.applicationContext
            .getSystemService(NotificationManager::class.java)
            .cancel(record.notificationId ?: record.jobId, notificationId(record.jobId))
    }

    fun notification(context: Context, record: JobRecord): Notification {
        val open = PendingIntent.getActivity(
            context,
            requestCode(record.jobId, 0),
            Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(MainActivity.EXTRA_JOB_ID, record.jobId),
            PendingIntent.FLAG_UPDATE_CURRENT or pendingFlags(),
        )
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(context, HermesApp.JOB_CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(context).setPriority(Notification.PRIORITY_DEFAULT)
        }
        val title = when (record.state) {
            JobState.QUEUED -> "Agent 任务已排队"
            JobState.RUNNING -> "Agent 任务运行中"
            JobState.PAUSED -> "Agent 任务已暂停"
            JobState.SUCCEEDED -> "Agent 任务已完成"
            JobState.FAILED -> "Agent 任务失败，可恢复"
            JobState.CANCELLED -> "Agent 任务已取消"
        }
        val text = when (record.state) {
            JobState.QUEUED -> "任务 ${shortId(record.jobId)} 等待运行"
            JobState.RUNNING -> "任务 ${shortId(record.jobId)} 正在后台执行"
            JobState.PAUSED -> "任务 ${shortId(record.jobId)} 可点击恢复"
            JobState.SUCCEEDED -> "任务 ${shortId(record.jobId)} 已完成"
            JobState.FAILED -> "任务 ${shortId(record.jobId)}：${record.lastError ?: "任务执行失败"}"
            JobState.CANCELLED -> "任务 ${shortId(record.jobId)} 已取消"
        }
        builder.setSmallIcon(R.drawable.ic_stat_hermes)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(open)
            .setOnlyAlertOnce(record.state == JobState.RUNNING || record.state == JobState.QUEUED)
            .setAutoCancel(record.state.isTerminal)
            .setOngoing(record.state == JobState.RUNNING || record.state == JobState.QUEUED)
        when (record.state) {
            JobState.RUNNING -> {
                builder.addAction(action(context, HermesService.ACTION_PAUSE_JOB, record.jobId, "暂停"))
                builder.addAction(action(context, HermesService.ACTION_CANCEL_JOB, record.jobId, "取消"))
            }
            JobState.PAUSED, JobState.FAILED -> {
                builder.addAction(action(context, HermesService.ACTION_RESUME_JOB, record.jobId, "恢复"))
                builder.addAction(action(context, HermesService.ACTION_CANCEL_JOB, record.jobId, "取消"))
            }
            JobState.QUEUED -> builder.addAction(action(context, HermesService.ACTION_CANCEL_JOB, record.jobId, "取消"))
            JobState.SUCCEEDED, JobState.CANCELLED -> Unit
        }
        return builder.build()
    }

    fun channelDiagnostics(context: Context): JSONObject {
        val appContext = context.applicationContext
        val manager = appContext.getSystemService(NotificationManager::class.java)
        val power = appContext.getSystemService(Context.POWER_SERVICE) as PowerManager
        val channel = if (Build.VERSION.SDK_INT >= 26) manager.getNotificationChannel(HermesApp.JOB_CHANNEL_ID) else null
        return JSONObject().apply {
            put("notificationsEnabled", if (Build.VERSION.SDK_INT >= 24) manager.areNotificationsEnabled() else true)
            put("jobChannel", HermesApp.JOB_CHANNEL_ID)
            put("jobChannelEnabled", channel == null || channel.importance != NotificationManager.IMPORTANCE_NONE)
            put("batteryOptimized", !power.isIgnoringBatteryOptimizations(appContext.packageName))
            put("powerSaveMode", power.isPowerSaveMode)
        }
    }

    private fun action(context: Context, action: String, jobId: String, label: String): Notification.Action {
        val intent = Intent(context, HermesService::class.java)
            .setAction(action)
            .putExtra(HermesService.EXTRA_JOB_ID, jobId)
        val pending = PendingIntent.getService(
            context,
            requestCode(jobId, action.hashCode()),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or pendingFlags(),
        )
        return Notification.Action.Builder(
            android.graphics.drawable.Icon.createWithResource(context, R.drawable.ic_stat_hermes),
            label,
            pending,
        ).build()
    }

    private fun notificationId(jobId: String): Int = 1000 + (abs(jobId.hashCode()) % 100000)

    private fun requestCode(jobId: String, salt: Int): Int = abs(31 * jobId.hashCode() + salt)

    private fun shortId(jobId: String): String = jobId.take(8)

    private fun pendingFlags(): Int = if (Build.VERSION.SDK_INT >= 23) PendingIntent.FLAG_IMMUTABLE else 0
}
