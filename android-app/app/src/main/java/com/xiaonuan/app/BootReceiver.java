package com.xiaonuan.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 开机自启：如果用户开启过每日守护，重启手机后自动恢复守护服务 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            boolean on = context.getSharedPreferences("settings", Context.MODE_PRIVATE)
                    .getBoolean("guard_on", false);
            if (on) {
                context.startForegroundService(new Intent(context, GuardService.class));
            }
        }
    }
}
