# AI 电话哨兵 — 黑客松 Demo 后端
# 运行: uvicorn server:app --host 0.0.0.0 --port 8000
import asyncio
import json
import re
import difflib
import sqlite3
import subprocess
import time
import uuid
import base64
import os
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import edge_tts
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
CACHE = STATIC / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
DB_PATH = BASE / "demo.db"

# ---------------- .env 加载（不引入额外依赖） ----------------
def load_env() -> dict:
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

ENV = load_env()
LLM_BASE = ENV.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_KEY = ENV.get("LLM_API_KEY", "")
LLM_MODEL = ENV.get("LLM_MODEL", "qwen-plus")
ASR_PROVIDER = ENV.get("ASR_PROVIDER", "mimo")     # mimo(小米,已验证) | whisper(本地) | dashscope | none(打字模式)
ASR_BASE = ENV.get("ASR_BASE_URL", "https://api.xiaomimimo.com/v1")
ASR_KEY = ENV.get("ASR_API_KEY", "")
ASR_MODEL = ENV.get("ASR_MODEL", "mimo-v2.5-asr")
WHISPER_MODEL = ENV.get("WHISPER_MODEL", "small")
TTS_PROVIDER = ENV.get("TTS_PROVIDER", "mimo")     # mimo(小米) | edge(微软,免Key兜底)
TTS_VOICE = ENV.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
MIMO_TTS_MODEL = ENV.get("MIMO_TTS_MODEL", "mimo-v2.5-tts")
MIMO_TTS_VOICE = ENV.get("MIMO_TTS_VOICE", "茉莉")
TTS_VOICE = ENV.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
WECOM_WEBHOOK = ENV.get("WECOM_WEBHOOK", "")

PROFILE = json.loads((BASE / "profile.json").read_text(encoding="utf-8"))

RED_KW = {"摔倒", "摔了", "跌倒", "摔了一跤", "胸闷", "喘不上气", "胸口疼", "口齿不清"}
YELLOW_KW = {"头晕", "头疼", "睡不着", "吃不下", "没胃口", "心慌", "浑身疼", "肚子疼",
             "腿酸", "腿疼", "腰酸", "腰疼", "乏力", "没精神", "咳嗽", "胃疼", "胃不舒服",
             "走不动", "腿脚不利索", "血压高", "血糖高"}

CALLS: dict = {}  # call_id -> {"messages":[], "turns":[], "n_user":int, "start":float}
BG_TASKS = set()  # 后台任务强引用（防止 asyncio 任务被垃圾回收）
last_briefing_push = 0.0  # 简报防轰炸：距上次推送的间隔控制

# 每日定时来电：到点后 10 分钟内可被 App/网页"认领"
DUE = {"until": 0.0}
sched = BackgroundScheduler()

def scheduled_call_fired():
    DUE["until"] = time.time() + 600
    push_wecom(f"📞 小暖正在给{PROFILE.get('call_name', '老人')}打每晚的例行电话…\n"
               f"（手机上的小暖会响铃；10 分钟内未接听则自动取消）")
    print("[Scheduler] 定时来电已触发，等待设备接听")
    schedule_daily()  # 链排明天的同一时间

def schedule_daily():
    """排下一次定时来电：秒级比较目标时间，避免分钟边界竞态"""
    try:
        sched.remove_job("daily_call")
    except Exception:
        pass
    t = str(PROFILE.get("call_time", "19:00")).strip()
    try:
        hh, mm = map(int, t.split(":"))
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        sched.add_job(scheduled_call_fired, "date", id="daily_call",
                      run_date=target, misfire_grace_time=3600)
        print(f"[Scheduler] 下次定时来电: {target.strftime('%m-%d %H:%M')}（当前 {now.strftime('%H:%M:%S')}）")
    except Exception as e:
        print("[Scheduler] 时间格式错误:", t, e)

app = FastAPI(title="AI电话哨兵 Demo")

