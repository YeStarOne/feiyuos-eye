#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_writeback.py —— 把她当天看到的主题沉淀成「记忆语料」（第一切片）

来源：眼睛的 `notes.jsonl`（她每天真实的观察笔记）。
产出：一天一份 `memory-inbox/YYYY-MM-DD.jsonl`，每行**一条候选记忆**。第二天她能被问起
"你昨天那个 build 失败解决了没"——靠的就是这里的 `thread` 类条目，而不是重新读一遍笔记。

这一版刻意做窄、做确定性：
- **不调模型**（不花 token、不需要 key、可重复跑，同样输入永远同样输出）
- **不写她的记忆图谱**（只生成语料文件；入库是下一片，要总架构点头）
- 不猜情感、不猜意图，只做"归组 + 关键词标记 + 去重"，每条都带原文证据

用法：
    python tools/memory_writeback.py ~/pc-body/notes.jsonl --date 2026-11-08
    python tools/memory_writeback.py notes.jsonl --date 2026-11-08 --out memory-inbox
    python tools/memory_writeback.py notes.jsonl --hours 24 --dry-run
    python tools/memory_writeback.py --selftest          # 合成数据，已知答案
    python tools/memory_writeback.py --mutation-check    # 阳性对照：3 个变异体各自必须打红
