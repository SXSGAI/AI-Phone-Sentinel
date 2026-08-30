package com.xiaonuan.app;

import android.animation.AnimatorSet;
import android.animation.ObjectAnimator;
import android.app.Activity;
import android.content.Intent;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaPlayer;
import android.media.MediaRecorder;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.method.ScrollingMovementMethod;
import android.view.View;
import android.view.animation.LinearInterpolator;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

public class MainActivity extends Activity {

    // ---- Tab 页 ----
    private View tabHome, tabRecords, tabSettings;
    private TextView navHome, navRecords, navSettings;
    private TextView homeTarget, homeChild, recordsView;

    // ---- 设置 ----
    private EditText etCallName, etElderName, etChildName, etHobbies, etHealth, etTaboo, etUrl;
    private EditText etPersonaStyle, etPersonaDialect, etPersonaTopics, etPersonaCustom;
    private EditText etCallTime;
    private TextView guardBtn;

    // ---- 通话覆盖层 ----
    private View callOverlay, screenDial, screenIncoming, screenIncall, screenEnded;
    private TextView answerCircle, micCircle, micLabel, callTimer, log;
    private TextView endDuration, endStatus, endSummary, btnRedial, btnBack;
    private View micBtn, hangBtn, answerBtn;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private String base = "";
    private String callId = null;
    private MediaPlayer player, sfxPlayer;
    private ObjectAnimator pulseX, pulseY;
    private Runnable callTick;
    private int callSec = 0;
    private String callMode = "out";
    private long lastHangTap = 0;
    private final StringBuilder transcript = new StringBuilder();
    private final SimpleDateFormat ts = new SimpleDateFormat("MM-dd HH:mm", Locale.CHINA);

    // 录音状态
    private AudioRecord recorder;
    private Thread recThread;
    private volatile boolean recording = false;
    private final ByteArrayOutputStream pcm = new ByteArrayOutputStream();

    private static final int SAMPLE_RATE = 16000;
    private static final int CHUNK = 1600; // 100ms @16k mono

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        // Tab
        tabHome = findViewById(R.id.tabHome);
        tabRecords = findViewById(R.id.tabRecords);
        tabSettings = findViewById(R.id.tabSettings);
        navHome = findViewById(R.id.navHome);
        navRecords = findViewById(R.id.navRecords);
        navSettings = findViewById(R.id.navSettings);
        homeTarget = findViewById(R.id.homeTarget);
        homeChild = findViewById(R.id.homeChild);
        recordsView = findViewById(R.id.recordsView);

        // 设置
        etCallName = findViewById(R.id.etCallName);
        etElderName = findViewById(R.id.etElderName);
        etChildName = findViewById(R.id.etChildName);
        etHobbies = findViewById(R.id.etHobbies);
        etHealth = findViewById(R.id.etHealth);
        etTaboo = findViewById(R.id.etTaboo);
        etUrl = findViewById(R.id.etUrl);
        etPersonaStyle = findViewById(R.id.etPersonaStyle);
        etPersonaDialect = findViewById(R.id.etPersonaDialect);
        etPersonaTopics = findViewById(R.id.etPersonaTopics);
        etPersonaCustom = findViewById(R.id.etPersonaCustom);
        etCallTime = findViewById(R.id.etCallTime);
        guardBtn = findViewById(R.id.guardBtn);

        // 通话
        callOverlay = findViewById(R.id.callOverlay);
        screenDial = findViewById(R.id.screenDial);
        screenIncoming = findViewById(R.id.screenIncoming);
        screenIncall = findViewById(R.id.screenIncall);
        screenEnded = findViewById(R.id.screenEnded);
        endDuration = findViewById(R.id.endDuration);
        endStatus = findViewById(R.id.endStatus);
        endSummary = findViewById(R.id.endSummary);
        btnRedial = findViewById(R.id.btnRedial);
        btnBack = findViewById(R.id.btnBack);
        answerBtn = findViewById(R.id.answerBtn);
        answerCircle = findViewById(R.id.answerCircle);
        View declineBtn = findViewById(R.id.declineBtn);
        micCircle = findViewById(R.id.micCircle);
        micLabel = findViewById(R.id.micLabel);
        hangBtn = findViewById(R.id.hangBtn);
        callTimer = findViewById(R.id.callTimer);
        log = findViewById(R.id.log);
        log.setMovementMethod(new ScrollingMovementMethod());