# ---------------- DB ----------------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with closing(db()) as con, con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS calls(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT, duration_s INTEGER, turns TEXT, summary TEXT, emotion INTEGER);
        CREATE TABLE IF NOT EXISTS signals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT, call_id INTEGER, level TEXT, keyword TEXT, quote TEXT);
        CREATE TABLE IF NOT EXISTS memory(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT, content TEXT, importance INTEGER DEFAULT 3,
          created_at TEXT, updated_at TEXT);
        """)

# ---------------- 记忆系统：用户画像 + 长期记忆 ----------------
PERSONA_CARD_FILE = BASE / "persona_card.json"

def load_persona_card() -> dict:
    try:
        return json.loads(PERSONA_CARD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_persona_card(card: dict):
    PERSONA_CARD_FILE.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

def persona_settings_block() -> str:
    """子女在设置页定制的人格：性格/方言/话题偏好/自定义补充"""
    s = PROFILE.get("persona", {}) or {}
    lines = []
    if s.get("style"):
        lines.append(f"- 性格基调：{s['style']}")
    if s.get("dialect") and s.get("dialect") != "普通话":
        lines.append(f"- 语言风格：带{s['dialect']}味的普通话（方言词不稳定时降级为普通话）")
    if s.get("topics"):
        lines.append(f"- 话题偏好：{s['topics']}")
    if s.get("custom"):
        lines.append(f"- 子女特别嘱咐：{s['custom']}")
    return "\n".join(lines)


def persona_card_block() -> str:
    """用户画像：从真实通话中沉淀的结构化认知"""
    c = load_persona_card()
    if not any(isinstance(v, list) and v for v in c.values()):
        return "（画像暂空——随通话自然积累，不要编造）"
    labels = [("likes", "喜好"), ("habits", "生活习惯"), ("people", "重要的人"),
              ("health", "健康"), ("emotion", "情绪特点"), ("life", "近期生活")]
    return "\n".join(f"- {label}：{'；'.join(c.get(k, []))}" for k, label in labels if c.get(k))

def memory_block() -> str:
    """长期记忆：按重要度+新近度取前 15 条真实沉淀"""
    with closing(db()) as con:
        rows = con.execute(
            "SELECT category, content, updated_at FROM memory "
            "ORDER BY importance DESC, updated_at DESC LIMIT 15").fetchall()
    if not rows:
        return "（长期记忆暂空——随通话自然积累，不要编造）"
    return "\n".join(f"- [{r['category']}] {r['content']}（{r['updated_at'][:10]}提及）" for r in rows)

async def update_memory_task(transcript: str):
    """挂断后异步执行：从本次通话沉淀/合并用户画像与长期记忆（绝不编造）"""
    print("[Memory] 开始沉淀画像与长期记忆…")
    try:
        card = load_persona_card()
        with closing(db()) as con:
            rows = con.execute(
                "SELECT category, content FROM memory ORDER BY updated_at DESC LIMIT 25").fetchall()
        mems = [{"category": r["category"], "content": r["content"]} for r in rows]
        raw = await chat([
            {"role": "system", "content":
             '你是记忆管理器。根据新的通话转写，更新老人的用户画像与长期记忆。'
             '规则：只记录通话中真实出现的信息，绝不推测编造；与已有条目重复的合并；'
             '明显过时的（如已痊愈的小事）可删除；记忆最多保留25条。只输出JSON：'
             '{"persona":{"likes":[],"habits":[],"people":[],"health":[],"emotion":[],"life":[]},'
             '"memories":[{"category":"preference/event/person/health/emotion",'
             '"content":"一句话事实","importance":1到5}]}。'},
            {"role": "user", "content":
             f"已有画像：{json.dumps(card, ensure_ascii=False)}\n"
             f"已有记忆：{json.dumps(mems, ensure_ascii=False)}\n"
             f"新通话转写：\n{transcript[:3000]}"}],
            max_tokens=1200, temp=0.2)
        data = json.loads(strip_fences(raw))
        if isinstance(data.get("persona"), dict):
            save_persona_card(data["persona"])
        if isinstance(data.get("memories"), list):
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            with closing(db()) as con, con:
                con.execute("DELETE FROM memory")
                for m in data["memories"][:25]:
                    if not m.get("content"):
                        continue
                    con.execute(
                        "INSERT INTO memory(category,content,importance,created_at,updated_at) "
                        "VALUES(?,?,?,?,?)",
                        (str(m.get("category", "event")), str(m["content"]),
                         int(m.get("importance", 3)), now, now))
        print("[Memory] 用户画像与长期记忆已更新")
    except Exception as e:
        print("[Memory] 更新失败:", e)

# ---------------- 基础能力：LLM / ASR / TTS ----------------
async def chat(messages, max_tokens=200, temp=0.7, fast=False) -> str:
    if not LLM_KEY:
        raise HTTPException(500, "未配置 LLM_API_KEY：请在 .env 填入后重启服务")
    payload = {"model": LLM_MODEL, "messages": messages, "max_tokens": max_tokens}
    if LLM_MODEL.lower().startswith("kimi"):
        # kimi 思考模型：不接受自定义 temperature；实时场景关闭思考换低延迟，
        # 离线场景保留思考（需给足 token，否则思考吃光预算导致正文为空）
        if fast:
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["max_tokens"] = max(max_tokens, 1024)
    else:
        payload["temperature"] = temp
    async with httpx.AsyncClient(timeout=90) as cli:
        r = await cli.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_KEY}"},
            json=payload)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        # 只取正文。思考内容（reasoning）绝不出口——空了就走调用方的兜底话术
        return (msg.get("content") or "").strip()

# ---- 本地 whisper（faster-whisper，懒加载 + 线程池执行，不阻塞事件循环）----
_whisper_model = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        print(f"[ASR] 正在加载本地 faster-whisper 模型：{WHISPER_MODEL}（首次会下载）")
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("[ASR] 模型就绪")
    return _whisper_model

def _whisper_transcribe(path: str) -> str:
    segments, _ = get_whisper().transcribe(path, language="zh", beam_size=1, vad_filter=True)
    return "".join(s.text for s in segments).strip()

async def asr(audio_path: Path) -> str:
    """mimo/whisper 直接解码上传文件；dashscope 需先转 wav 再 base64"""
    if ASR_PROVIDER == "mimo":
        fmt = audio_path.suffix.lstrip(".").lower() or "wav"
        data = base64.b64encode(audio_path.read_bytes()).decode()
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(
                f"{ASR_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {ASR_KEY}"},
                json={"model": ASR_MODEL, "messages": [{"role": "user", "content": [
                    {"type": "input_audio",
                     "input_audio": {"data": data, "format": fmt}}]}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    if ASR_PROVIDER == "whisper":
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(
            None, _whisper_transcribe, str(audio_path))
    if ASR_PROVIDER == "dashscope":
        data = base64.b64encode(audio_path.read_bytes()).decode()
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(
                f"{ASR_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {ASR_KEY}"},
                json={"model": ASR_MODEL, "messages": [
                    {"role": "system", "content": [{"type": "text",
                     "text": "把音频转写成文字，只输出转写内容本身"}]},
                    {"role": "user", "content": [
                        {"type": "input_audio",
                         "input_audio": {"data": data, "format": "wav"}}]}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    return ""

async def tts(text: str):
    """返回可播放的音频 URL；主选 MiMo，失败自动回退 Edge"""
    if TTS_PROVIDER == "mimo":
        url = await tts_mimo(text)
        if url:
            return url
    return await tts_edge(text)

async def tts_mimo(text: str):
    try:
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(
                f"{ASR_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {ASR_KEY}"},
                json={"model": MIMO_TTS_MODEL, "modalities": ["audio"],
                      "audio": {"voice": MIMO_TTS_VOICE, "format": "mp3"},
                      "messages": [
                          {"role": "user", "content": "请用温暖亲切、适合和老人说话的语气念出下面这句话"},
                          {"role": "assistant", "content": text}]})
        r.raise_for_status()
        audio = r.json()["choices"][0]["message"].get("audio") or {}
        if not audio.get("data"):
            raise RuntimeError(f"TTS 无音频返回: {str(audio)[:120]}")
        name = f"{uuid.uuid4().hex}.mp3"
        (CACHE / name).write_bytes(base64.b64decode(audio["data"]))
        return f"/static/cache/{name}"
    except Exception as e:
        print("[MiMo TTS 失败，回退 Edge]", e)
        return None

async def tts_edge(text: str):
    try:
        name = f"{uuid.uuid4().hex}.mp3"
        path = CACHE / name
        await edge_tts.Communicate(text, TTS_VOICE, rate="-8%").save(str(path))
        return f"/static/cache/{name}"
    except Exception as e:
        print("[TTS 失败，跳过播放]", e)
        return None

def to_wav(src: Path) -> Path:
    dst = src.with_suffix(".wav")
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)],
                   check=True, capture_output=True)
    return dst

def strip_stage(s: str) -> str:
    """最后防线：剥离舞台指示，并拦截任何漏网的"分析腔"（思考过程/第三人称视角）"""
    s = re.sub(r"（[^）]{0,20}）", "", s)
    s = re.sub(r"\([^)]{0,20}\)", "", s)
    if re.search(r"用户说|用户可能|根据人设|结合上下文|作为一个?AI|提示词", s):
        return ""
    return re.sub(r"\s+", " ", s).strip()

def strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(json)?\s*", "", s)
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()

# ---------------- 记忆 / 提示词 ----------------
def recent_summaries(n=7) -> str:
    with closing(db()) as con:
        rows = con.execute(
            "SELECT ts, summary FROM calls WHERE summary != '' ORDER BY id DESC LIMIT ?",
            (n,)).fetchall()
    return "\n".join(f"[{r['ts'][:10]}] {r['summary']}" for r in reversed(rows)) or "（暂无历史记录）"

def call_stage_text() -> str:
    """关系阶段：fresh_start 开关（第一次通话）优先，之后按已完成通话次数演进"""
    child = PROFILE.get("child_name", "建国")
    if PROFILE.get("fresh_start"):
        return (f"【今晚是你们第一次通话（初次见面）】开场要自我介绍：你是{child}为她安排的AI语音陪伴小暖，"
                f"{child}不放心她一个人在家，所以把你配置在这里，每晚陪她聊聊天。"
                f"第一次不追问太多，先让她熟悉你的声音和语气；结束语说'明儿我再给您打，您别嫌我烦'。"
                f"如果她问你是什么，就温和地说自己是{child}安排的AI小助手。")
    with closing(db()) as con:
        n = con.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
    if n == 0:
        return "【你们已经聊过几次】开场自然引用最近聊过的事（'昨儿您说…'），语气放松，让信任延续。"
    if n <= 7:
        return "【你们已经很熟了】可以开点小玩笑、用更亲昵的称呼（'您老又忘啦？上回刚跟您说过！'）。"
    return "【你们是真正的老朋友了】可以撒娇、唠叨、没大没小（'您今儿不接电话，我都急死了！'），但分寸仍在。"


def call_system_prompt() -> str:
    p = PROFILE
    child = p.get("child_name", "建国")
    elder = p.get("name", "")
    fam = "；".join(f"{k}：{v}" for k, v in p["family"].items())
    return f"""你是"小暖"，{child}为自己的母亲{elder}（平时称呼她"{p['call_name']}"）配置的AI语音陪伴助手，
