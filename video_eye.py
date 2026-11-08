#!/usr/bin/env python3
# 视频眼 · 原生视频理解（GLM-5.3-Flash video_url）—— 2026-11-04 实装，2026-11-06 复验后入库
#
# 定位：她「按需真看视频」的通道（整段视频喂模型，不抽帧）。
#   - 车道：固定直连 opencode-go（GO）。池网关(zcode2api) 不支持 video_url（HTTP 400 协议转换）。
#     调用瞬间烧 GO 额度，不占主模型上下文/缓存（主模型不用切）。
#   - 成本实测：480p/2fps 30s=16,938 token/13s；120s=29,538 token/33.9s；单帧 800x509~695 token。
#     结论：常驻哨兵不传视频（约 10 倍 token），只有「按需真看」才走这里。
#   - 体积：压 360p CRF28 后按 <=9MiB/段切分（base64 膨胀 33%，须低于单请求 16MiB 上限）。
#   - 听：B站官方字幕 + 硅基流动 SenseVoice 免费转写（可选；抓不到只靠画面，不阻塞）。
#   - 缓存：/tmp/video-eye/<BV>/（v360.mp4 / notes.txt / meta.json），追问 --ask 不重下载。
#
# 用法（服务器）:
#   python3 ~/.dsh/video-eye/eye.py <BV号或链接> [问题]      首看：出带时间戳笔记（可带问题直接答）
#   python3 ~/.dsh/video-eye/eye.py --ask <BV号或链接> 问题   追问：走缓存，只花一次问答 token
#
# 部署：服务器 ~/.dsh/video-eye/eye.py；本仓 code/eyes/video_eye.py 为镜像（改完两边同步）。
# 密钥：OPENCODE_API_KEY（GO）、SILICONFLOW_API_KEY（转写，可选）均从 ~/.dsh/dsh-web.env 读，
#       不写任何文件、不入库（AGENTS.md §1）。
# 依赖：dsh-bili-whale 的 bili.mjs（下载/九宫格/字幕）、ffmpeg、ffprobe。
# 注意：B站以外的源要先把视频落成本地 mp4（把 whale_watch 换成本地文件输入即可复用整条链）。
import json, os, re, subprocess, sys, time, base64, urllib.request, urllib.error, uuid
from pathlib import Path

BILI = os.path.expanduser('~/.dsh/profiles/web/node_modules/dsh-bili-whale/tools/bili.mjs')
ENVF = os.path.expanduser('~/.dsh/dsh-web.env')
CACHE = Path('/tmp/video-eye')
ENDPOINT = 'https://opencode.ai/zen/go/v1/chat/completions'
SEG_TARGET = 9 * 1024 * 1024          # 压缩后单段 ≤9MiB（base64 后 ~12MiB，低于 16MiB 上限）

def go_key():
    for line in open(ENVF, encoding='utf-8'):
        if line.startswith('OPENCODE_API_KEY='):
            return line.strip().split('=', 1)[1]
    raise RuntimeError('OPENCODE_API_KEY 不在 dsh-web.env')

def bv_of(s):
    m = re.search(r'(BV[0-9A-Za-z]{10})', s)
    return m.group(1) if m else None

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout + r.stderr

def ts(sec):
    sec = int(float(sec))
    return f'{sec//60:02d}:{sec%60:02d}'

def whale_watch(bv, work):
    out = run(['node', BILI, 'watch-video', bv, '3x3'])
    mp4 = re.search(r'已下载: (\S+\.mp4)', out)
    sheet = re.search(r'SHEET: (\S+\.jpg)', out)
    subsjpg = re.search(r'SUBS: (\S+\.jpg)', out)
    title = re.search(r'标题: (.+)', out)
    dur = re.search(r'时长: (\d+)s', out)
    if not mp4:
        raise RuntimeError('whale watch-video 失败:\n' + out[-600:])
    return {'mp4': mp4.group(1),
            'sheet': sheet.group(1) if sheet else '',
            'subsjpg': subsjpg.group(1) if subsjpg else '',
            'title': title.group(1).strip() if title else bv,
            'dur': int(dur.group(1)) if dur else 0}

def whale_subs(bv):
    out = run(['node', BILI, 'subtitles', bv])
    m = re.search(r'JSON : (\S+\.json)', out)
    if not m:
        return ''
    try:
        j = json.load(open(m.group(1), encoding='utf-8'))
        body = j.get('body') or []
        lines = [f"[{ts(x.get('from',0))}-{ts(x.get('to',0))}] {x.get('content','')}" for x in body]
        return '\n'.join(lines)[:6000]
    except Exception:
        return ''