        requestPermissions(new String[]{android.Manifest.permission.RECORD_AUDIO,
                android.Manifest.permission.POST_NOTIFICATIONS}, 1);

        // 导航
        navHome.setOnClickListener(v -> switchTab(0));
        navRecords.setOnClickListener(v -> { switchTab(1); renderRecords(); });
        navSettings.setOnClickListener(v -> switchTab(2));

        // 首页
        findViewById(R.id.startBtn).setOnClickListener(v -> startOutbound());
        findViewById(R.id.incomingBtn).setOnClickListener(v -> startIncomingCall());

        // 设置
        findViewById(R.id.saveSettingsBtn).setOnClickListener(v -> saveSettings());
        guardBtn.setOnClickListener(v -> toggleGuard());
        btnRedial.setOnClickListener(v -> startOutbound());
        btnBack.setOnClickListener(v -> closeCallOverlay());

        // 记录
        findViewById(R.id.clearRecordsBtn).setOnClickListener(v -> {
            new File(getFilesDir(), "records.json").delete();
            renderRecords();
            toast("本地记录已清空");
        });

        // 通话
        answerBtn.setOnClickListener(v -> answer());
        declineBtn.setOnClickListener(v -> { sfx(R.raw.flash); startOutbound(); });
        micBtn = (View) findViewById(R.id.micCircle).getParent();
        micBtn.setOnClickListener(v -> { if (recording) stopRecording(); else startRecording(); });
        hangBtn.setOnClickListener(v -> {
            long now = System.currentTimeMillis();
            if (now - lastHangTap < 1200) return;   // 挂断防抖
            lastHangTap = now;
            hangup();
        });

        loadSettings();
        fetchProfile();
        renderRecords();
        switchTab(0);

        if (getSharedPreferences("settings", MODE_PRIVATE).getBoolean("guard_on", false)) {
            startGuardService();
        }
        // 守护服务始终随 App 启动常驻（老人专用机，到点必须能响铃）
        getSharedPreferences("settings", MODE_PRIVATE).edit().putBoolean("guard_on", true).apply();
        startGuardService();

        handleIncomingIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIncomingIntent(intent);
    }

    /** 从来电通知/悬浮窗点进来：incoming=显示来电界面；answer_direct=直接接通 */
    private void handleIncomingIntent(Intent i) {
        if (i == null) return;
        if (i.getBooleanExtra("answer_direct", false)) {
            openCallOverlay();
            show(screenIncall);
            startCallTimer();
            addLog("已接通 · 通话中");
            doStart("/api/call/start");
        } else if (i.getBooleanExtra("incoming", false)) {
            openCallOverlay();
            show(screenIncoming);
            stop(sfxPlayer);
            sfxPlayer = play(R.raw.dial, true);
            startPulse();
        }
    }

    // ================= 每日守护 =================
    private void toggleGuard() {
        android.content.SharedPreferences sp = getSharedPreferences("settings", MODE_PRIVATE);
        boolean on = sp.getBoolean("guard_on", false);
        sp.edit().putBoolean("guard_on", !on).apply();
        if (on) {
            stopService(new Intent(this, GuardService.class));
            toast("每日定时守护已关闭");
        } else {
            startGuardService();
            toast("守护已开启：到点小暖自动打来");
        }
        updateGuardBtn();
    }

    private void startGuardService() {
        try { startForegroundService(new Intent(this, GuardService.class)); } catch (Exception ignored) {}
        updateGuardBtn();
    }

    private void updateGuardBtn() {
        boolean on = getSharedPreferences("settings", MODE_PRIVATE).getBoolean("guard_on", false);
        guardBtn.setText(on ? "✅ 每日定时守护已开启（点关闭）" : "⭕ 开启每日定时守护");
        guardBtn.setTextColor(on ? 0xFF217A4B : 0xFF4E5D49);
    }

    // ================= Tab 切换 =================
    private void switchTab(int i) {
        tabHome.setVisibility(i == 0 ? View.VISIBLE : View.GONE);
        tabRecords.setVisibility(i == 1 ? View.VISIBLE : View.GONE);
        tabSettings.setVisibility(i == 2 ? View.VISIBLE : View.GONE);
        int on = 0xFF6E8B74, off = 0xFFA8A8A0;
        navHome.setTextColor(i == 0 ? on : off);
        navRecords.setTextColor(i == 1 ? on : off);
        navSettings.setTextColor(i == 2 ? on : off);
        if (i == 0) refreshHome();
    }

    private void refreshHome() {
        android.content.SharedPreferences sp = getSharedPreferences("settings", MODE_PRIVATE);
        homeTarget.setText("称呼：" + orDash(sp.getString("call_name", "")) + "　·　" + orDash(sp.getString("elder_name", "")));
        homeChild.setText("由子女「" + orDash(sp.getString("child_name", "")) + "」配置 · AI 会依据这些信息对话");
    }

    private String orDash(String s) { return s == null || s.trim().isEmpty() ? "—" : s.trim(); }

    // ================= 设置 =================
    private void loadSettings() {
        android.content.SharedPreferences sp = getSharedPreferences("settings", MODE_PRIVATE);
        etCallName.setText(sp.getString("call_name", ""));
        etElderName.setText(sp.getString("elder_name", ""));
        etChildName.setText(sp.getString("child_name", ""));
        etHobbies.setText(sp.getString("hobbies", ""));
        etHealth.setText(sp.getString("health_notes", ""));
        etTaboo.setText(sp.getString("taboo", ""));
        etPersonaStyle.setText(sp.getString("persona_style", ""));
        etPersonaDialect.setText(sp.getString("persona_dialect", "普通话"));
        etPersonaTopics.setText(sp.getString("persona_topics", ""));
        etPersonaCustom.setText(sp.getString("persona_custom", ""));
        etUrl.setText(sp.getString("url", "http://192.168.1.100:8000"));
        etCallTime.setText(sp.getString("call_time", "19:00"));
        updateGuardBtn();
    }

    private void fetchProfile() {
        new Thread(() -> {
            try {
                String body = httpGet(baseUrl() + "/api/profile");
                JSONObject j = new JSONObject(body);
                ui.post(() -> {
                    etCallName.setText(j.optString("call_name", ""));
                    etElderName.setText(j.optString("elder_name", ""));
                    etChildName.setText(j.optString("child_name", ""));
                    etHobbies.setText(j.optString("hobbies", ""));
                    etHealth.setText(j.optString("health_notes", ""));
                    etTaboo.setText(j.optString("taboo", ""));
                etPersonaStyle.setText(j.optString("persona_style", ""));
                etPersonaDialect.setText(j.optString("persona_dialect", "普通话"));
                etPersonaTopics.setText(j.optString("persona_topics", ""));
                etPersonaCustom.setText(j.optString("persona_custom", ""));
                etCallTime.setText(j.optString("call_time", "19:00"));
                    persistSettings();
                    refreshHome();
                });
            } catch (Exception ignored) {}
        }).start();
    }

    private void saveSettings() {
        String callName = etCallName.getText().toString().trim();
        String url = etUrl.getText().toString().trim();
        if (callName.isEmpty()) { toast("请填写对老人的称呼"); return; }
        if (url.isEmpty()) { toast("请填写服务器地址"); return; }
        persistSettings();
        refreshHome();
        toast("设置已保存");
        final String payload = jsonSettings();
        new Thread(() -> {
            try {
                postJson(baseUrl() + "/api/profile", payload);
                ui.post(() -> toast("已同步到 AI 服务 ✓"));
            } catch (Exception e) {
                ui.post(() -> toast("本地已保存（服务器暂不可达）"));
            }
        }).start();
    }

    private void persistSettings() {
        getSharedPreferences("settings", MODE_PRIVATE).edit()
                .putString("call_name", etCallName.getText().toString().trim())
                .putString("elder_name", etElderName.getText().toString().trim())
                .putString("child_name", etChildName.getText().toString().trim())
                .putString("hobbies", etHobbies.getText().toString().trim())
                .putString("health_notes", etHealth.getText().toString().trim())
                .putString("taboo", etTaboo.getText().toString().trim())
                .putString("persona_style", etPersonaStyle.getText().toString().trim())
                .putString("persona_dialect", etPersonaDialect.getText().toString().trim())
                .putString("persona_topics", etPersonaTopics.getText().toString().trim())
                .putString("persona_custom", etPersonaCustom.getText().toString().trim())
                .putString("url", etUrl.getText().toString().trim())
                .putString("call_time", etCallTime.getText().toString().trim())
                .apply();
    }

    private String jsonSettings() {
        JSONObject j = new JSONObject();
        try {
            j.put("call_name", etCallName.getText().toString().trim());
            j.put("elder_name", etElderName.getText().toString().trim());
            j.put("child_name", etChildName.getText().toString().trim());
            j.put("hobbies", etHobbies.getText().toString().trim());
            j.put("health_notes", etHealth.getText().toString().trim());
            j.put("taboo", etTaboo.getText().toString().trim());
            j.put("persona_style", etPersonaStyle.getText().toString().trim());
            j.put("persona_dialect", etPersonaDialect.getText().toString().trim());
            j.put("persona_topics", etPersonaTopics.getText().toString().trim());
            j.put("persona_custom", etPersonaCustom.getText().toString().trim());
            j.put("call_time", etCallTime.getText().toString().trim());
        } catch (Exception ignored) {}
        return j.toString();
    }

    private String baseUrl() {
        String s = etUrl.getText().toString().trim();
        if (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }

    // ================= 本地通话记录 =================
    private void saveRecord(String mode, String summary, String quote) {
        try {
            JSONObject r = new JSONObject();
            r.put("ts", ts.format(new Date()));
            r.put("mode", mode);
            r.put("duration", callSec);
            r.put("summary", summary);
            r.put("quote", quote);
            r.put("transcript", transcript.toString());
            JSONArray arr = readRecords();
            JSONArray out = new JSONArray();
            out.put(r);
            for (int i = 0; i < arr.length() && i < 49; i++) out.put(arr.get(i));
            try (FileOutputStream fos = openFileOutput("records.json", MODE_PRIVATE)) {
                fos.write(out.toString().getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {}
    }

    private JSONArray readRecords() {
        try {
            File f = new File(getFilesDir(), "records.json");
            if (!f.exists()) return new JSONArray();
            FileInputStream fis = new FileInputStream(f);
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] b = new byte[4096]; int n;
            while ((n = fis.read(b)) > 0) bos.write(b, 0, n);
            fis.close();
            return new JSONArray(new String(bos.toByteArray(), StandardCharsets.UTF_8));
        } catch (Exception e) { return new JSONArray(); }
    }

    private void renderRecords() {
        JSONArray arr = readRecords();
        if (arr.length() == 0) { recordsView.setText("暂无记录\n\n打完一通电话后，这里会保留本机的对话记录（摘要 + 完整对话内容）。"); return; }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject r = arr.optJSONObject(i);
            if (r == null) continue;
            sb.append("📅 ").append(r.optString("ts", ""))
              .append(" · ").append("out".equals(r.optString("mode")) ? "小暖外呼" : "老人来电")
              .append(" · ").append(fmtDur(r.optInt("duration"))).append('\n')
              .append("📋 ").append(r.optString("summary", "")).append('\n');
            if (!r.optString("quote", "").isEmpty()) sb.append("💬 “").append(r.optString("quote")).append("”\n");
            sb.append("————————————————\n");
        }
        recordsView.setText(sb.toString());
    }

    private String fmtDur(int s) { return s >= 60 ? (s / 60) + "分" + (s % 60) + "秒" : s + "秒"; }

    // ================= 通话流程 =================
    private void openCallOverlay() {
        callOverlay.setVisibility(View.VISIBLE);
        log.setText("");
        transcript.setLength(0);
        callSec = 0;
        callTimer.setText("00:00");
    }

    private void closeCallOverlay() {
        callOverlay.setVisibility(View.GONE);
        switchTab(0);
    }

    private void startCallTimer() {
        if (callTick != null) ui.removeCallbacks(callTick);
        callSec = 0;
        callTimer.setText("00:00");
        callTick = new Runnable() {
            @Override public void run() {
                callSec++;
                callTimer.setText(String.format("%02d:%02d", callSec / 60, callSec % 60));
                ui.postDelayed(this, 1000);
            }
        };
        ui.postDelayed(callTick, 1000);
    }

    private void startOutbound() {
        openCallOverlay();
        show(screenDial);
        sfxPlayer = play(R.raw.dial, true);
        ui.postDelayed(this::toIncoming, 2600);
    }

    private void toIncoming() {
        stop(sfxPlayer);
        show(screenIncoming);
        sfxPlayer = play(R.raw.dial, true);
        startPulse();
    }

    private void startIncomingCall() {
        openCallOverlay();
        show(screenIncall);
        startCallTimer();
        addLog("📲 老人主动来电 · 小暖惊喜接起");
        doStart("/api/call/incoming");
    }

    private void answer() {
        stopPulse();
        stop(sfxPlayer);
        sfx(R.raw.flash);
        show(screenIncall);
        startCallTimer();
        addLog("已接通 · 通话中");
        doStart("/api/call/start");
    }

    private void doStart(String path) {
        base = baseUrl();
        persistSettings();
        callMode = path.endsWith("incoming") ? "in" : "out";
        new Thread(() -> {
            try {
                String body = postPlain(base + path);
                JSONObject j = new JSONObject(body);
                callId = j.getString("call_id");
                String greeting = j.getString("greeting");
                String audio = j.optString("audio", "");
                addLog("小暖: " + greeting);
                transcript.append("小暖: ").append(greeting).append('\n');
                playAudio(base + audio);
            } catch (Exception e) {
                addLog("❌ 接通失败: " + e.getMessage());
            }
        }).start();
    }

    // ================= 录音（硬件 AEC + 自动断句）=================
    private void startRecording() {
        if (callId == null) { toast("请先接通电话"); return; }
        int buf = Math.max(AudioRecord.getMinBufferSize(SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT), CHUNK * 4);
        recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, buf);
        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
            toast("录音初始化失败"); return;
        }
        pcm.reset();
        recording = true;
        micCircle.setBackgroundResource(R.drawable.circle_red);
        micLabel.setText("说完停顿即发送");
        addLog("🎙️ 开始聆听");
        final double[] ambient = {0.008};
        final int[] calib = {10};
        recThread = new Thread(() -> {
            recorder.startRecording();
            short[] chunk = new short[CHUNK];
            int loud = 0, quiet = 0; boolean spoken = false; long startAt = System.currentTimeMillis();
            while (recording) {
                int n = recorder.read(chunk, 0, CHUNK);
                if (n <= 0) continue;
                byte[] bytes = new byte[n * 2];
                for (int i = 0; i < n; i++) {
                    bytes[i * 2] = (byte) (chunk[i] & 0xFF);
                    bytes[i * 2 + 1] = (byte) ((chunk[i] >> 8) & 0xFF);
                }
                synchronized (pcm) { pcm.write(bytes, 0, bytes.length); }
                double sum = 0;
                for (int i = 0; i < n; i++) sum += chunk[i] * chunk[i];
                double rms = Math.sqrt(sum / n);
                double th = Math.max(ambient[0] * 2.8, 250);
                if (calib[0] > 0) { ambient[0] = Math.min(ambient[0], Math.max(rms / 1000, 0.004)); calib[0]--; continue; }
                if (rms > th) { loud++; quiet = 0; if (!spoken && loud >= 3) spoken = true; }
                else { quiet++; loud = 0; }
                boolean autoCut = spoken && quiet >= 12;
                boolean tooLong = System.currentTimeMillis() - startAt > 20000;
                if (autoCut || tooLong) { ui.post(this::stopRecording); break; }
            }
            try { recorder.stop(); } catch (Exception ignored) {}
            recorder.release();
        });
        recThread.start();
    }

    private void stopRecording() {
        if (!recording) return;
        recording = false;
        micCircle.setBackgroundResource(R.drawable.circle_gray);
        micLabel.setText("点这里说话");
        byte[] wav;
        synchronized (pcm) { wav = toWav(pcm.toByteArray(), SAMPLE_RATE); }
        if (wav.length < 32000) { addLog("（说话时间太短，已忽略）"); return; }
        addLog("🟢 说完了，小暖思考中…");
        final byte[] data = wav;
        new Thread(() -> {
            try {
                Map<String, String> fields = new LinkedHashMap<>();
                fields.put("call_id", callId);
                String body = multipartPost(base + "/api/call/turn", fields, "audio", "speech.wav", data, "audio/wav");
                JSONObject j = new JSONObject(body);
                addLog("老人: " + j.optString("you", ""));
                addLog("小暖: " + j.optString("reply", ""));
                transcript.append("老人: ").append(j.optString("you", "")).append('\n');
                transcript.append("小暖: ").append(j.optString("reply", "")).append('\n');
                playAudio(base + j.optString("audio", ""));
            } catch (Exception e) {
                addLog("❌ 对话失败: " + e.getMessage());
            }
        }).start();
    }

    // ================= 挂断 =================
    private void hangup() {
        if (callId == null) { closeCallOverlay(); return; }
        if (recording) stopRecording();
        if (callTick != null) ui.removeCallbacks(callTick);
        // 挂断瞬间：立即切"通话已结束"页 + 挂断音 + 忙音，绝不僵在通话界面
        sfx(R.raw.flash);
        ui.postDelayed(() -> sfx(R.raw.busy), 450);
        show(screenEnded);
        endDuration.setText("通话时长 " + fmtDur(callSec));
        endStatus.setText("正在生成通话摘要…");
        endSummary.setText("");
        btnRedial.setEnabled(false);
        btnBack.setEnabled(false);
        final String cid = callId;
        callId = null;
        new Thread(() -> {
            String summary = "", quote = ""; boolean pushed = false;
            String sugg = "";
            try {
                String body = postForm(base + "/api/call/end", "call_id", cid);
                JSONObject j = new JSONObject(body);
                summary = j.optString("summary", "");
                quote = j.optString("quote", "");
                pushed = j.optBoolean("pushed");
                org.json.JSONArray sa = j.optJSONArray("suggestions");
                if (sa != null) {
                    StringBuilder sb = new StringBuilder();
                    for (int i = 0; i < sa.length() && i < 3; i++)
                        sb.append("💡 ").append(sa.optString(i)).append("\n");
                    sugg = sb.toString();
                }
            } catch (Exception ignored) {}
            final String fSum = summary, fQuote = quote, fSugg = sugg;
            final boolean fPushed = pushed;
            saveRecord(callMode, summary, quote);
            ui.post(() -> {
                endStatus.setText(fPushed ? "📲 简报已推送给子女微信" : "⚠️ 微信推送失败/未配置");
                endSummary.setText((fSugg.isEmpty() ? "" : fSugg + "\n")
                        + (fQuote.isEmpty() ? "" : "💬 “" + fQuote + "”\n")
                        + "📋 " + (fSum.isEmpty() ? "(摘要生成失败)" : fSum));
                btnRedial.setEnabled(true);
                btnBack.setEnabled(true);
                renderRecords();
            });
        }).start();
    }

    // ================= 音效 / 动画 =================
    private MediaPlayer play(int res, boolean loop) {
        MediaPlayer mp = MediaPlayer.create(this, res);
        if (mp == null) return null;
        mp.setLooping(loop);
        mp.start();
        return mp;
    }

    private void sfx(int res) {
        MediaPlayer mp = MediaPlayer.create(this, res);
        if (mp == null) return;
        mp.setOnCompletionListener(MediaPlayer::release);
        mp.start();
    }

    private void stop(MediaPlayer mp) {
        if (mp == null) return;
        try { mp.stop(); mp.release(); } catch (Exception ignored) {}
    }

    private void startPulse() {
        pulseX = ObjectAnimator.ofFloat(answerCircle, "scaleX", 1f, 1.18f);
        pulseY = ObjectAnimator.ofFloat(answerCircle, "scaleY", 1f, 1.18f);
        for (ObjectAnimator a : new ObjectAnimator[]{pulseX, pulseY}) {
            a.setDuration(700);
            a.setRepeatCount(ObjectAnimator.INFINITE);
            a.setRepeatMode(ObjectAnimator.REVERSE);
            a.setInterpolator(new LinearInterpolator());
            a.start();
        }
    }

    private void stopPulse() {
        if (pulseX != null) pulseX.cancel();
        if (pulseY != null) pulseY.cancel();
        answerCircle.setScaleX(1f); answerCircle.setScaleY(1f);
    }

    private void playAudio(String url) {
        if (url == null || url.isEmpty() || !url.startsWith("http")) return;
        new Thread(() -> {
            try {
                MediaPlayer mp = new MediaPlayer();
                mp.setDataSource(url);
                mp.prepare();
                mp.start();
                mp.setOnCompletionListener(MediaPlayer::release);
            } catch (Exception ignored) {}
        }).start();
    }

    private void show(View s) {
        screenDial.setVisibility(s == screenDial ? View.VISIBLE : View.GONE);
        screenIncoming.setVisibility(s == screenIncoming ? View.VISIBLE : View.GONE);
        screenIncall.setVisibility(s == screenIncall ? View.VISIBLE : View.GONE);
        screenEnded.setVisibility(s == screenEnded ? View.VISIBLE : View.GONE);
    }

    // ================= 网络（零依赖 HttpURLConnection）=================
    private String httpGet(String urlStr) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setConnectTimeout(8000); c.setReadTimeout(20000);
        return readAll(c);
    }

    private String postJson(String urlStr, String json) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setRequestMethod("POST"); c.setDoOutput(true);
        c.setConnectTimeout(8000); c.setReadTimeout(20000);
        c.setRequestProperty("Content-Type", "application/json");
        try (OutputStream os = c.getOutputStream()) { os.write(json.getBytes(StandardCharsets.UTF_8)); }
        return readAll(c);
    }

    private String postPlain(String urlStr) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setRequestMethod("POST"); c.setDoOutput(true);
        c.setConnectTimeout(15000); c.setReadTimeout(120000);
        try (OutputStream os = c.getOutputStream()) { os.write(new byte[0]); }
        return readAll(c);
    }

    private String postForm(String urlStr, String k, String v) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setRequestMethod("POST"); c.setDoOutput(true);
        c.setConnectTimeout(15000); c.setReadTimeout(120000);
        c.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
        byte[] body = (k + "=" + java.net.URLEncoder.encode(v, "UTF-8")).getBytes(StandardCharsets.UTF_8);
        try (OutputStream os = c.getOutputStream()) { os.write(body); }
        return readAll(c);
    }

    private String multipartPost(String urlStr, Map<String, String> fields,
                                 String fileField, String fileName, byte[] fileBytes, String fileType) throws Exception {
        String boundary = "----xiaonuan" + System.currentTimeMillis();
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setRequestMethod("POST"); c.setDoOutput(true);
        c.setConnectTimeout(15000); c.setReadTimeout(120000);
        c.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        for (Map.Entry<String, String> e : fields.entrySet()) {
            out.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + e.getKey()
                    + "\"\r\n\r\n" + e.getValue() + "\r\n").getBytes(StandardCharsets.UTF_8));
        }
        out.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + fileField
                + "\"; filename=\"" + fileName + "\"\r\nContent-Type: " + fileType + "\r\n\r\n")
                .getBytes(StandardCharsets.UTF_8));
        out.write(fileBytes);
        out.write(("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));
        try (OutputStream os = c.getOutputStream()) { os.write(out.toByteArray()); }
        return readAll(c);
    }

    private String readAll(HttpURLConnection c) throws Exception {
        InputStream is = c.getResponseCode() >= 400 ? c.getErrorStream() : c.getInputStream();
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] b = new byte[4096]; int n;
        while ((n = is.read(b)) > 0) bos.write(b, 0, n);
        is.close();
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    private byte[] toWav(byte[] pcmData, int sampleRate) {
        int len = pcmData.length;
        byte[] out = new byte[44 + len];
        System.arraycopy("RIFF".getBytes(), 0, out, 0, 4);
        putInt(out, 4, 36 + len); System.arraycopy("WAVE".getBytes(), 0, out, 8, 4);
        System.arraycopy("fmt ".getBytes(), 0, out, 12, 4);
        putInt(out, 16, 16); putShort(out, 20, (short) 1); putShort(out, 22, (short) 1);
        putInt(out, 24, sampleRate); putInt(out, 28, sampleRate * 2);
        putShort(out, 32, (short) 2); putShort(out, 34, (short) 16);
        System.arraycopy("data".getBytes(), 0, out, 36, 4);
        putInt(out, 40, len);
        System.arraycopy(pcmData, 0, out, 44, len);
        return out;
    }
    private void putInt(byte[] a, int off, int v) {
        a[off] = (byte) v; a[off + 1] = (byte) (v >> 8); a[off + 2] = (byte) (v >> 16); a[off + 3] = (byte) (v >> 24);
    }
    private void putShort(byte[] a, int off, short v) {
        a[off] = (byte) v; a[off + 1] = (byte) (v >> 8);
    }

    private void addLog(String t) { ui.post(() -> log.append(t + "\n")); }
    private void toast(String t) { ui.post(() -> Toast.makeText(this, t, Toast.LENGTH_SHORT).show()); }

    @Override
    protected void onDestroy() {
        recording = false;
        stopPulse();
        stop(sfxPlayer);
        if (recorder != null) { try { recorder.release(); } catch (Exception ignored) {} }
        super.onDestroy();
    }
}