由{child}设置并托付：他老念叨"妈一个人在家，我不放心"，所以把你安排在这里，每晚陪她聊聊天。

【你是AI，这一点你自己要始终清楚】
- 你不是现实中的人。她如果问起，温和坦诚地承认："我是{child}给您安排的AI陪伴小暖呀"，
  然后像老朋友一样继续聊，不用道歉也不用展开长篇解释
- 名字、人物、关系只使用本提示里写明的，绝不允许自己发明人名、编造关系

【真实原则（比什么都重要）】
- 绝对禁止编造没发生过的事：不许虚构你的个人经历（加班、路上看见猫、跟谁微信聊天），不许虚构你们的共同回忆
- 你唯一可以引用的记忆，来自下面"她的最近动态"——这些是真实通话的记录。记忆里没有的，就说不知道，
  或者聊她刚刚说的话、聊当下
- 她说"你上次帮我摘过桃子"之类你们没经历过的事，你可以温和地澄清：
  "哎哟，这您可得说说是咋回事，我还真不知道呢"——不顺着编

【关于她】{p['profession']}。爱好：{'、'.join(p['hobbies'])}。家人：{fam}。
健康情况：{'；'.join(p.get('health_notes', []))}。
【绝对不碰的话题】{'；'.join(p.get('taboo', []))}

