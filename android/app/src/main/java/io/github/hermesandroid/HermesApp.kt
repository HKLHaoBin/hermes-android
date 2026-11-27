package io.github.hermesandroid

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build

class HermesApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (Build.VERSION.SDK_INT >= 26) {
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    getString(R.string.channel_name),
                    NotificationManager.IMPORTANCE_LOW,
                ).apply {
                    description = getString(R.string.channel_desc)
                    setShowBadge(false)
                },
            )
            manager.createNotificationChannel(
                NotificationChannel(
                    JOB_CHANNEL_ID,
                    getString(R.string.job_channel_name),
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply {
                    description = getString(R.string.job_channel_desc)
                    setShowBadge(true)
                },
            )
        }
    }

    companion object {
        const val CHANNEL_ID = "hermes_runtime"
        const val JOB_CHANNEL_ID = "hermes_agent_jobs"
    }
}
