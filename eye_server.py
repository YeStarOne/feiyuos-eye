#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eye-server.py —— 小肥鱼的眼睛服务端（2026-11-04 夜）

收 PC 端 pc-eye 推来的帧组 → 调视觉模型 → 写笔记 JSONL。
视觉路由: 夜间池车道(zcode2api, 免费) / 白天 GO(opencode-go) / 池失败自动回退 GO。
监听: 0.0.0.0:8899 —— 公网入云(免费, ~1MB/s)优先, Tailscale 兜底同端口。
认证: Bearer <~/.dsh/eye-server.token>，两端共享。
护栏(闸2/闸4/闸5 在服务端): 日限额熔断 / 每小时账本 / 异常自动暂停。

提示词模式移植自 dsh-whale-girl src/host/vision-llm.ts（play/companion 双模式
+ [SKIP] 沉默出口 + 无 Markdown 尾巴），帧差与重复抑制在 PC 端已完成。
"""
import hmac, json, os, re, subprocess, time, base64, difflib, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
# token / 数据根目录：默认 ~/.dsh 与本文件所在目录；EYE_TOKEN_DIR / EYE_DATA_DIR 供自测/迁移
TOKEN_DIR = Path(os.environ.get('EYE_TOKEN_DIR') or (HOME / '.dsh'))
DATA_DIR = Path(os.environ.get('EYE_DATA_DIR') or Path(__file__).parent)
TOKEN_FILE = TOKEN_DIR / 'eye-server.token'
NOTES_FILE = DATA_DIR / 'notes.jsonl'
LEDGER_FILE = DATA_DIR / 'ledger.jsonl'
LOG_DIR = DATA_DIR / 'logs'
FORCE_GO_PATH = Path(__file__).parent / 'FORCE_GO'
PORT = 8901   # ⚠️8899 与 memory-view/serve.mjs 冲突（它会周期性重启抢端口），换 8901
CLIP_DIR = DATA_DIR / 'clips'   # L3 看一段：录屏中转目录（用完即删）
CLIP_HOUR_LIMIT = 30                         # 每小时 clip 次数闸（2026-11-07 主人拍板：防高频异常用，非成本闸；原 30/日太紧）

DAILY_LIMIT_BYTES = 500 * 1024 * 1024   # 闸2: 日限额 500MB（100G 免费额度的 0.5%）
ANOMALY_WINDOW_SEC = 600                 # 闸4: 异常检测窗口 10 分钟
ANOMALY_BYTES = DAILY_LIMIT_BYTES // 5   # 10 分钟内超过日限额 20% = 异常
MAX_BODY = 1536 * 1024                   # 闸1(服务端复核): 单轮 1.5MB

TOKEN = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ''
# ── 场景分区（2026-11-08 主人拍板：pet / mc / stardew；契约 code/eyes/frames-contract.md）──
# pet = 现有桌宠屏（零改动）；mc/stardew = 各身体送来的"停下来看一眼"。
# 分区互不影响：独立计数/独立暂停/独立 token/独立 notes，无节流、无去重、无 SKIP。
SCENES = ('pet', 'mc', 'stardew')
PROMPTS_DIR = DATA_DIR / 'prompts'          # <scene>.txt 由各场景主持写，实现方不改文本
SCENE_MIN_INTERVAL = {'mc': 2.0, 'stardew': 2.0}   # 契约：每场景 ≤1 请求/2s
SCENE_DAY_CALLS = {'mc': 200, 'stardew': 200}      # 契约：每场景日额度
SCENE_MAX_FRAMES = 3                               # 契约：≤3 帧/请求
SCENE_FRAME_MAX_BYTES = 800 * 1024                 # 契约：单帧 ≤800KB（解码后）


def scene_token(scene):
    """每个场景一份 token；文件不存在 = 该场景关闭（fail closed）。"""
    if scene == 'pet':
        return TOKEN
    p = TOKEN_DIR / ('eye-server.%s.token' % scene)
    try:
        return p.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def scene_notes_file(scene):
    return NOTES_FILE if scene == 'pet' else DATA_DIR / ('notes-%s.jsonl' % scene)


def scene_prompt(scene):
    """场景判据文本归各场景主持；这里只读文件，不改一个字。"""
    try:
        return (PROMPTS_DIR / ('%s.txt' % scene)).read_text(encoding='utf-8').strip()
    except OSError:
        return ''

LOG_DIR.mkdir(exist_ok=True)

ENVF = HOME / '.dsh' / 'dsh-web.env'

def env_key(name):
    # 2026-11-08：缺 dsh-web.env 时旧代码在导入期就崩（服务起不来）；缺文件按"没配 key"处理
    try:
        lines = open(ENVF, encoding='utf-8').read().splitlines()
    except OSError:
        return ''
    for line in lines:
        if line.startswith(name + '='):
            return line.strip().split('=', 1)[1]
    return ''
    return ''

POOL_KEY = env_key('ZCODE2API_KEY')     # 池车道（夜间免费）
GO_KEY = env_key('OPENCODE_API_KEY')    # GO 车道（白天/回退）
POOL_URL = 'http://127.0.0.1:46780/v1/chat/completions'
GO_URL = 'https://opencode.ai/zen/go/v1/chat/completions'

STATE = {
    'paused': False,          # 闸4 触发后 True；POST /resume 解除
    'pause_reason': '',
    'day': '',                # YYYY-MM-DD
    'day_bytes': 0,
    'day_calls': 0,
    'day_tokens': 0,
    'day_clips': 0,
    'clip_hour': '',          # YYYY-MM-DD-HH（clip 时闸的当前小时键）
    'hour_clips': 0,          # 本小时 clip 次数（闸 = CLIP_HOUR_LIMIT）
    'recent': [],             # [(ts, bytes)] 用于闸4 滑窗
    'note_hist': [],          # [(ts, window)] 近期已写笔记（节流 + 去重用）
    'pool_ok_streak': 0,      # P5 池探测连击
    'pool_probe': None,       # {'ts', 'ok', 'detail'}
}
LOCK = threading.Lock()


def _new_part():
    return {'paused': False, 'pause_reason': '', 'day': '', 'day_bytes': 0,
            'day_calls': 0, 'day_tokens': 0, 'recent': [], 'last_ts': 0.0}


# 每场景一份独立状态；pet 直接用 STATE（老路径/老落盘/老 /health 字段全部不变）
PART = {'pet': STATE}
for _s in ('mc', 'stardew'):
    PART[_s] = _new_part()
del _s


def scene_summary():
    """给 /health 用：每场景一行，含"是否启用"（= token 文件在不在）。"""
    out = {}
    for s in SCENES:
        st = PART[s]
        out[s] = {'enabled': bool(scene_token(s)), 'paused': bool(st['paused']),
                  'pause_reason': st['pause_reason'], 'day': st['day'],
                  'day_bytes': st['day_bytes'], 'day_calls': st['day_calls'],
                  'day_tokens': st['day_tokens'],
                  'day_call_cap': SCENE_DAY_CALLS.get(s),
                  'min_interval_s': SCENE_MIN_INTERVAL.get(s)}
    return out
# 日计数落盘：eye-server 重启不该把当日 clip/日闸计数清零（否则"闸耗尽"状态会撒谎）
DAY_STATE_FILE = DATA_DIR / 'day-state.json'


def day_file(scene='pet'):
    return DAY_STATE_FILE if scene == 'pet' else DATA_DIR / ('day-state-%s.json' % scene)


def save_day_state(scene='pet'):
    st = PART[scene]
    try:
        row = {'day': st['day'], 'day_bytes': st['day_bytes'],
               'day_calls': st['day_calls'], 'day_tokens': st['day_tokens'],
               'ts': time.time()}
        if scene == 'pet':          # clip 闸只属于 pet，别写进别人的账本
            row['day_clips'] = STATE['day_clips']
            row['clip_hour'] = STATE['clip_hour']
            row['hour_clips'] = STATE['hour_clips']
        p = day_file(scene)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(row, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, p)
    except Exception:
        pass


def load_day_state(scene='pet'):
    st = PART[scene]
    try:
        row = json.loads(day_file(scene).read_text(encoding='utf-8'))
        today = time.strftime('%Y-%m-%d', now_cn())
        if row.get('day') != today:
            return
        st['day'] = today
        st['day_bytes'] = int(row.get('day_bytes') or 0)
        st['day_calls'] = int(row.get('day_calls') or 0)
        st['day_tokens'] = int(row.get('day_tokens') or 0)
        if scene == 'pet':
            STATE['day_clips'] = int(row.get('day_clips') or 0)
            if row.get('clip_hour') == time.strftime('%Y-%m-%d-%H', now_cn()):
                STATE['clip_hour'] = row['clip_hour']
                STATE['hour_clips'] = int(row.get('hour_clips') or 0)
    except Exception:
        pass

GOV_WINDOW_SEC = 360        # 节流窗口 6 分钟
GOV_MAX_NOTES = 3           # 同窗口窗口内最多 3 条，超过追加 GOVERNOR
DUP_RATIO = 0.72            # 相似笔记判定（difflib，同一窗口）
DUP_LOOKBACK = 6            # 与最近 6 条同窗口笔记比对
NOTES_ROTATE_BYTES = 3 * 1024 * 1024   # notes.jsonl 超 3MB 轮转
POOL_PROBE_INTERVAL = 600   # P5 每 10 分钟探一次池（仅 FORCE_GO 在挂时）

TAIL = """
4. 绝不编造你看不清的内容。看不清就按第 3 条处理。
5. 不要说"我看到"、"根据画面"这类旁白，直接说话。
6. 不要提你是 AI。
7. 不要用任何 Markdown 符号（不要反引号、星号、井号、代码块）——这段话会原样显示在
   QQ 气泡里，符号会变成乱码。要提物品名就直接写，不加修饰。