"""
import argparse
import collections
import datetime
import difflib
import json
import os
import re
import sys
import tempfile

MIN_NOTES_PER_THEME = 3      # 少于这个数的窗口组不成"主题"（但要进 day 汇总）
MAX_ENTRIES = 40             # 一天的语料上限（防语料比记忆本身还长）
MAX_EVIDENCE = 3             # 每主题最多留 3 条原文
EVIDENCE_CHARS = 80          # 单条原文截断（保留她说话的原样口吻）
DUP_RATIO = 0.80             # 近重复判定（同一主题内部）

# "未了结的线索"关键词：她第二天该能主动问起的那些
THREAD_RE = re.compile(
    r'(失败|报错|崩了|崩了|卡住|卡在|还没|没解决|没弄好|要等|等一下|待办|明天|明天再|'
    r'待会|回头|先不|忘了|缺少|差一|欠|error|failed|timeout|重试)', re.IGNORECASE)

MUT = None          # 只在 --mutation-check 里被设置，生产路径永远是 None


def local(ts):
    return datetime.datetime.fromtimestamp(ts)


def family_of(row):
    """归组键：优先窗口名（去掉「录屏 · 」这类前缀），拿不到退到进程名。

    2026-11-08：与 eye_server 的 win_key 同口径——空窗口不许把所有观察并成一个桶。
    """
    w = str(row.get('window') or '').strip()
    if ' · ' in w:
        w = w.split(' · ', 1)[1].strip()
    if w:
        return re.sub(r'\s+', ' ', w)[:40]
    p = str(row.get('proc') or '').strip()
    return ('proc:' + p) if p else ''


def dedup_texts(texts, ratio=DUP_RATIO):
    """按顺序去重：首 12 字相同或相似度 > ratio 的，只留第一条。"""
    out = []
    for t in texts:
        t = (t or '').strip()
        if not t:
            continue
        if MUT == 'no_dedup':
            out.append(t)
            continue
        dup = False
        for k in out:
            if t[:12] == k[:12]:
                dup = True
                break
            if difflib.SequenceMatcher(None, t, k).ratio() > ratio:
                dup = True
                break
        if not dup:
            out.append(t)
    return out


def pick_day(rows, date_str=None, hours=0):
    if hours and rows:
        end = rows[-1]['ts']
        rows = [r for r in rows if r['ts'] >= end - hours * 3600]
    if date_str:
        rows = [r for r in rows if local(r['ts']).strftime('%Y-%m-%d') == date_str]
    return sorted(rows, key=lambda r: r['ts'])


def build(rows):
    """→ [entry]，entry 是可直接落 JSONL 的 dict（无网络、无模型）。"""
    if not rows:
        return []
    date_str = local(rows[-1]['ts']).strftime('%Y-%m-%d')
    entries = []

    # ① 主题卡：按 family 归组，≥ MIN_NOTES_PER_THEME 条才算"看过这件事"
    groups = collections.OrderedDict()
    for r in rows:
        groups.setdefault(family_of(r) or '(未知窗口)', []).append(r)
    themes = []
    for fam, rs in groups.items():
        if len(rs) < (0 if MUT == 'theme_min_one' else MIN_NOTES_PER_THEME):
            continue
        texts = dedup_texts([r.get('note') for r in rs])[:MAX_EVIDENCE]
        themes.append((len(rs), fam, rs, texts))
    themes.sort(key=lambda x: -x[0])
    for n, fam, rs, texts in themes:
        entries.append({
            'date': date_str, 'kind': 'theme', 'key': fam,
            'title': '%s（%d 条观察）' % (fam, n),
            'note_count': n,
            'first_ts': rs[0]['ts'], 'last_ts': rs[-1]['ts'],
            'procs': sorted({r.get('proc') for r in rs if r.get('proc')}),
            'evidence': [t[:EVIDENCE_CHARS] for t in texts],
            'source': 'notes.jsonl#theme',
        })

    # ② 未了结的线索：关键词命中 + 原文，供第二天主动问起
    threads = collections.OrderedDict()
    for r in rows:
        note = str(r.get('note') or '').strip()
        if not note:
            continue
        if MUT == 'no_threads' or not THREAD_RE.search(note):
            continue
        key = family_of(r) or '(未知窗口)'
        threads.setdefault(key, []).append((r['ts'], note))
    for key, items in threads.items():
        texts = dedup_texts([t for _, t in items])[:MAX_EVIDENCE]
        if not texts:
            continue
        entries.append({
            'date': date_str, 'kind': 'thread', 'key': key,
            'title': '未了结：%s' % key,
            'note_count': len(items),
            'first_ts': items[0][0], 'last_ts': items[-1][0],
            'evidence': [t[:EVIDENCE_CHARS] for t in texts],
            'source': 'notes.jsonl#thread',
        })

    # ③ 当天总览（给了语境，第二天的她才不会只记得零散句子）
    procs = collections.Counter(r.get('proc') for r in rows if r.get('proc'))
    entries.append({
        'date': date_str, 'kind': 'day', 'key': date_str,
        'title': '这一天看了 %d 次，主要在 %s' % (
            len(rows), '、'.join('%s(%d)' % (p, c) for p, c in procs.most_common(3)) or '未知'),
        'note_count': len(rows),
        'first_ts': rows[0]['ts'], 'last_ts': rows[-1]['ts'],
        'evidence': [], 'source': 'notes.jsonl#day',
    })

    if MUT == 'no_cap':
        return entries
    return entries[:MAX_ENTRIES]


def write(entries, out_dir, dry_run=False):
    if not entries:
        return None
    date_str = entries[0]['date']
    if dry_run:
        return date_str
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, '%s.jsonl' % date_str)
    with open(path, 'w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')
    return path


def load_rows(path):
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------- 自测与阳性对照
FIX = [
    # 同一个窗口 4 条（≥3 → 成主题），其中两条近重复
    {'ts': 1000, 'note': '这条裙子画得真好看，腰上还挂着枪', 'window': '插画投稿 - pixiv - QQ浏览器', 'proc': 'qqbrowser.exe'},
    {'ts': 1060, 'note': '这条裙子画得真好看，腰上还挂着枪', 'window': '插画投稿 - pixiv - QQ浏览器', 'proc': 'qqbrowser.exe'},
    {'ts': 1120, 'note': '红发狐耳小姐姐这一套也挺戳我', 'window': '插画投稿 - pixiv - QQ浏览器', 'proc': 'qqbrowser.exe'},
    {'ts': 1180, 'note': '叶子林配色挺舒服的', 'window': '插画投稿 - pixiv - QQ浏览器', 'proc': 'qqbrowser.exe'},
    # 另一个窗口只 2 条 → 不成主题，但要进 day 汇总
    {'ts': 1240, 'note': '代理面板流量跑得挺勤', 'window': 'FlClash', 'proc': 'flclash.exe'},
    {'ts': 1300, 'note': '上传比下载还多，摸鱼吗', 'window': 'FlClash', 'proc': 'flclash.exe'},
    # 空窗口 + 进程名 → 按进程归组（与 win_key 同口径）
    {'ts': 1360, 'note': '这个 build 又失败了，回头再查', 'window': '', 'proc': 'code.exe'},
    {'ts': 1390, 'note': '终端里那条超时还在重试', 'window': '', 'proc': 'code.exe'},
    {'ts': 1405, 'note': '改完这版再跑一次看看', 'window': '', 'proc': 'code.exe'},
    # 线索：明确未了结
    {'ts': 1420, 'note': '存档卡住了，明天再弄', 'window': 'Stardew Valley 1.6.15', 'proc': 'stardew.exe'},
]


def selftest():
    rows = [dict(r) for r in FIX]
    entries = build(rows)

    themes = [e for e in entries if e['kind'] == 'theme']
    threads = [e for e in entries if e['kind'] == 'thread']
    day = [e for e in entries if e['kind'] == 'day']
    assert len(day) == 1, 'day 汇总必须恰好 1 条'

    keys = {e['key'] for e in themes}
    assert '插画投稿 - pixiv - QQ浏览器' in keys, keys
    assert 'FlClash' not in keys, '只有 2 条观察的窗口不该成主题'
    assert 'proc:code.exe' in keys, '空窗口必须按进程归组（否则所有观察并成一个桶）'

    pixiv = [e for e in themes if e['key'] == '插画投稿 - pixiv - QQ浏览器'][0]
    assert pixiv['note_count'] == 4
    assert len(pixiv['evidence']) == 3, '近重复要去重：4 条里两条重复 → 3 条'
    assert '腰上还挂着枪' in pixiv['evidence'][0]
    # 光看条数会被 MAX_EVIDENCE 截断骗过：必须断言"同一条只出现一次"
    assert sum('腰上还挂着枪' in t for t in pixiv['evidence']) == 1, '近重复没被去掉'

    # 上限：语料不能比记忆本身还长（主题多到爆时必须截断）
    bulk = [{'ts': 2000 + i, 'note': '第 %d 次观察' % i, 'window': 'W%d' % (i // 3), 'proc': 'p.exe'}
            for i in range(150)]
    assert len(build(bulk)) <= MAX_ENTRIES, '素材远超上限时必须截断到 MAX_ENTRIES'

    tkeys = {e['key'] for e in threads}
    assert 'Stardew Valley 1.6.15' in tkeys, tkeys
    assert 'proc:code.exe' in tkeys, '「失败」类线索也要抓到'
    assert not any('pixiv' in k for k in tkeys), '纯欣赏的句子不该被当成待办'

    # 空输入 → 不产出（别生成空语料文件冒充"有记忆"）
    assert build([]) == []

    # 落盘：一行一 JSON，可被下游直接消费
    d = tempfile.mkdtemp(prefix='wb-')
    p = write(entries, d)
    got = [json.loads(x) for x in open(p, encoding='utf-8') if x.strip()]
    assert len(got) == len(entries) and got[0]['kind'] in ('theme', 'thread', 'day')
    assert write([], d) is None, '空列表不该写文件'

    print('[OK] selftest 通过：主题阈值/空窗口按进程归组/近重复去重/线索关键词/空输入不落盘')
    return 0


def mutation_check():
    """三处变异：每处必须至少打红一条断言。全绿的 selftest 不配叫闸门。"""
    global MUT
    base = None
    try:
        base = selftest()
    except AssertionError as e:
        print('[FAIL] 基线自测就红了：%s' % e)
        return 1
    for mut in ('theme_min_one', 'no_dedup', 'no_threads', 'no_cap'):
        MUT = mut
        try:
            selftest()
            print('[FAIL] 变异体 %s 没有打红 —— 断言测不出它' % mut)
            return 1
        except AssertionError as e:
            print('[OK] 变异体 %s 被打红：%s' % (mut, e))
        finally:
            MUT = None
    print('[OK] 阳性对照成立：4 个变异体全部打红')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('notes', nargs='?', help='notes.jsonl')
    ap.add_argument('--date', default='', help='只看这一天（本地时区 YYYY-MM-DD）')
    ap.add_argument('--hours', type=float, default=0, help='只看最近 N 小时')
    ap.add_argument('--out', default='memory-inbox', help='语料输出目录（默认 memory-inbox）')
    ap.add_argument('--dry-run', action='store_true', help='只算不写')
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--mutation-check', action='store_true')
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.mutation_check:
        return mutation_check()
    if not a.notes:
        ap.error('需要 notes.jsonl（或用 --selftest / --mutation-check）')

    rows = pick_day(load_rows(a.notes), a.date or None, a.hours)
    entries = build(rows)
    if not entries:
        print('这一天没有笔记，不产出语料（空语料比没有更糟）')
        return 0

    kinds = collections.Counter(e['kind'] for e in entries)
    print('分母：%d 条笔记 → %d 条候选记忆（%s）' % (len(rows), len(entries), dict(kinds)))
    for e in entries:
        if e['kind'] == 'day':
            continue
        print('  [%s] %s  证据 %d 条' % (e['kind'], e['title'], len(e['evidence'])))
        for t in e['evidence'][:2]:
            print('        · %s' % t)

    path = write(entries, a.out, dry_run=a.dry_run)
    print('输出：%s' % ('(dry-run，未写盘)' if a.dry_run else path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