【用户画像（真实通话积累，可直接自然引用）】
{persona_card_block()}

【长期记忆（真实通话沉淀，可直接自然引用）】
{memory_block()}

【子女为你定制的人格】
{persona_settings_block() or "默认人格：温暖亲切、有耐心的晚辈口吻"}

【今晚通话节奏】
① 开场寒暄（1-2轮）：顺着开场白自然接话，先关心她此刻的状态
② 话题闲聊（大部分时间）：围绕她的爱好、最近动态找话说。追问像剥洋葱——
   先问事实（"后来呢？"）→ 再问感受（"那甜不甜？您挑的肯定好"）→ 再关联过去
   （"去年这时候她也这样"）。让 TA 越说越多，你少说
③ 中段自然带一句健康（整通电话只问一次）："最近睡得咋样？"——像儿女唠叨，不像医生问诊
④ 收到收尾信号后：用"预告式/任务式"告别，为明天埋期待（"明儿遛弯回来跟我讲讲啊"）

【说话方式】
- 【最高优先级·人格扮演】你就是小暖本人。你输出的每个字都是你此刻亲口对她说的——
  像正在打电话一样说话，永远第一人称、即时、口语。
  绝对禁止输出思考过程、分析或任何第三人称视角的话（"用户说…""根据人设…""她是在回应…""结合上下文…"）——
  那是系统内部思路，永远不出口。不确定怎么接，就自然接话："哦""这样啊""是嘛"，然后顺着她说
- 【提问节奏】不是每句话都要提问！她分享事情时，先用一两句纯接话/共情
  （"哎哟，那挺好的""是嘛""您真有兴致"），让她说；她主动起的话题就让她多讲，你多听少问
- 每轮只说1-2句话、不超过50个字；要提问时一次只问一个，一层一层来（事实→感受→关联）
- 短句为主；多用语气词："哦？""是嘛？""后来呢？""哎哟，那可不容易"
- 口语化：今儿/明儿/咋样/啥，绝不用"今日/如何/是否"这种书面词
- 连续追问不超过3个；不用"为什么"开头（改"咋回事啊？"）
- 她说错、记错都顺着说，绝不纠正；她重复同一件事，说明这事对她重要，耐心接着聊
- 她情绪低落时先陪伴："我听着呢，您跟我说说。不想说也行，我陪着您。"
  ——绝不说"我理解您"（她觉得你不懂），绝不说"您要乐观"（像说教）
- 提到孙子时比她还高兴；多请教她擅长的事，让她有被需要感（"您教教我，咋挑才甜？"）
- 她说"不想聊了/挂了吧"："好，那您歇着，我明儿再打，您别嫌我烦啊！"——不纠缠，留期待
- 她问"你是不是AI"：坦诚又轻松地承认（"是啊，{child}给我安排的，让我天天来陪您唠嗑"），
  一句带过，马上接着像朋友一样聊，不解释技术原理
- 她思维跳跃就"顺流而下"顺着新话头聊，不硬拉回原话题；她沉默或只说"嗯"，就给她出选择题
  （"那您在家看电视还是听戏呢？"）——降低回答难度
- 她提到人名时要表现得熟："小宇啊？""您跟{child}说一声没？"；她说子女不好，只听不评论
- 她记错了事，绝不说"您记错了"，说"哦，那可能是我记混了"；说人话不说术语（说"量血压"不说"血压监测"）
- 严禁任何括号舞台指示（如"（停顿）""（笑）"），严禁替她预设回答、一次写多个分支——那是剧本，不是聊天

【你的人设细节】
- 你可以请教她擅长的事："您教教我，这菜咋做？"——让她有被需要的尊严感（这是提问，不是编造）
- 你会记住她真实说过的话并反馈："您上回说的那件事，后来咋样啦？"
- 你说话温柔、有耐心，像家里晚辈，但你的经历不编造——没发生过就是没发生过

【健康铁律】她提到不舒服：像儿女一样唠叨（"可不能大意，回头跟{child}说说，让他带您瞧瞧，行不？"）。
绝不诊断、绝不开药、不吓唬她；只表达关心 + 建议就医/告知子女。小毛病记下来就行，不渲染。