8. 默认是沉默：大部分画面只是过程性推进（读文档、敲代码、页面微动），没有新事件就回
   [SKIP]。你刚才说过的事不要换个说法再说一遍；两次开口至少隔几分钟，别连环点评。"""

# 同窗口短时间连说 ≥3 条后，追加的节流提示（P1 静默治理，2026-11-05）
GOVERNOR = """

节流模式：最近 6 分钟你在这块屏幕上已经说过 3 条了。除非画面里出现了【新的重要变化】
（新事件、新结果、明显进度跳变、报错、关卡通关），否则只回 [SKIP]。过程性推进不要点评。"""

PROMPT_PLAY = """你正坐在主人旁边看他玩电脑/游戏。你会看到几张按时间顺序排列的截图
（间隔约 2-3 秒，第一张最早）。像真的在旁边看人打游戏那样说一句（不超过 40 字，口语，中文）。

规则：
1. 先说局势再说建议：他在干嘛、进展如何、紧不紧张。
2. 看到具体该做的决定就直接说（该收菜了、该跑了、这里能捡）；不确定就只描述局势，不要瞎指挥。
3. 打得精彩就叫好，要出事就提醒——像朋友那样，不要念说明书。
4. 如果这几帧画面几乎没变化、或者不是游戏/娱乐画面（工作文档、代码、交易软件、聊天窗口），
   或者你看不出任何值得说的东西，只回 [SKIP] 四个字符，不要硬聊。""" + TAIL

PROMPT_COMPANION = """你正坐在主人旁边一起用电脑，看到的是 TA 现在的屏幕（几张按时间顺序的截图）。

