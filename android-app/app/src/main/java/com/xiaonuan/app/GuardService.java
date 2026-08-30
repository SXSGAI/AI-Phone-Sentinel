package com.xiaonuan.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.PixelFormat;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.view.LayoutInflater;
import android.view.View;
import android.view.WindowManager;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** 守护前台服务：每 30 秒轮询服务器，到点（每日定时）全屏响铃提醒老人接听 */
public class GuardService extends Service {

    private final Handler h = new Handler(Looper.getMainLooper());
    private boolean polling = false;
    private long lastNotified = 0;
    private MediaPlayer ring;
    private View overlayView;
    private WindowManager wm;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("guard", "守护服务",
                NotificationManager.IMPORTANCE_LOW));
        NotificationChannel calls = new NotificationChannel("calls", "来电提醒",
                NotificationManager.IMPORTANCE_HIGH);
        calls.setSound(null, null); // 铃声由服务自己播放（用回铃音）
        nm.createNotificationChannel(calls);
        startForeground(1, buildNotification("小暖守护运行中", "到点会自动来电话，记得保持手机开机"));
    }

    private Notification buildNotification(String title, String text) {
        return new Notification.Builder(this, "guard")
                .setSmallIcon(android.R.drawable.sym_action_call)
                .setContentTitle(title)
                .setContentText(text)
                .setOngoing(true)
                .build();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!polling) {
            polling = true;
            h.postDelayed(pollRun, 5000);
        }
        return START_STICKY;
    }

    private final Runnable pollRun = new Runnable() {
        @Override public void run() {
            pollOnce();
            h.postDelayed(this, 30000);
        }
    };

    private void pollOnce() {
        final String base = getSharedPreferences("settings", MODE_PRIVATE)
                .getString("url", "http://127.0.0.1:8000");
        new Thread(() -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(base + "/api/poll").openConnection();
                c.setConnectTimeout(8000); c.setReadTimeout(20000);
                InputStream is = c.getInputStream();
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                byte[] b = new byte[4096]; int n;
                while ((n = is.read(b)) > 0) bos.write(b, 0, n);
                is.close();
                JSONObject j = new JSONObject(new String(bos.toByteArray(), StandardCharsets.UTF_8));
                if (j.optBoolean("due") && System.currentTimeMillis() - lastNotified > 9 * 60 * 1000) {
                    lastNotified = System.currentTimeMillis();
                    h.post(this::fireIncomingCall);
                }
            } catch (Exception ignored) {}
        }).start();
    }

    /** 到点：多级兜底，保证总有一种方式能提醒到老人 */
    private void fireIncomingCall() {
        // 1) 悬浮窗来电界面（已授权悬浮窗时最可靠，画在最上层、无需启动界面）
        if (Settings.canDrawOverlays(this)) {
            try {
                showOverlayIncoming();
                Log.e("XN", "来电: 悬浮窗已显示");
                startRing();
                return;
            } catch (Exception e) {
                Log.e("XN", "来电: 悬浮窗失败 -> " + e);
                removeOverlay();
            }
        } else {
            Log.e("XN", "来电: 无悬浮窗权限");
        }
        // 2) 尝试后台拉起来电界面（部分系统允许前台服务这么做）
        try {
            Intent intent = new Intent(this, MainActivity.class);
            intent.putExtra("incoming", true);
            intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            startActivity(intent);
            Log.e("XN", "来电: startActivity 已调用");
            startRing();
            return;
        } catch (Exception e) {
            Log.e("XN", "来电: startActivity 失败 -> " + e);
        }
        // 3) 全屏通知兜底（锁屏也能看到）
        Log.e("XN", "来电: 走全屏通知兜底");
        notifyCall();
    }

    private void startRing() {
        stopRing();
        ring = MediaPlayer.create(this, R.raw.dial);
        if (ring != null) {
            ring.setLooping(true);
            ring.start();
            new Handler(Looper.getMainLooper()).postDelayed(this::stopRing, 60000);
        }
    }

    private void showOverlayIncoming() {
        if (overlayView != null) return;
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        overlayView = LayoutInflater.from(this).inflate(R.layout.overlay_incoming, null);
        WindowManager.LayoutParams p = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        wm.addView(overlayView, p);
        overlayView.findViewById(R.id.ovAnswer).setOnClickListener(v -> {
            removeOverlay();
            stopRing();
            Intent it = new Intent(this, MainActivity.class);
            it.putExtra("answer_direct", true);
            it.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            try { startActivity(it); } catch (Exception e) { notifyCall(); }
        });
        overlayView.findViewById(R.id.ovDecline).setOnClickListener(v -> {
            removeOverlay();
            stopRing();
        });
    }

    private void removeOverlay() {
        if (overlayView != null && wm != null) {
            try { wm.removeView(overlayView); } catch (Exception ignored) {}
            overlayView = null;
        }
    }

    private void notifyCall() {
        Intent intent = new Intent(this, MainActivity.class);
        intent.putExtra("incoming", true);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent pi = PendingIntent.getActivity(this, 11, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationManager nm = getSystemService(NotificationManager.class);
        Notification n = new Notification.Builder(this, "calls")
                .setSmallIcon(android.R.drawable.sym_action_call)
                .setContentTitle("📞 小暖来电话啦")
                .setContentText("点一下接听，小暖陪你聊聊天")
                .setFullScreenIntent(pi, true)
                .setContentIntent(pi)
                .setCategory(Notification.CATEGORY_CALL)
                .setAutoCancel(true)
                .build();
        nm.notify(22, n);

        // 服务层自己响回铃（最长 60 秒），锁屏也能听见
        try {
            stopRing();
            ring = MediaPlayer.create(this, R.raw.dial);
            if (ring != null) {
                ring.setLooping(true);
                ring.start();
                new Handler(Looper.getMainLooper()).postDelayed(this::stopRing, 60000);
            }
        } catch (Exception ignored) {}
    }

    private void stopRing() {
        if (ring != null) {
            try { ring.stop(); ring.release(); } catch (Exception ignored) {}
            ring = null;
        }
    }

    @Override
    public void onDestroy() {
        h.removeCallbacks(pollRun);
        stopRing();
        super.onDestroy();
    }
}