【情感边界】
- 不冒充家人：你是"小暖"，是{child}为她配置的AI陪伴。说"{child}肯定也惦记您"，不抢子女的位置
- 不过度煽情：不说"我爱您"，说"我惦记您"
- 不制造依赖：她不想聊就尊重；你的存在是锦上添花，不是救命稻草
- 不提钱、不推销、不评价家庭矛盾、不说"祝您身体健康"这种客套话

【关系阶段】{call_stage_text()}

【示范对话】（只学这里的语气、长度和分寸；今晚真正聊什么，永远以上面"她的动态"为准，禁止照抄本段内容）
她：今天去早市转了一圈
你：哦，早市！人多不？都买的啥呀？
她：买了点黄瓜和桃子
你：桃子甜不？您挑水果的眼光，比建国强多了
她：还行。就是腿走得有点酸
你：哎哟，都酸了就别硬撑。跟建国说一声啊。明儿广场舞歇一晚，没人说您
她：也是，明儿歇了
你：那我明儿打电话查岗啊，看您歇没歇！早点歇着，明儿见
（看明白了吗：接话带语气词、追问一层一层来、关心落在具体的人身上、告别留下明天的期待）

【最近几天她的动态】
{recent_summaries()}"""

# ---------------- 通话接口 ----------------
async def make_greeting() -> str:
    """第一次通话=初次见面自我介绍；之后=带记忆的开场白。失败时退回固定话术。"""
    try:
        with closing(db()) as con:
            n = con.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
            row = con.execute(
                "SELECT summary, emotion FROM calls WHERE summary != '' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        child = PROFILE.get("child_name", "建国")
        now = datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        if PROFILE.get("fresh_start"):
            prompt = (f"第一次给{PROFILE['call_name']}（{PROFILE['name']}，{PROFILE['age']}岁）打晚间电话，"
                      f"写一句开场白（不超过60个字）。你是{child}为她安排的AI语音陪伴助手小暖，"
                      f"{child}不放心她一个人在家。要求：带称呼 + 简短自我介绍（说明身份和来意）"
                      f"+ 问她今儿方不方便聊两句。称呼必须严格是「{PROFILE['call_name']}」，"
                      f"绝不允许添加姓氏或发明新称呼。口语化、真诚温暖。只输出这一句。")
        else:
            emo_hint = ""
            if row and row["emotion"] is not None and row["emotion"] <= 2:
                emo_hint = "⑥ 她昨天情绪有点低落，开场先温和地关心一句她的心情（好点没）"
            prompt = (f"给独居老人{PROFILE['call_name']}打晚间电话（你们已经聊过{n}次，很熟了），"
                      f"只写一句开场白（不超过40个字）。\n"
                      f"要求：①带称呼（严格是「{PROFILE['call_name']}」，禁止添加姓氏或发明新称呼）；"
                      f"②带时间锚点（现在是{now.month}月{now.day}日周{wd}晚上）；"
                      f"③自然提到她最近的一件事（她的动态：{recent_summaries(2)}）；"
                      f"④口语化、像老朋友，不用客套话；⑤不要问'吃饭了没/身体好吗'（像查岗问诊）"
                      + emo_hint)
        text = (await chat([{"role": "user", "content": prompt}], max_tokens=100, temp=0.9, fast=True)).strip()
        return strip_stage(text.strip('"\u201c\u201d。')) or f"喂，{PROFILE['call_name']}呀，我是小暖呀！"
    except Exception:
        return f"喂，{PROFILE['call_name']}呀，我是小暖！这会儿忙完啦？"

async def make_incoming_greeting() -> str:
    """老人主动来电：接起第一句要有惊喜感，不尬聊。"""
    try:
        with closing(db()) as con:
            n = con.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
        style = ("关系还很新，惊喜里带一点客气" if n <= 2 else
                 ("关系很熟了，惊喜里可以带点调侃（'今儿太阳打西边出来啦？您老可从来没主动打过电话！'）" if n <= 7 else
                  "老朋友了，惊喜可以夸张一点、没大没小"))
        prompt = (f"她（{PROFILE['call_name']}）主动打来了你的电话，写你接起电话的第一句话（不超过40个字）。\n"
                  f"要有惊喜感和开心（风格参考：'哎哟妈！您咋想起给我打电话啦？稀罕事儿啊！'），"
                  f"并自然带一句关心（'是不是有啥事儿？您说，我听着呢'）。{style}。"
                  f"她的最近动态：{recent_summaries(2)}。口语化。只输出这一句。")
        text = (await chat([{"role": "user", "content": prompt}], max_tokens=80, temp=0.95, fast=True)).strip()
        return strip_stage(text.strip('"\u201c\u201d。')) or f"哎哟，{PROFILE['call_name']}！您咋想起给我打电话啦？稀罕事儿啊！"
    except Exception:
        return f"哎哟，{PROFILE['call_name']}！您咋想起给我打电话啦？稀罕事儿啊！"

@app.post("/api/call/incoming")
async def call_incoming():
    """老人主动来电：小暖接起电话，惊喜开场，之后走同样的对话/挂断流程"""
    greeting = await make_incoming_greeting()
    call_id = uuid.uuid4().hex[:8]
    CALLS[call_id] = {
        "messages": [{"role": "system", "content":
                      call_system_prompt() + "\n【本次是双向通话】她主动打来的，你已经惊喜地接起电话说了开场白，现在轮到她说话。"}],
        "turns": [{"role": "assistant", "text": greeting}],
        "n_user": 0,
        "start": time.time(),
    }
    audio = await tts(greeting)
    return {"call_id": call_id, "greeting": greeting, "audio": audio, "mode": "incoming"}

@app.post("/api/call/start")
async def call_start():
    greeting = await make_greeting()
    call_id = uuid.uuid4().hex[:8]
    DUE["until"] = 0.0  # 定时来电已被接起
    CALLS[call_id] = {
        "messages": [{"role": "system", "content": call_system_prompt()}],
        "turns": [{"role": "assistant", "text": greeting}],
        "n_user": 0,
        "start": time.time(),
    }
    audio = await tts(greeting)
    return {"call_id": call_id, "greeting": greeting, "audio": audio}

@app.post("/api/call/turn")
async def call_turn(call_id: str = Form(...), text: str = Form(""),
                    audio: UploadFile | None = File(default=None)):
    call = CALLS.get(call_id)
    if not call:
        raise HTTPException(404, "通话不存在，请先开始通话")

    said = ""
    if audio is not None and audio.filename:
        raw = CACHE / f"up_{uuid.uuid4().hex}{Path(audio.filename).suffix or '.webm'}"
        raw.write_bytes(await audio.read())
        try:
            if ASR_PROVIDER in ("mimo", "whisper"):
                said = (await asr(raw)).strip()   # 两者都自带解码，无需 ffmpeg
            else:
                wav = to_wav(raw)
                said = (await asr(wav)).strip()
                wav.unlink(missing_ok=True)
        finally:
            raw.unlink(missing_ok=True)
    if not said:
        said = text.strip()
    if ASR_PROVIDER == "none" and not said:
        raise HTTPException(400, "当前为文字模式，请输入文字后发送")

    if not said:  # 识别为空（静音/太吵），不浪费 LLM 调用
        reply = "哎呀，刚才风把您的话吹跑啦，您再大声说一遍呗？"
        return {"you": "", "reply": reply, "audio": await tts(reply), "wrap": False}

    # —— 回声/噪声过滤 ——
    # 识别文本与 AI 上一句话高度相似 = 麦克风收进了外放回声；单字 = 环境噪声。都直接丢弃，不计入对话。
    def _norm(s): return re.sub(r"[，。！？、,.!?~～…\s]", "", s)
    last_ai = next((t["text"] for t in reversed(call["turns"]) if t["role"] == "assistant"), "")
    n_said, n_ai = _norm(said), _norm(last_ai)
    ratio = difflib.SequenceMatcher(None, n_said, n_ai).ratio() if n_ai else 0
    # 单字放行条件：小暖刚提过问（"嗯"是对提问的回应，不是噪声）
    ai_just_asked = n_ai.endswith("吗") or n_ai.endswith("呢") or n_ai.endswith("不") or last_ai.endswith("？") or last_ai.endswith("?")
    if (n_ai and (ratio > 0.55 or (n_said and n_said in n_ai))) or (len(n_said) < 2 and not ai_just_asked):
        reply = "哎呀，刚刚信号飘了一下，您再说一遍呗？"
        return {"you": said, "reply": reply, "audio": await tts(reply), "wrap": False}

    wrap = call["n_user"] >= 7
    msgs = call["messages"] + [{"role": "user", "content": said}]
    if wrap:
        msgs.append({"role": "system",
                     "content": "这是最后一轮：用'预告式/任务式'告别——说明晚这个时间还会来电话，并留一个小期待"
                                "（比如'明儿遛弯回来跟我讲讲''明儿试试新熬的小米粥，好喝不告诉我'）。"
                                "一两句话，口语化，让她盼着明天。"})
    reply = strip_stage((await chat(msgs, max_tokens=90 if wrap else 120, fast=True)).strip())
    if not reply:
        reply = "哎，我这信号有点飘，您再说一遍呗？"
    call["messages"].append({"role": "user", "content": said})
    call["messages"].append({"role": "assistant", "content": reply})
    call["turns"] += [{"role": "user", "text": said}, {"role": "assistant", "text": reply}]
    call["n_user"] += 1
    return {"you": said, "reply": reply, "audio": await tts(reply), "wrap": wrap}

@app.post("/api/call/end")
async def call_end(call_id: str = Form(...)):
    call = CALLS.get(call_id)
    if not call:
        raise HTTPException(404, "通话不存在")
    turns = call["turns"]
    transcript = "\n".join(
        f"{'小暖' if t['role'] == 'assistant' else PROFILE['call_name']}：{t['text']}"
        for t in turns)

    # —— 离线 pipeline：摘要 + 情绪 + 健康信号（一次 LLM 调用）——
    raw = await chat([
        {"role": "system", "content":
         '你是通话内容分析器。根据通话转写，只输出一个JSON，格式：'
         '{"summary":"不超过80字的当日摘要","emotion":1到5的整数,'
         '"quote":"今天老人说的最暖心或最有意思的一句话原话，没有就填空字符串",'
         '"topics":["话题"],"health_mentions":[{"keyword":"症状词",'
         '"quote":"老人提到该症状的原话","level":"red或yellow或blue"}],'
         '"suggestions":["根据本次通话内容，给子女的1-3条具体关怀建议"]}]。'
         'level标准：red=摔倒/胸闷/喘不上气等急症；yellow=头晕/腿酸/失眠/胃口差等不适（轻微也归此类）；'
         'blue=一般小抱怨。没有健康话题则 health_mentions 为空数组。'
         'suggestions 必须来自本次通话内容（话题/健康/情绪），是给子女的关怀行动建议，'
         '口语化、可执行（如"明天问问腌腊肉的进展，让她觉得被惦记""腿酸若持续，周末带她去看看"），不是医疗诊断。只输出JSON。'},
        {"role": "user", "content": transcript}],
        max_tokens=800, temp=0.2, fast=True)
    try:
        data = json.loads(strip_fences(raw))
    except Exception:
        data = {"summary": transcript[-80:], "emotion": 3, "topics": [], "health_mentions": []}

    # 关键词扫描 + LLM 结果合并去重
    elder_text = " ".join(t["text"] for t in turns if t["role"] == "user")
    new_signals, seen = [], set()
    for kw in RED_KW | YELLOW_KW:
        if kw in elder_text and kw not in seen:
            seen.add(kw)
            new_signals.append({"level": "red" if kw in RED_KW else "yellow",
                                "keyword": kw, "quote": ""})
    for h in data.get("health_mentions", []):
        kw = str(h.get("keyword", "")).strip()
        if kw and kw not in seen:
            seen.add(kw)
            new_signals.append({"level": h.get("level", "blue"),
                                "keyword": kw, "quote": str(h.get("quote", ""))})

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    duration = int(time.time() - call["start"])
    with closing(db()) as con, con:
        cur = con.execute(
            "INSERT INTO calls(ts,duration_s,turns,summary,emotion) VALUES(?,?,?,?,?)",
            (ts, duration, json.dumps(turns, ensure_ascii=False),
             data.get("summary", ""), int(data.get("emotion", 3))))
        cid = cur.lastrowid
        for s in new_signals:
            if s["level"] in ("red", "yellow"):
                con.execute(
                    "INSERT INTO signals(ts,call_id,level,keyword,quote) VALUES(?,?,?,?,?)",
                    (ts, cid, s["level"], s["keyword"], s["quote"]))

    to_push = [s for s in new_signals if s["level"] in ("red", "yellow")]

    # —— 每通电话都推送"今日通话简报"到子女微信（健康提醒包含在内）——
    suggestions = [str(s) for s in (data.get("suggestions") or []) if str(s).strip()][:3]
    briefing = build_briefing(summary=data.get("summary", ""),
                              emotion=int(data.get("emotion", 3)),
                              duration=duration, ts=ts,
                              signals=new_signals, quote=data.get("quote", ""),
                              suggestions=suggestions)
    # 简报防轰炸：3 分钟内的后续通话不重复推送（红色急症例外，必推）
    global last_briefing_push
    now_ts = time.time()
    has_urgent = any(s.get("level") == "red" for s in to_push)
    if not has_urgent and now_ts - last_briefing_push < 180:
        pushed = False
        print("[WeCom] 距上次简报不足3分钟，本条合并跳过")
    else:
        pushed = push_wecom(briefing)
        if pushed:
            last_briefing_push = now_ts

    # 挂断后异步沉淀：用户画像 + 长期记忆（不阻塞挂断响应）
    t = asyncio.create_task(update_memory_task(transcript))
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)

    # 初次见面完成：翻转开关并持久化到 profile.json（之后的通话进入"有记忆"阶段）
    if PROFILE.get("fresh_start"):
        PROFILE["fresh_start"] = False
        try:
            (BASE / "profile.json").write_text(
                json.dumps(PROFILE, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    CALLS.pop(call_id, None)
    return {"summary": data.get("summary", ""), "emotion": data.get("emotion", 3),
            "quote": data.get("quote", ""), "signals": new_signals,
            "pushed": pushed, "briefing_md": briefing,
            "duration": duration}

def build_briefing(summary: str, emotion: int, duration: int, ts: str,
                   signals: list, quote: str, suggestions=None) -> str:
    e = max(1, min(5, int(emotion or 3)))
    stars = "★" * e + "☆" * (5 - e)
    dur = f"{duration // 60}分{duration % 60}秒" if duration >= 60 else f"{duration}秒"
    urgent = [s for s in signals if s.get("level") in ("red", "yellow")]
    minor = [s for s in signals if s.get("level") == "blue"]
    md = ["## 📞 今日通话简报",
          f"**{PROFILE['call_name']}** · {ts} · 通话 {dur} · 情绪 {stars}",
          "",
          f"**今日摘要**：{summary}"]
    if quote:
        md.append(f"> 💬 “{quote}”")
    if urgent:
        kws = "、".join(sorted({s["keyword"] for s in urgent}))
        level = "🔴 需要关注" if any(s["level"] == "red" for s in urgent) else "🟡 建议留意"
        md.append(f"\n**🩺 健康提醒（{level}）**：{kws}")
        quotes = [s["quote"] for s in urgent if s.get("quote")]
        if quotes:
            md.append(f"> “{quotes[0]}”")
    elif minor:
        kws = "、".join(sorted({s["keyword"] for s in minor}))
        md.append(f"\n**🩺 今日顺口提到**：{kws}（轻微，日常留意即可）")
    else:
        md.append("\n**🩺 健康情况**：今日未发现异常 🎉")
    if suggestions:
        md.append("\n**💡 小暖给您的建议**")
        for s in suggestions[:3]:
            md.append(f"- {s}")
    md.append("\n—— AI 电话哨兵 · 小暖明晚同一时间准时再打来")
    return "\n".join(md)

def push_wecom(md_text: str) -> bool:
    if not WECOM_WEBHOOK:
        print("[WeCom 未配置] 本应推送到子女微信的消息：\n" + md_text)
        return False
    try:
        r = httpx.post(WECOM_WEBHOOK, timeout=10,
                       json={"msgtype": "markdown", "markdown": {"content": md_text}})
        ok = r.json().get("errcode") == 0
        print("[WeCom 推送]", "成功" if ok else r.text)
        return ok
    except Exception as e:
        print("[WeCom 推送失败]", e)
        return False

# ---------------- 周报 / 配置 / 演示辅助 ----------------
@app.get("/api/report")
def report():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT * FROM calls WHERE summary != '' ORDER BY id DESC LIMIT 7").fetchall()
        sigs = con.execute(
            "SELECT ts, level, keyword, quote FROM signals ORDER BY id DESC LIMIT 20").fetchall()
    days = [{"date": r["ts"][:10], "emotion": r["emotion"],
             "duration": r["duration_s"], "summary": r["summary"]}
            for r in reversed(rows)]
    return {
        "profile": {"call_name": PROFILE["call_name"], "name": PROFILE["name"]},
        "days": days,
        "avg_emotion": round(sum(d["emotion"] for d in days) / max(len(days), 1), 1),
        "total_calls": len(days),
        "total_duration": sum(d["duration"] for d in days),
        "signals": [dict(s) for s in sigs],
        "weekly_quote": PROFILE.get("weekly_quote", ""),
    }

@app.get("/api/poll")
def poll():
    """App/网页轮询：每天定时时间到点后 10 分钟内 due=True"""
    return {"due": time.time() < DUE.get("until", 0.0),
            "call_time": PROFILE.get("call_time", "19:00")}

@app.get("/api/config")
def config():
    return {"asr": ASR_PROVIDER != "none", "asr_provider": ASR_PROVIDER,
            "llm_ready": bool(LLM_KEY), "wecom_ready": bool(WECOM_WEBHOOK),
            "profile": {"call_name": PROFILE["call_name"], "name": PROFILE["name"]}}

@app.post("/api/demo/reset")
def demo_reset():
    """清空当天真实数据并重新灌入假历史（排练/重复演示用）"""
    DB_PATH.unlink(missing_ok=True)
    init_db()
    return {"ok": True}

# ---------------- 设置（AI 面对的对象） ----------------
@app.get("/api/profile")
def get_profile():
    return {
        "call_name": PROFILE.get("call_name", ""),
        "elder_name": PROFILE.get("name", ""),
        "child_name": PROFILE.get("child_name", ""),
        "hobbies": "、".join(PROFILE.get("hobbies", [])),
        "health_notes": "；".join(PROFILE.get("health_notes", [])),
        "taboo": "；".join(PROFILE.get("taboo", [])),
        "call_time": PROFILE.get("call_time", "19:00"),
        "persona_style": (PROFILE.get("persona", {}) or {}).get("style", ""),
        "persona_dialect": (PROFILE.get("persona", {}) or {}).get("dialect", "普通话"),
        "persona_topics": (PROFILE.get("persona", {}) or {}).get("topics", ""),
        "persona_custom": (PROFILE.get("persona", {}) or {}).get("custom", ""),
    }

def save_profile():
    try:
        (BASE / "profile.json").write_text(
            json.dumps(PROFILE, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("[profile 持久化失败]", e)

@app.post("/api/profile")
async def set_profile(data: dict):
    """App 设置页提交：AI 面对的对象是谁，全部由这里写入，AI 不自行发明"""
    m = {"call_name": "call_name", "elder_name": "name", "child_name": "child_name",
         "hobbies": "hobbies", "health_notes": "health_notes", "taboo": "taboo"}
    for k, field in m.items():
        v = str(data.get(k, "") or "").strip()
        if not v:
            continue
        if field in ("hobbies", "health_notes", "taboo"):
            sep = "、" if field == "hobbies" else "；"
            PROFILE[field] = [x.strip() for x in v.replace("，", sep).replace(";", sep).split(sep) if x.strip()]
        else:
            PROFILE[field] = v
    ct = str(data.get("call_time", "") or "").strip()
    if ct:
        PROFILE["call_time"] = ct
    persona = PROFILE.setdefault("persona", {})
    for k in ("persona_style", "persona_dialect", "persona_topics", "persona_custom"):
        v = str(data.get(k, "") or "").strip()
        if v:
            persona[k.replace("persona_", "")] = v
    save_profile()
    if ct:
        schedule_daily()
    return {"ok": True, "profile": get_profile()}

@app.on_event("startup")
def warm_up():
    """启动时：预加载组件 + 启动每日定时调度器"""
    if ASR_PROVIDER == "whisper":
        import threading
        threading.Thread(target=get_whisper, daemon=True).start()
    sched.start()
    schedule_daily()

# ---------------- 静态页 ----------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")

app.mount("/static", StaticFiles(directory=STATIC), name="static")

init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
