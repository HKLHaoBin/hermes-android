package io.github.hermesandroid

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class AgentJobAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val jobId = intent?.getStringExtra(EXTRA_JOB_ID).orEmpty()
        if (jobId.isBlank()) return
        val profileId = intent.getStringExtra(EXTRA_PROFILE) ?: AgentJobCoordinator.AGENT_PROFILE
        AgentJobCoordinator.onAlarm(context.applicationContext, jobId, profileId)
    }

    companion object {
        const val ACTION = "io.github.hermesandroid.AGENT_JOB_ALARM"
        const val EXTRA_JOB_ID = "agent_job_id"
        const val EXTRA_PROFILE = "agent_profile"
    }
}