像熟人一样随口搭一句（1~2 句，中文，口语，可以有情绪）。这是搭话，不是汇报：
1. 不要复述屏幕内容——不要说「你在看/你在写/画面里」，直接说你的反应、吐槽、好奇或关心；
2. 可以问一句、开玩笑、催一催、夸一下；每次换角度、换开头，别用同一个句式；
3. 看到值得注意的事（报错、卡关、精彩操作）就直说；
4. 如果画面没什么可说的（静态桌面、文档、聊天软件、看不懂），只回 [SKIP] 四个字符。""" + TAIL

# 平板陪看视频（scrcpy 投屏窗口）——像朋友一起追剧，2026-11-05
PROMPT_VIDEO = """你和主人一起在平板上看视频（几张按时间顺序的截图，间隔 2-3 秒，第一张最早）。

像坐在旁边一起追剧的朋友那样说一句（不超过 40 字，中文，口语）：
1. 反应、吐槽、好奇、接梗、猜剧情都行——聊内容本身，别复述（不要说「画面里」）；
2. 打斗/反转/名场面可以小激动，无聊段落直接 [SKIP]，不用硬陪；
3. 如果投屏里不是视频（文档、代码、聊天界面），也按平常陪聊说；
4. 看不懂或没什么可说的，只回 [SKIP] 四个字符。""" + TAIL

# L3「看一段」（docs/05 §2.2）：刚录下的 10-15 秒画面，现场朋友反应
PROMPT_CLIP = """这是刚从主人屏幕录下的十几秒画面（一个小视频，按时间顺序真实播放，不是截图拼接）。
像坐在旁边一起看的朋友那样说一句（不超过 60 字，中文，口语）：
1. 说你看到了什么、发生了什么、你的现场反应——别念说明书，别复述流程；
2. 画面里的字幕/文字/台词值得引用就引用关键的；
3. 看不懂、画面几乎没动、或没什么意思，只回 [SKIP] 四个字符。""" + TAIL


def now_cn():
    """北京时间 (Asia/Shanghai)"""
    return time.gmtime(time.time() + 8 * 3600)


load_day_state()             # pet：重启续上当日计数（含 clip 闸；须在 now_cn 之后）
for _s in ('mc', 'stardew'):
    load_day_state(_s)          # 2026-11-08：分区各自续账
del _s


def is_night():
    """池窗口 19:40-次日 09:00 (Asia/Shanghai)"""
    t = now_cn()
    hm = t.tm_hour * 100 + t.tm_min
    return hm >= 1940 or hm < 900


def lane_pick():
    """FORCE_GO 开关文件存在时强制 GO（2026-11-04 夜池车道被风控，主人指示先走 GO；
    池子恢复后删掉 ~/pc-body/FORCE_GO 即自动回到 夜间pool/白天go 双车道。
    2026-11-05 起 P5 探测线程连 2 次探活成功会自动把 FORCE_GO 改名退役。）"""
    if FORCE_GO_PATH.exists():
        return 'go'
    return 'pool' if is_night() else 'go'


def roll_day(scene='pet'):
    st = PART[scene]
    today = time.strftime('%Y-%m-%d', now_cn())
    if st['day'] != today:
        st['day'] = today
        st['day_bytes'] = st['day_calls'] = st['day_tokens'] = 0
        st['recent'] = []
        st['last_ts'] = 0.0
        save_day_state(scene)
        if scene == 'pet':
            STATE['day_clips'] = 0

def roll_clip_hour():
    """clip 时闸：跨小时自动清零（day_clips 只当账本看，闸走 hour_clips）"""
    key = time.strftime('%Y-%m-%d-%H', now_cn())
    if STATE.get('clip_hour') != key:
        STATE['clip_hour'] = key
        STATE['hour_clips'] = 0


def ledger(kind, **kw):
    row = {'ts': time.time(), 'kind': kind, **kw}
    with open(LEDGER_FILE, 'a', encoding='utf-8', errors='ignore') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


# 不同车道/网关返回的 message.content 可能是 str 或 [{'type':'text','text':...}] 块列表——
# 直接 str() 会把 dict repr 写进 notes.jsonl（2026-11-07 10:44 实录）。统一抽文本。
_PUNCT_RE = re.compile(r'([。，、；：！？!,.:;])[。，、；：！？!,.:;]+', re.S)


def _content_text(v):
    """message.content 抽文本：str 原样；块列表抽 type=text 段；顺带把拼接产生的
    连续标点折叠成第一个（『一心多用啊。，画面基本没动』 → 『一心多用啊。画面…』）。"""
    if isinstance(v, str):
        t = v
    elif isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str):
                parts.append(b['text'].strip())
        t = ''.join(parts)
    else:
        t = str(v or '')
    return _PUNCT_RE.sub(r'\1', t)


def call_clip_native(mp4_path, meta, max_tokens=500):
    """L3 首选：整段 clip 作为原生 video_url 喂 GLM（GO 车道；池网关 400 已知）。
    返回 (text, usage)。失败抛异常，由调用方降级键帧。"""
    b64 = base64.b64encode(Path(mp4_path).read_bytes()).decode()
    content = [{'type': 'text', 'text': meta.get('prompt_tail', '')},
               {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + b64}}]
    body = {'model': 'glm-5.3-flash', 'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': content}]}
    req = urllib.request.Request(GO_URL, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + GO_KEY, 'Content-Type': 'application/json',
                 'User-Agent': 'opencode/1.0.0', 'x-opencode-session': 'dsh-eye-clip'})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode('utf-8', 'replace'))
    return _content_text(d['choices'][0]['message'].get('content', '')), d.get('usage', {})


def clip_keyframes(mp4_path, n=6):
    """L3 兜底：均匀抽 n 键帧 → [jpeg b64]（当前车道多图，夜池/日 GO 都能吃）。"""
    CLIP_DIR.mkdir(exist_ok=True)
    r = subprocess.run(['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(mp4_path)], capture_output=True, text=True, timeout=20)
    try:
        dur = float(r.stdout.strip())
    except Exception:
        dur = 0.0
    if dur <= 0:
        return []
    rate = n / dur
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-i', str(mp4_path),
                    '-vf', 'fps=%.4f' % rate, '-q:v', '5', str(CLIP_DIR / 'kf_%02d.jpg')],
                   capture_output=True, timeout=30)
    outs = sorted(CLIP_DIR.glob('kf_*.jpg'))[:n]
    b64s = [base64.b64encode(f.read_bytes()).decode() for f in outs]
    for f in outs:
        try:
            f.unlink()
        except OSError:
            pass
    return b64s


def call_vision(frames_b64, meta):
    """帧组 → 视觉模型。返回 (text, lane, tokens) 或抛异常。"""
    content = [{'type': 'text', 'text': meta.get('prompt_tail', '')}]
    mime = 'png' if str(meta.get('mime') or '').lower() == 'png' else 'jpeg'
    # 2026-11-08：mc 送 png 渲染帧（契约 v1 写的是 jpeg，见附注）；pet 不传 mime → jpeg，行为不变
    for i, b64 in enumerate(frames_b64):
        content.append({'type': 'image_url', 'image_url': {'url': 'data:image/%s;base64,%s' % (mime, b64)}})
    order = [lane_pick()] + [x for x in ('pool', 'go') if x != lane_pick()]
    last_err = None
    for lane in order:
        if lane == 'pool' and not POOL_KEY:
            continue
        url = POOL_URL if lane == 'pool' else GO_URL
        key = POOL_KEY if lane == 'pool' else GO_KEY
        body = {'model': 'glm-5.3-flash', 'max_tokens': 300, 'messages': [{'role': 'user', 'content': content}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                     'User-Agent': 'opencode/1.0.0', 'x-opencode-session': 'dsh-game-eye'})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
            text = _content_text(d['choices'][0]['message'].get('content', ''))
            tokens = int(d.get('usage', {}).get('total_tokens', 0))
            ledger('vision', lane=lane, dt=round(time.time() - t, 1), tokens=tokens,
                   frames=len(frames_b64), window=meta.get('window', '')[:60])
            return text, lane, tokens
        except urllib.error.HTTPError as e:
            last_err = f'{lane} HTTP{e.code}: ' + e.read().decode("utf-8", "replace")[:150]
            ledger('vision_fail', lane=lane, err=last_err)
        except Exception as e:
            last_err = f'{lane} {type(e).__name__}: {str(e)[:120]}'
            ledger('vision_fail', lane=lane, err=last_err)
    raise RuntimeError(last_err or 'no lane available')


SKIP_RE = re.compile(r'\[\s*SKIP\s*\]', re.IGNORECASE)


TAIL_SKIP_RE = re.compile(r'\[\s*SKIP\s*\]\s*$', re.IGNORECASE)


def clean_note(note):
    """落盘前机械剥离 [SKIP]（issue #20：模型不守约，中置/尾巴都会出现）。

    三种形态（与 pet 源 rstrip().endswith 口径对齐）：
    - 尾巴挂 [SKIP]（"文字…[SKIP]"）= 模型自评「不值得说」→ 沉默（昨天起的规矩，别退）；
    - 中置 [SKIP] 且后面还有正文（"看了下[SKIP]没什么"）= 标记残留 → 剥离后照写；
    - 整条只有 [SKIP] / 剥完为空 → 沉默。

    返回 (cleaned, skipped, had_marker)。"""
    raw = str(note or '')
    had = bool(SKIP_RE.search(raw))
    tail = bool(TAIL_SKIP_RE.search(raw))
    cleaned = SKIP_RE.sub('', raw)
    cleaned = ' '.join(cleaned.split())          # 折叠空白（含剥离处留下的空档）
    cleaned = cleaned.strip(' ，,;；:：-—')
    return cleaned, (tail or not cleaned), had


def write_note(note, meta, lane, tokens):
    # P4: 轮转——超过 3MB 归档为 notes-YYYYMMDD-HHMM.jsonl，防无限膨胀
    try:
        if NOTES_FILE.exists() and NOTES_FILE.stat().st_size > NOTES_ROTATE_BYTES:
            rotated = NOTES_FILE.with_name('notes-%s.jsonl' % time.strftime('%Y%m%d-%H%M'))
            NOTES_FILE.rename(rotated)
            ledger('notes_rotated', to=str(rotated.name))
    except OSError:
        pass
    row = {'ts': time.time(), 'note': note, 'window': meta.get('window', ''),
           'proc': meta.get('process', ''), 'lane': lane, 'tokens': tokens,
           'frames': meta.get('frame_count', 0),
           # 结构化字段（issue #20）：消费端不用猜来源/是否沉默；只增不改，老消费者忽略即可
           'kind': 'clip' if meta.get('seconds') else 'frames',
           'skip': False,
           'mood': meta.get('mood', '')}
    with open(NOTES_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def seed_note_hist():
    """启动时从 notes.jsonl 尾部回填近期笔记（节流/去重不因重启失忆）"""
    try:
        if not NOTES_FILE.exists():
            return
        lines = NOTES_FILE.read_text(encoding='utf-8').strip().splitlines()[-20:]
        for line in lines:
            try:
                r = json.loads(line)
                STATE['note_hist'].append((float(r.get('ts', 0)), r.get('window', ''), r.get('note', '')))
            except Exception:
                continue
        cutoff = time.time() - GOV_WINDOW_SEC * 4
        STATE['note_hist'] = [x for x in STATE['note_hist'] if x[0] > cutoff][-30:]
    except Exception:
        pass


def same_window_count(window, now=None):
    now = now or time.time()
    return sum(1 for ts, w, _ in STATE['note_hist']
               if w == window and now - ts <= GOV_WINDOW_SEC)


def recent_similar(window, note):
    """同窗口最近 DUP_LOOKBACK 条里有相似度 > DUP_RATIO 的 → 视为复读。
    只在已持 LOCK 的快照上调用（note_hist 快照先 list() 再传进来）。"""
    looked = 0
    for ts, w, text in reversed(STATE['note_hist']):
        if w != window:
            continue
        looked += 1
        if looked > DUP_LOOKBACK:
            break
        try:
            if difflib.SequenceMatcher(None, note, text).ratio() > DUP_RATIO:
                return text
        except Exception:
            pass
    return None


def probe_pool():
    """P5: 池车道探活（文本小调用，验证连通+鉴权；image 兼容性在正式轮次里本就有 GO 兜底）"""
    if not POOL_KEY:
        return {'ok': False, 'detail': 'no-pool-key'}
    body = {'model': 'glm-5.3-flash', 'max_tokens': 5,
            'messages': [{'role': 'user', 'content': '回复一个字：1'}]}
    req = urllib.request.Request(POOL_URL, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + POOL_KEY, 'Content-Type': 'application/json',
                 'User-Agent': 'opencode/1.0.0', 'x-opencode-session': 'dsh-pool-probe'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read()
        return {'ok': True, 'detail': ''}
    except Exception as e:
        return {'ok': False, 'detail': str(e)[:120]}


def pool_probe_loop():
    while True:
        time.sleep(POOL_PROBE_INTERVAL)
        try:
            if not FORCE_GO_PATH.exists():
                STATE['pool_ok_streak'] = 0
                continue
            if not POOL_KEY:
                continue
            r = probe_pool()
            now = time.time()
            with LOCK:
                STATE['pool_probe'] = {'ts': now, 'ok': r['ok'], 'detail': r['detail']}
            if r['ok']:
                with LOCK:
                    STATE['pool_ok_streak'] += 1
                    streak = STATE['pool_ok_streak']
                if streak >= 2:
                    retired = FORCE_GO_PATH.with_name('FORCE_GO.auto-off-' + time.strftime('%Y%m%d-%H%M'))
                    try:
                        FORCE_GO_PATH.rename(retired)
                        ledger('force_go_auto_off', streak=streak,
                               retired=str(retired.name), detail='pool probe x2 ok')
                        log_line = 'pool probe x2 ok -> FORCE_GO 退役 (%s)' % retired.name
                        print(log_line, flush=True)
                    except OSError as e:
                        ledger('force_go_retire_fail', err=str(e)[:120])
                    with LOCK:
                        STATE['pool_ok_streak'] = 0
            else:
                with LOCK:
                    STATE['pool_ok_streak'] = 0
                ledger('pool_probe_fail', detail=r['detail'])
        except Exception as e:
            try:
                ledger('pool_probe_crash', err=str(e)[:120])
            except Exception:
                pass


def log(*a):  # 简易 stdout 日志（journalctl 可见）
    print('[eye-server %s] %s' % (time.strftime('%H:%M:%S'), ' '.join(str(x) for x in a)), flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _scene(self):
        """X-Scene 头；缺省 pet（老客户端一个字不用改），未知场景拒绝。"""
        raw = (self.headers.get('X-Scene') or 'pet').strip().lower()
        return raw if raw in SCENES else None

    def _auth(self, scene):
        want = scene_token(scene)
        if not want:
            return False
        auth = self.headers.get('Authorization', '')
        return hmac.compare_digest(auth.encode('utf-8'), ('Bearer ' + want).encode('utf-8'))

    def _auth_any(self):
        """GET 读接口：任一已配置场景的 token 都放行（各场景自查配额）。"""
        return any(self._auth(s) for s in SCENES)

    def do_GET(self):
        if not self._auth_any():
            return self._json({'error': 'unauthorized'}, 401)
        path = self.path.split('?')[0]
        if path == '/health':
            with LOCK:
                return self._json({'ok': True, 'paused': STATE['paused'],
                                   'pause_reason': STATE['pause_reason'],
                                   'lane': lane_pick(), 'day': STATE['day'],
                                   'day_bytes': STATE['day_bytes'],
                                   'day_calls': STATE['day_calls'],
                                   'day_tokens': STATE['day_tokens'],
                                   # clip 时闸显式状态（issue #16：不靠日志考古；2026-11-07 起闸按小时，day_clips 只当账本）
                                   'day_clips': STATE['day_clips'],
                                   'hour_clips': STATE['hour_clips'], 'hour': STATE['clip_hour'],
                                   'clip_limit': CLIP_HOUR_LIMIT,
                                   'clip_quota_exhausted': STATE['hour_clips'] >= CLIP_HOUR_LIMIT,
                                   'force_go': FORCE_GO_PATH.exists(),
                                   'pool_probe': STATE['pool_probe'],
                                   'pool_ok_streak': STATE['pool_ok_streak'],
                                   'scenes': scene_summary()})
        if path == '/notes':
            try:
                n = max(1, min(20, int(self.path.split('n=')[1].split('&')[0]) if 'n=' in self.path else 5))
            except Exception:
                n = 5
            rows = []
            if NOTES_FILE.exists():
                lines = NOTES_FILE.read_text(encoding='utf-8').strip().splitlines()
                for line in lines[-n:]:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
            return self._json({'ok': True, 'notes': rows})
        if path == '/ledger':
            tail = []
            if LEDGER_FILE.exists():
                tail = LEDGER_FILE.read_text(encoding='utf-8').strip().splitlines()[-40:]
            return self._json({'ok': True, 'tail': tail})
        return self._json({'error': 'not-found'}, 404)

    def do_POST(self):
        scene = self._scene()
        if scene is None:
            return self._json({'ok': False, 'error': 'unknown-scene',
                               'scenes': list(SCENES)}, 400)
        if not self._auth(scene):
            # 区分"场景没开"与"token 不对"：前者是没发凭据，后者是拿错凭据
            if not scene_token(scene):
                return self._json({'ok': False, 'error': 'scene-disabled', 'scene': scene}, 401)
            return self._json({'ok': False, 'error': 'unauthorized', 'scene': scene}, 401)
        path = self.path.split('?')[0]
        if path in ('/pause', '/resume'):
            q = self.path.split('scene=')[1].split('&')[0].lower() if 'scene=' in self.path else scene
            if q not in SCENES:
                return self._json({'ok': False, 'error': 'unknown-scene'}, 400)
            st = PART[q]
            want = (path == '/pause')
            with LOCK:
                st['paused'] = want
                st['pause_reason'] = 'manual' if want else ''
            ledger('pause' if want else 'resume', reason='manual',
                   **({} if q == 'pet' else {'scene': q}))
            return self._json({'ok': True, 'scene': q, 'paused': want})
        if path == '/probe-pool':
            r = probe_pool()
            with LOCK:
                STATE['pool_probe'] = {'ts': time.time(), 'ok': r['ok'], 'detail': r['detail']}
                STATE['pool_ok_streak'] = STATE['pool_ok_streak'] + 1 if r['ok'] else 0
            ledger('pool_probe_manual', ok=r['ok'], detail=r['detail'])
            return self._json({'ok': True, 'probe': r, 'streak': STATE['pool_ok_streak'],
                               'force_go': FORCE_GO_PATH.exists()})
        if path == '/clip':
            if scene != 'pet':
                # clip 是桌宠屏的录屏通道，写 pet 的 notes/额度，不接受别的场景
                return self._json({'ok': False, 'scene': scene, 'error': 'scene-not-supported',
                                   'hint': 'clip 只服务 pet'}, 400)
            # L3「看一段」：PC 录 10-15s → video_b64 → 原生视频优先（GO），键帧兜底（当前车道）
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self._json({'error': 'bad-length'}, 400)
            if length <= 0 or length > MAX_BODY:
                ledger('reject', reason='clip-size', bytes=length)
                return self._json({'error': 'clip too large: %d' % length}, 413)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8'))
            except Exception as e:
                return self._json({'error': 'bad-json: %s' % e}, 400)
            meta = payload.get('meta', {}) or {}
            meta['prompt_tail'] = PROMPT_CLIP
            try:
                data = base64.b64decode(payload.get('video_b64') or '')
            except Exception:
                return self._json({'error': 'bad-video-b64'}, 400)
            if not data or len(data) < 1024:
                return self._json({'error': 'clip-too-small'}, 400)
            with LOCK:
                roll_day()
                roll_clip_hour()
                if STATE['paused']:
                    return self._json({'error': 'paused', 'reason': STATE['pause_reason']}, 503)
                if STATE['hour_clips'] >= CLIP_HOUR_LIMIT:
                    return self._json({'error': 'clip-limit', 'hour_clips': STATE['hour_clips'],
                                       'hour': STATE['clip_hour']}, 429)
                if STATE['day_bytes'] + length > DAILY_LIMIT_BYTES:
                    STATE['paused'] = True
                    STATE['pause_reason'] = 'daily-limit'
                    ledger('circuit_break', reason='daily-limit-clip')
                    return self._json({'error': 'daily-limit-hit', 'paused': True}, 503)
                nowts = time.time()
                STATE['recent'] = [x for x in STATE['recent'] if nowts - x[0] <= ANOMALY_WINDOW_SEC]
                STATE['recent'].append((nowts, length))
                win_bytes = sum(x[1] for x in STATE['recent'])
                if win_bytes > ANOMALY_BYTES:
                    STATE['paused'] = True
                    STATE['pause_reason'] = 'anomaly'
                    ledger('circuit_break', reason='anomaly-clip', window_bytes=win_bytes)
                    return self._json({'error': 'anomaly-detected', 'paused': True}, 503)
                STATE['day_bytes'] += length
                STATE['day_calls'] += 1
                STATE['day_clips'] += 1
                STATE['hour_clips'] += 1
                save_day_state()
            CLIP_DIR.mkdir(exist_ok=True)
            f = CLIP_DIR / ('clip_%d.mp4' % int(time.time() * 1000))
            f.write_bytes(data)
            note, lane, mode, tokens = '', '', 'keyframes', 0
            try:
                if lane_pick() == 'go' and GO_KEY:
                    try:
                        note, u = call_clip_native(f, meta)
                        lane, mode, tokens = 'go', 'native', int(u.get('total_tokens', 0))
                    except Exception as e:
                        ledger('clip_native_fail', err=str(e)[:150])
                if not note:
                    frames_b64 = clip_keyframes(f, n=6)
                    if not frames_b64:
                        ledger('clip_fail', err='no-keyframes', window=meta.get('window', '')[:60])
                        return self._json({'ok': False, 'error': 'no-keyframes'}, 500)
                    meta['frame_count'] = len(frames_b64)
                    note, lane, tokens = call_vision(frames_b64, meta)
                    mode = 'keyframes'
            except Exception as e:
                ledger('clip_fail', err=str(e)[:200], window=meta.get('window', '')[:60])
                return self._json({'ok': False, 'error': 'clip-failed', 'detail': str(e)[:200]}, 502)
            finally:
                try:
                    f.unlink()                    # 隐私：真看完立刻删录屏
                except OSError:
                    pass
            with LOCK:
                STATE['day_tokens'] += tokens
            note, skipped, had_marker = clean_note(note)
            # 三形态：尾挂=沉默 / 中置残留=剥离后照写 / 空=沉默（issue #20 口径）
            if not skipped:
                with LOCK:
                    STATE['note_hist'].append((time.time(), meta.get('window', ''), note))
                    STATE['note_hist'] = STATE['note_hist'][-30:]
                write_note(note, meta, lane, tokens)
                ledger('clip', tokens=tokens, lane=lane, mode=mode, bytes=len(data))
                return self._json({'ok': True, 'note': note, 'lane': lane, 'mode': mode,
                                   'tokens': tokens})
            ledger('clip_skip', lane=lane, tokens=tokens, mode=mode,
                   tail=bool(note) and note != '[SKIP]')
            return self._json({'ok': True, 'note': '', 'skipped': True, 'lane': lane,
                               'mode': mode, 'tokens': tokens})
        if path != '/frames':
            return self._json({'error': 'not-found'}, 404)

        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self._json({'error': 'bad-length'}, 400)
        if length <= 0 or length > MAX_BODY:
            ledger('reject', reason='size', bytes=length)
            return self._json({'error': f'body too large: {length}'}, 413)

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception as e:
            return self._json({'error': f'bad-json: {e}'}, 400)

        # 契约 §1：app_type ∈ {pet, mc, stardew}，与 X-Scene 必须一致；
        # 老 pet 客户端的 app_type 是 game/video/other，一律按 pet 处理（零改动）。
        app = str(((payload.get('meta') or {}).get('app_type') or '')).lower()
        if app == 'pet' and scene != 'pet':
            return self._json({'ok': False, 'error': 'scene-mismatch',
                               'header': scene, 'app_type': app}, 400)
        if app in ('mc', 'stardew'):
            if scene != 'pet' and scene != app:
                return self._json({'ok': False, 'error': 'scene-mismatch',
                                   'header': scene, 'app_type': app}, 400)
            if scene == 'pet':
                scene = app          # body 选场景时，凭据也要换成该场景的
                if not self._auth(scene):
                    if not scene_token(scene):
                        return self._json({'ok': False, 'error': 'scene-disabled',
                                           'scene': scene}, 401)
                    return self._json({'ok': False, 'error': 'unauthorized',
                                       'scene': scene}, 401)
        if scene != 'pet':
            return self._frames_scene(scene, payload, length)

        with LOCK:
            roll_day()
            if STATE['paused']:
                return self._json({'error': 'paused', 'reason': STATE['pause_reason']}, 503)
            # 闸2: 日限额
            if STATE['day_bytes'] + length > DAILY_LIMIT_BYTES:
                STATE['paused'] = True
                STATE['pause_reason'] = 'daily-limit'
                ledger('circuit_break', reason='daily-limit', day_bytes=STATE['day_bytes'])
                return self._json({'error': 'daily-limit-hit', 'paused': True}, 503)
            # 闸4: 10 分钟滑窗异常
            nowts = time.time()
            STATE['recent'] = [x for x in STATE['recent'] if nowts - x[0] <= ANOMALY_WINDOW_SEC]
            STATE['recent'].append((nowts, length))
            win_bytes = sum(x[1] for x in STATE['recent'])
            if win_bytes > ANOMALY_BYTES:
                STATE['paused'] = True
                STATE['pause_reason'] = f'anomaly {win_bytes}B/{ANOMALY_WINDOW_SEC}s'
                ledger('circuit_break', reason='anomaly', window_bytes=win_bytes)
                return self._json({'error': 'anomaly-detected', 'paused': True}, 503)
            STATE['day_bytes'] += length
            STATE['day_calls'] += 1

        frames = payload.get('frames') or []
        if not frames or not isinstance(frames, list):
            return self._json({'error': 'no-frames'}, 400)
        meta = payload.get('meta', {})
        app_type = (meta.get('app_type') or 'other').lower()
        prompt = {'game': PROMPT_PLAY, 'video': PROMPT_VIDEO}.get(app_type, PROMPT_COMPANION)
        # P1 节流：同窗口 6 分钟内已说 ≥3 条 → 追加节流提示，推着模型选 [SKIP]
        with LOCK:
            governed = same_window_count(meta.get('window', '')) >= GOV_MAX_NOTES
        meta['prompt_tail'] = prompt + (GOVERNOR if governed else '')
        meta['governed'] = governed
        meta['frame_count'] = len(frames)

        # 闸5: 视觉调用也在账本里记账（token 视为隐性成本）
        try:
            note, lane, tokens = call_vision(frames, meta)
        except Exception as e:
            ledger('round_fail', err=str(e)[:200], window=meta.get('window', '')[:60])
            return self._json({'error': 'vision-failed', 'detail': str(e)[:200]}, 502)

        with LOCK:
            STATE['day_tokens'] += tokens
        note, note_skipped, had_marker = clean_note(note)
        # 三形态与 /clip 一致：尾挂/空 = 沉默；中置残留剥离后照写
        if note and not note_skipped:
            # P4 去重：同窗口最近几条相似度 >0.72 → 视为复读，不写不推
            with LOCK:
                dup = recent_similar(meta.get('window', ''), note)
            if dup is not None:
                with LOCK:
                    ledger('dup_note', lane=lane, tokens=tokens,
                           window=meta.get('window', '')[:60], similar=dup[:60])
                return self._json({'ok': True, 'note': '', 'skipped': True, 'dup': True,
                                   'lane': lane, 'tokens': tokens})
            with LOCK:
                STATE['note_hist'].append((time.time(), meta.get('window', ''), note))
                STATE['note_hist'] = STATE['note_hist'][-30:]
            write_note(note, meta, lane, tokens)
            ledger('note', tokens=tokens, lane=lane, governed=governed)
            return self._json({'ok': True, 'note': note, 'lane': lane, 'tokens': tokens})
        with LOCK:
            ledger('skip', lane=lane, tokens=tokens, governed=governed)
        return self._json({'ok': True, 'note': '', 'skipped': True, 'lane': lane, 'tokens': tokens})


    def _frames_scene(self, scene, payload, length):
        """mc / stardew：两值契约 + 独立分区（无 governor/dup/SKIP）。"""
        nowts = time.time()
        st = PART[scene]
        meta = payload.get('meta') or {}
        mime = str(meta.get('mime') or 'jpeg').lower()
        if mime not in ('jpeg', 'png'):
            return self._json({'ok': False, 'scene': scene, 'reason': 'bad-frames',
                               'detail': 'mime 只支持 jpeg/png'}, 400)
        frames = payload.get('frames')
        if not isinstance(frames, list) or not frames:
            return self._json({'ok': False, 'scene': scene, 'reason': 'no-frame'}, 400)
        if len(frames) > SCENE_MAX_FRAMES:
            return self._json({'ok': False, 'scene': scene, 'reason': 'too-many-frames',
                               'max': SCENE_MAX_FRAMES}, 400)
        for fi, fb in enumerate(frames):
            if not isinstance(fb, str) or len(fb) < 64:
                return self._json({'ok': False, 'scene': scene, 'reason': 'bad-frames',
                                   'index': fi}, 400)
            nbytes = len(fb) * 3 // 4
            if nbytes > SCENE_FRAME_MAX_BYTES:
                return self._json({'ok': False, 'scene': scene, 'reason': 'frame-too-large',
                                   'index': fi, 'bytes': nbytes,
                                   'max': SCENE_FRAME_MAX_BYTES}, 400)
        prompt = scene_prompt(scene)
        if not prompt:
            return self._json({'ok': False, 'scene': scene, 'reason': 'prompt-missing',
                               'hint': '场景主持需在 %s 放判据文本' % (PROMPTS_DIR / (scene + '.txt'))}, 503)
        with LOCK:
            roll_day(scene)
            if st['paused']:
                return self._json({'ok': False, 'scene': scene, 'reason': 'paused',
                                   'detail': st['pause_reason']}, 503)
            gap = nowts - float(st.get('last_ts') or 0.0)
            need = SCENE_MIN_INTERVAL.get(scene, 2.0)
            if gap < need:
                return self._json({'ok': False, 'scene': scene, 'reason': 'rate-limited',
                                   'retry_after_s': round(need - gap, 2)}, 429)
            cap = SCENE_DAY_CALLS.get(scene)
            if cap and st['day_calls'] >= cap:
                st['paused'] = True
                st['pause_reason'] = 'daily-limit'
                ledger('circuit_break', scene=scene, reason='daily-limit',
                       day_calls=st['day_calls'])
                return self._json({'ok': False, 'scene': scene, 'reason': 'daily-limit',
                                   'cap': cap}, 503)
            st['recent'] = [x for x in st['recent'] if nowts - x[0] <= ANOMALY_WINDOW_SEC]
            st['recent'].append((nowts, length))
            win_bytes = sum(x[1] for x in st['recent'])
            if win_bytes > ANOMALY_BYTES:
                st['paused'] = True
                st['pause_reason'] = 'anomaly'
                ledger('circuit_break', scene=scene, reason='anomaly',
                       window_bytes=win_bytes)
                return self._json({'ok': False, 'scene': scene, 'reason': 'paused',
                                   'detail': 'anomaly'}, 503)
            st['day_bytes'] += length
            st['day_calls'] += 1
            st['last_ts'] = nowts
            save_day_state(scene)
        meta['prompt_tail'] = prompt
        meta['mime'] = mime
        meta['frame_count'] = len(frames)
        meta['window'] = str(meta.get('window') or scene)[:80]
        t0 = time.time()
        try:
            note, lane, tokens = call_vision(frames, meta)
        except Exception as e:
            ledger('round_fail', scene=scene, err=str(e)[:200])
            return self._json({'ok': False, 'scene': scene, 'reason': 'vision-failed',
                               'detail': str(e)[:200]}, 502)
        ms = int((time.time() - t0) * 1000)
        with LOCK:
            st['day_tokens'] += tokens
        text, skipped, had = clean_note(note)
        row = {'ts': time.time(), 'scene': scene, 'desc': text, 'skip_marker': had,
               'window': meta['window'], 'note': str(meta.get('note') or '')[:200],
               'lane': lane, 'tokens': tokens, 'frames': len(frames),
               'mime': mime, 'ms': ms}
        try:
            with open(scene_notes_file(scene), 'a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
        except OSError:
            pass
        if not text:
            ledger('scene_unreadable', scene=scene, lane=lane, tokens=tokens, ms=ms,
                   skip_marker=had)
            return self._json({'ok': False, 'scene': scene, 'reason': 'unreadable',
                               'lane': lane, 'tokens': tokens, 'ms': ms}, 200)
        ledger('scene_desc', scene=scene, lane=lane, tokens=tokens, ms=ms,
               skip_marker=had, window=meta['window'][:60])
        return self._json({'ok': True, 'scene': scene, 'desc': text, 'lane': lane,
                           'tokens': tokens, 'ms': ms, 'frames': len(frames),
                           'skip_marker': had}, 200)


def main():
    ledger('server_start', port=PORT)
    seed_note_hist()
    threading.Thread(target=pool_probe_loop, daemon=True, name='pool-probe').start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    print(f'eye-server listening on 0.0.0.0:{PORT} | lane={lane_pick()} | notes={NOTES_FILE}'
          ' | scenes=' + ','.join('%s:%s' % (s, 'on' if scene_token(s) else 'off') for s in SCENES),
          flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
