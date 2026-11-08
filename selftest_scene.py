# -*- coding: utf-8 -*-
"""selftest_scene.py —— eye-server 场景分区（pet / mc / stardew）的端到端自测。

测的是"分区"本身：pet 零改动、mc/stardew 两值契约（无 SKIP）、独立会计互不误杀、
超限返回失败而不是排队。视觉模型用假实现替换（不花钱、不需要 key），HTTP 走真 socket。

用法：
    python selftest_scene.py                 # 测本目录的 eye_server.py
    python selftest_scene.py --control DIR   # 阳性对照：用 DIR 里的旧 eye_server.py，必须红
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

RESULTS = []
FAKE = {'text': '前方两格有树，右边是水'}


def check(name, ok, extra=''):
    RESULTS.append((name, ok))
    print('%-46s %s %s' % (name, 'PASS' if ok else 'FAIL', '' if ok else extra))


def http(port, method, path, body=None, headers=None, timeout=15):
    s = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    h = {'Host': 'x', 'Content-Type': 'application/json'}
    h.update(headers or {})
    raw = b'' if body is None else json.dumps(body).encode()
    head = '%s %s HTTP/1.1\r\n' % (method, path) + ''.join('%s: %s\r\n' % kv for kv in h.items())
    head += 'Content-Length: %d\r\n\r\n' % len(raw)
    s.sendall(head.encode() + raw)
    buf = b''
    while b'\r\n\r\n' not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    head_b, _, rest = buf.partition(b'\r\n\r\n')
    n = 0
    for line in head_b.split(b'\r\n'):
        if line.lower().startswith(b'content-length:'):
            n = int(line.split(b':')[1])
    while len(rest) < n:
        c = s.recv(65536)
        if not c:
            break
        rest += c
    s.close()
    code = int(head_b.split(b' ')[1])
    try:
        return code, json.loads(rest.decode() or '{}')
    except ValueError:
        return code, None


def rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in open(path, encoding='utf-8') if x.strip()]


def main():
    control = None
    if '--control' in sys.argv:
        i = sys.argv.index('--control')
        control = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    here = os.path.dirname(os.path.abspath(__file__))
    data = tempfile.mkdtemp(prefix='eye-scene-')
    toks = tempfile.mkdtemp(prefix='eye-tok-')
    mod_dir = here
    if control:
        mod_dir = tempfile.mkdtemp(prefix='eye-ctrl-')
        shutil.copyfile(os.path.join(control, 'eye_server.py'),
                        os.path.join(mod_dir, 'eye_server.py'))

    os.environ['EYE_DATA_DIR'] = data
    os.environ['EYE_TOKEN_DIR'] = toks
    open(os.path.join(toks, 'eye-server.token'), 'w').write('PETTOK\n')
    os.makedirs(os.path.join(data, 'prompts'), exist_ok=True)
    open(os.path.join(data, 'prompts', 'mc.txt'), 'w', encoding='utf-8').write('描述眼前的方块世界\n')

    if control:
        # 旧实现在导入期强依赖 ~/.dsh/dsh-web.env；给对照一个假 HOME，别让它红在"缺文件"
        import pathlib
        fake_home = tempfile.mkdtemp(prefix='eye-home-')
        os.makedirs(os.path.join(fake_home, '.dsh'), exist_ok=True)
        open(os.path.join(fake_home, '.dsh', 'dsh-web.env'), 'w').write('STUB=1' + chr(10))
        pathlib.Path.home = classmethod(lambda cls: pathlib.Path(fake_home))
    sys.path.insert(0, mod_dir)
    import eye_server as S                                    # noqa: E402

    S.call_vision = lambda frames, meta: (FAKE['text'], 'pool', 42)
    if control:
        # 旧实现没有 EYE_TOKEN_DIR：把 token 直接塞进去，让对照红在"没有分区"而不是"读不到 token"
        S.TOKEN = 'PETTOK'
    srv = ThreadingHTTPServer(('127.0.0.1', 0), S.H)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print('data=%s tokens=%s module=%s port=%d\n' % (data, toks, mod_dir, port))

    PET = {'Authorization': 'Bearer PETTOK'}
    MC = {'Authorization': 'Bearer MCTOK', 'X-Scene': 'mc'}
    FR = ['A' * 200, 'B' * 200]

    def part(scene):
        return (getattr(S, 'PART', {}) or {}).get(scene, {})

    def pset(scene, k, v):
        d = (getattr(S, 'PART', {}) or {}).get(scene)
        if isinstance(d, dict):
            d[k] = v

    def mpost(meta, frames=None, headers=None):
        """mc 顺序检查：先清限流计时，保证每条断言只测自己那件事。"""
        pset('mc', 'last_ts', 0.0)
        meta = dict(meta)
        meta.setdefault('app_type', 'mc')
        pay = {'frames': FR if frames is None else frames, 'meta': meta}
        return http(port, 'POST', '/frames', pay, headers or MC)

    # ── 1. pet 零改动：老形状、老 notes
    code, d = http(port, 'POST', '/frames',
                   {'frames': FR, 'meta': {'app_type': 'game', 'window': 'w-pet'}}, PET)
    check('pet 老路径仍返回 note', code == 200 and d and d.get('ok') and d.get('note'),
          repr((code, d)))
    check('pet notes 写进老文件', len(rows(os.path.join(data, 'notes.jsonl'))) == 1)

    # ── 2. mc 没发 token = 场景关闭（fail closed）
    code, d = mpost({'window': 'mc:w0'})
    check('mc 未开 → 401 scene-disabled', code == 401 and d and d.get('error') == 'scene-disabled',
          repr((code, d)))

    # ── 3. 拿 pet 的 token 进 mc = 拒绝（不许串场景）
    open(os.path.join(toks, 'eye-server.mc.token'), 'w').write('MCTOK\n')
    code, d = mpost({'window': 'mc:w0b'}, headers={'Authorization': 'Bearer PETTOK', 'X-Scene': 'mc'})
    check('mc 拿错 token → 401 unauthorized', code == 401 and d and d.get('error') == 'unauthorized',
          repr((code, d)))

    # ── 4. mc 正常：两值契约的 ok 支
    code, d = mpost({'window': 'mc:w1', 'mime': 'png', 'note': '盖房前看一眼'})
    check('mc 返回 ok+desc', code == 200 and d and d.get('ok') and d.get('desc') == FAKE['text'],
          repr((code, d)))
    check('mc 不污染 pet notes', len(rows(os.path.join(data, 'notes.jsonl'))) == 1)
    mcnotes = rows(os.path.join(data, 'notes-mc.jsonl'))
    check('mc 自己一份 notes（带 scene/note）',
          len(mcnotes) == 1 and mcnotes[0].get('scene') == 'mc'
          and mcnotes[0].get('note') == '盖房前看一眼', repr(mcnotes))

    # ── 5. 无 SKIP：模型沉默 = unreadable（不是"成功但没内容"）
    FAKE['text'] = '[SKIP]'
    code, d = mpost({'window': 'mc:w2'})
    check('mc SKIP → ok:false unreadable', code == 200 and d and d.get('ok') is False
          and d.get('reason') == 'unreadable', repr((code, d)))

    FAKE['text'] = '[SKIP] 头顶有屋檐'
    code, d = mpost({'window': 'mc:w3'})
    check('mc 中置 SKIP 剥离后仍有正文', code == 200 and d and d.get('ok')
          and d.get('desc') == '头顶有屋檐' and d.get('skip_marker') is True, repr((code, d)))

    # ── 6. 形状校验（契约 §1）
    code, d = mpost({'window': 'mc:w4'}, frames=[])
    check('mc 空 frames → 400 no-frame', code == 400 and d and d.get('reason') == 'no-frame',
          repr((code, d)))
    code, d = mpost({'window': 'mc:w5'}, frames=FR * 2)
    check('mc 超 3 帧 → 400 too-many-frames',
          code == 400 and d and d.get('reason') == 'too-many-frames', repr((code, d)))
    big = 'A' * (810 * 1024 * 4 // 3)          # 解码后 ~810KB > 800KB
    code, d = mpost({'window': 'mc:w6'}, frames=[big])
    check('mc 单帧 >800KB → 400 frame-too-large',
          code == 400 and d and d.get('reason') == 'frame-too-large', repr((code, d)))
    code, d = mpost({'window': 'mc:w7'}, headers={'Authorization': 'Bearer MCTOK',
                                                 'X-Scene': 'mc', 'Content-Type': 'application/json'})
    code2, d2 = http(port, 'POST', '/frames', {'frames': FR, 'meta': {'app_type': 'stardew'}}, MC)
    check('header/app_type 不一致 → 400 scene-mismatch',
          code2 == 400 and d2 and d2.get('error') == 'scene-mismatch', repr((code2, d2)))

    code3, d3 = http(port, 'POST', '/frames', {'frames': FR, 'meta': {'app_type': 'pet'}}, MC)
    check('X-Scene: mc + app_type: pet → 400 scene-mismatch',
          code3 == 400 and d3 and d3.get('error') == 'scene-mismatch', repr((code3, d3)))

    # ── 7. prompt 归场景主持：文件不在 = 明确失败，不是用别人的判据
    os.rename(os.path.join(data, 'prompts', 'mc.txt'), os.path.join(data, 'prompts', 'mc.txt.off'))
    code, d = mpost({'window': 'mc:w8'})
    check('mc 无场景 prompt → 503 prompt-missing',
          code == 503 and d and d.get('reason') == 'prompt-missing', repr((code, d)))
    os.rename(os.path.join(data, 'prompts', 'mc.txt.off'), os.path.join(data, 'prompts', 'mc.txt'))

    # ── 8. 未知场景（拼错就拒，不落 pet）
    code, d = http(port, 'POST', '/frames', {'frames': FR, 'meta': {}},
                   {'Authorization': 'Bearer PETTOK', 'X-Scene': 'minecrft'})
    check('未知场景 → 400 unknown-scene', code == 400 and d and d.get('error') == 'unknown-scene',
          repr((code, d)))

    # ── 9. 端点：/clip 只服务 pet
    code, d = http(port, 'POST', '/clip', {'video_b64': 'A' * 4096, 'meta': {}}, MC)
    check('/clip 拒绝 mc → 400', code == 400 and d and d.get('error') == 'scene-not-supported',
          repr((code, d)))

    # ── 10. 限流：同场景 2s 内第二次 429（失败而非排队），pet 不受影响
    pset('mc', 'last_ts', __import__('time').time())      # 确定性：刚发过一次
    code, d = http(port, 'POST', '/frames',
                   {'frames': FR, 'meta': {'app_type': 'mc', 'window': 'mc:w9'}}, MC)
    check('mc 2s 内连发 → 429 rate-limited', code == 429 and d and d.get('reason') == 'rate-limited'
          and d.get('retry_after_s', 0) > 0, repr((code, d)))
    code, d = http(port, 'POST', '/frames',
                   {'frames': FR, 'meta': {'app_type': 'game', 'window': 'w-pet2'}}, PET)
    check('mc 被限流时 pet 照常', code == 200 and d and d.get('ok'), repr((code, d)))

    # ── 11. 分区隔离：mc 日额度耗尽 ≠ pet 暂停
    pset('mc', 'day_calls', getattr(S, 'SCENE_DAY_CALLS', {}).get('mc', 200))
    code, d = mpost({'window': 'mc:w10'})
    check('mc 日额度耗尽 → 503 daily-limit', code == 503 and d and d.get('reason') == 'daily-limit',
          repr((code, d)))
    check('mc 暂停不影响 pet.paused', part('pet').get('paused') is False, repr(part('pet')))
    code, d = http(port, 'POST', '/frames',
                   {'frames': FR, 'meta': {'app_type': 'game', 'window': 'w-pet3'}}, PET)
    check('pet 在 mc 暂停时仍 200', code == 200 and d and d.get('ok'), repr((code, d)))

    # ── 12. 反向隔离：pet 暂停 ≠ mc 被拦
    if hasattr(S, 'STATE'):
        S.STATE['paused'] = True
        S.STATE['pause_reason'] = 'manual'
    code, d = http(port, 'POST', '/frames',
                   {'frames': FR, 'meta': {'app_type': 'game', 'window': 'w-pet4'}}, PET)
    check('pet 暂停 → 503 paused', code == 503 and d and d.get('error') == 'paused', repr((code, d)))
    pset('mc', 'day_calls', 0); pset('mc', 'paused', False); pset('mc', 'pause_reason', '')
    code, d = mpost({'window': 'mc:w11'})
    check('pet 暂停时 mc 仍 200', code == 200 and d and d.get('ok'), repr((code, d)))
    if hasattr(S, 'STATE'):
        S.STATE['paused'] = False
        S.STATE['pause_reason'] = ''

    # ── 13. /health：老字段在 + scenes 快照
    code, d = http(port, 'GET', '/health', None, PET)
    keys = set(d or {})
    check('/health 老字段仍在', {'paused', 'day_bytes', 'day_calls', 'clip_limit',
                                 'hour_clips', 'pool_probe'} <= keys, repr(sorted(keys)))
    check('/health 有 scenes 且标了启用状态',
          isinstance(d.get('scenes'), dict) and d['scenes']['pet']['enabled'] is True
          and d['scenes']['mc']['enabled'] is True and d['scenes']['stardew']['enabled'] is False,
          repr(d.get('scenes')))
    code, d2 = http(port, 'GET', '/health', None, MC)
    check('mc token 也能读 /health（自查配额）', code == 200 and d2 and d2.get('ok'), repr(code))

    # ── 14. 按场景 pause/resume
    code, d = http(port, 'POST', '/pause?scene=mc', None, MC)
    check('/pause?scene=mc 只停 mc', code == 200 and part('mc').get('paused') is True
          and part('pet').get('paused') is False, repr((code, d, part('mc').get('paused'))))
    code, d = http(port, 'POST', '/resume?scene=mc', None, MC)
    check('/resume?scene=mc 只解 mc', code == 200 and part('mc').get('paused') is False
          and part('stardew').get('paused') is False, repr((code, d)))

    # ── 15. 跨日清零只清本场景
    pset('mc', 'day', '2000-01-01')
    pset('mc', 'day_calls', 5)
    pet_calls = (getattr(S, 'STATE', {}) or {}).get('day_calls')
    try:
        S.roll_day('mc')
    except Exception:
        pass
    check('roll_day(mc) 只清 mc', part('mc').get('day_calls') == 0
          and (getattr(S, 'STATE', {}) or {}).get('day_calls') == pet_calls,
          repr((part('mc').get('day_calls'), (getattr(S, 'STATE', {}) or {}).get('day_calls'))))

    # ── 16. ledger 归因：mc 行带 scene，pet 行不带（老消费方不受影响）
    led = rows(os.path.join(data, 'ledger.jsonl'))
    mc_rows = [r for r in led if r.get('scene') == 'mc']
    pet_rows = [r for r in led if r.get('kind') in ('note', 'skip', 'vision')]
    check('ledger: mc 行带 scene', len(mc_rows) >= 1, 'mc_rows=%d' % len(mc_rows))
    check('ledger: pet 行没有 scene',
          bool(pet_rows) and all('scene' not in r for r in pet_rows), repr(pet_rows[:1]))

    bad = [n for n, ok in RESULTS if not ok]
    print('\n%d/%d 通过' % (len(RESULTS) - len(bad), len(RESULTS)))
    shutil.rmtree(data, ignore_errors=True)
    shutil.rmtree(toks, ignore_errors=True)
    if control:
        if not bad:
            print('❌ 阳性对照失败：旧实现也全绿，说明断言测不出分区')
            return 1
        print('✅ 阳性对照成立：旧实现在 %d 条上红' % len(bad))
        return 0
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