def transcribe_audio(src, work):
    """提取全片音轨 -> 硅基流动 SenseVoice 转写（免费档）。失败/超限返回空串，不阻塞看片。"""
    out = work / 'audio.txt'
    if out.exists():
        return out.read_text(encoding='utf-8')
    audio = work / 'audio.mp3'
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-i', src, '-vn', '-ac', '1',
                    '-ar', '16k', '-b:a', '48k', str(audio)], capture_output=True, text=True)
    if not audio.exists() or audio.stat().st_size == 0 or audio.stat().st_size > 28 * 1024 * 1024:
        return ''
    key = None
    for line in open(ENVF, encoding='utf-8'):
        if line.startswith('SILICONFLOW_API_KEY='):
            key = line.strip().split('=', 1)[1]
            break
    if not key:
        return ''
    CRLF = chr(13) + chr(10)
    boundary = uuid.uuid4().hex
    data = audio.read_bytes()
    head = (f'--{boundary}{CRLF}Content-Disposition: form-data; name="model"{CRLF}{CRLF}'
            f'FunAudioLLM/SenseVoiceSmall{CRLF}'
            f'--{boundary}{CRLF}Content-Disposition: form-data; name="file"; filename="a.mp3"{CRLF}'
            f'Content-Type: audio/mpeg{CRLF}{CRLF}').encode()
    body = head + data + f'{CRLF}--{boundary}--{CRLF}'.encode()
    req = urllib.request.Request('https://api.siliconflow.cn/v1/audio/transcriptions', data=body, headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            d = json.loads(resp.read().decode('utf-8', 'replace'))
        txt = d.get('text', '')
        out.write_text(txt, encoding='utf-8')
        return txt
    except Exception:
        return ''


def compress(src, work):
    dst = work / 'v360.mp4'
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    r = subprocess.run(['ffmpeg', '-nostdin', '-y', '-i', src, '-vf', 'scale=-2:360',
                        '-c:v', 'libx264', '-crf', '28', '-preset', 'fast',
                        '-c:a', 'aac', '-b:a', '48k', str(dst)], capture_output=True, text=True)
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError('ffmpeg 压缩失败:\n' + (r.stderr or '')[-500:])
    return dst

def segments(v360, work):
    size = v360.stat().st_size
    if size <= SEG_TARGET:
        return [v360]
    n = -(-size // SEG_TARGET)
    r = subprocess.run(['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(v360)], capture_output=True, text=True)
    d = float(r.stdout.strip() or 0)
    st = max(3, int(d // n))
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-i', str(v360), '-c', 'copy', '-f', 'segment',
                    '-segment_time', str(st), str(work / 'seg%02d.mp4')], check=True, capture_output=True)
    parts = sorted(work.glob('seg*.mp4'))
    bad = [p for p in parts if p.stat().st_size > 14 * 1024 * 1024]
    if bad:
        raise RuntimeError('分段后仍超 14MiB，需调小 SEG_TARGET 重跑')
    return parts

def call_go(key, prompt, video_path, max_tokens=900):
    b64 = base64.b64encode(open(video_path, 'rb').read()).decode()
    body = {'model': 'glm-5.3-flash', 'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + b64}}]}]}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
        'User-Agent': 'opencode/1.0.0', 'x-opencode-session': 'dsh-video-eye'})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode('utf-8', 'replace'))
    return d['choices'][0]['message'].get('content', ''), d.get('usage', {})

def main():
    args = sys.argv[1:]
    ask = bool(args and args[0] == '--ask')
    if ask:
        args = args[1:]
    if not args:
        raise SystemExit(__doc__)
    bv = bv_of(args[0])
    if not bv:
        raise SystemExit('没认出 BV 号: ' + args[0])
    q = args[1] if len(args) > 1 else None
    work = CACHE / bv
    work.mkdir(parents=True, exist_ok=True)
    meta_p, notes_p = work / 'meta.json', work / 'notes.txt'
    if notes_p.exists():
        meta = json.load(open(meta_p, encoding='utf-8'))
        full = notes_p.read_text(encoding='utf-8')
    else:
        if meta_p.exists():
            meta = json.load(open(meta_p, encoding='utf-8'))
        else:
            meta = whale_watch(bv, work)
            json.dump(meta, open(meta_p, 'w', encoding='utf-8'))
        v360 = compress(meta['mp4'], work)
        parts = segments(v360, work)
        subs_txt = whale_subs(bv)
        audio_txt = transcribe_audio(meta['mp4'], work)
        key = go_key()
        notes, tok = [], 0
        for i, p in enumerate(parts, 1):
            off = '' if len(parts) == 1 else f'（全片第 {i}/{len(parts)} 段，注意时间戳是段内的）'
            pr = (f'你是视频笔记员。这部B站视频《{meta["title"]}》时长约{meta["dur"]}秒{off}。'
                  '边看边输出带时间戳的中文要点时间线：场景与画面关键信息、出现的文字/字幕关键句、'
                  '整体在讲什么。只输出笔记本体，不要客套。')
            if subs_txt:
                pr += '\n官方字幕（辅助对齐，可能不完整）：\n' + subs_txt[:4000]
            if audio_txt:
                pr += '\n音频机器转写（可能有同音错字，无时间戳，用于听清台词/歌词）：\n' + audio_txt[:6000]
            c, u = call_go(key, pr, p)
            notes.append(c)
            tok += int(u.get('total_tokens', 0))
            time.sleep(1)
        full = f'《{meta["title"]}》({bv}) 视频眼笔记：\n' + '\n\n'.join(notes)
        notes_p.write_text(full, encoding='utf-8')
        print(f'[视频眼] 消耗 {tok} token（GO 通道）', file=sys.stderr)
    print(full)
    if ask and q:
        key = go_key()
        parts = segments(work / 'v360.mp4', work)
        for i, p in enumerate(parts, 1):
            seg = '' if len(parts) == 1 else f'（这是第{i}/{len(parts)}段）'
            atxt = work / 'audio.txt'
            extra = ("\n音频转写参考：\n" + atxt.read_text(encoding='utf-8')[:4000]) if atxt.exists() else ''
            c, u = call_go(key, f'{seg}结合这段视频回答问题，50-150字，直接答：{q}' + extra, p, max_tokens=400)
            print('—', c)
    print(f"\n[可看图] 九宫格: {meta.get('sheet','')} 字幕带: {meta.get('subsjpg','')}"
          f" 缓存: {work}（追问用 --ask {bv} 问题）")

if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f'GO 端点 HTTP {e.code}: {e.read().decode("utf-8","replace")[:300]}')
    except Exception as e:
        print(f'{type(e).__name__}: {e}')
