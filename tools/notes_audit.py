#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notes_audit.py —— 眼睛笔记流对账（可重复、分母先行、能自检）

契约 §6 的"24h notes 金标准对账"以前是手敲 python 片段，每次口径都会漂一点。
这个工具把它钉死：**先报分母，再报比例**；每个比例旁边都写着它的 n。

用法：
    python notes_audit.py ~/pc-body/notes.jsonl
    python notes_audit.py notes.jsonl --split 12:41,13:57     # 按本地时刻切成多段
    python notes_audit.py notes.jsonl --date 2026-11-08       # 只看某一天（本地时区）
    python notes_audit.py --selftest                          # 用合成数据验自己算得对

退出码：0 = 全部不变量成立；1 = 有硬不变量破了（见 INVARIANTS）。
⚠️ 它对比例只做**报告**，不做门槛判定——门槛是契约 §6 的事，这里给的是分母和原始计数。
"""
import argparse
import collections
import datetime
import json
import os
import statistics
import sys
import tempfile

# 硬不变量：破一条就 exit 1。只放"一定不代表好状态"的规则，别把口径解释塞进来。
INVARIANTS = [
    ('kind 必须是已知集合', lambda r: 'kind' in r and r['kind'] in ('frames', 'clip')),
    ('note 必须是字符串', lambda r: isinstance(r.get('note'), str)),
    ('frames 必须是非负整数', lambda r: isinstance(r.get('frames'), int) and not isinstance(r.get('frames'), bool) and r['frames'] >= 0),
    ('lane 必须是 pool/go/空', lambda r: (r.get('lane') or '') in ('pool', 'go', '')),
    ('ts 必须是数字', lambda r: isinstance(r.get('ts'), (int, float))),
]


def load_rows(paths):
    rows = []
    for p in paths:
        with open(p, encoding='utf-8') as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError as e:
                    raise SystemExit('%s:%d 不是合法 JSON：%s' % (p, ln, e))
                r['_src'] = os.path.basename(p)
                rows.append(r)
    rows.sort(key=lambda r: r.get('ts') or 0)
    return rows


def local(ts):
    return datetime.datetime.fromtimestamp(ts)


def split_by(rows, cuts):
    """按本地时刻 'HH:MM' 切段；返回 [(label, rows)]。切点即段起点。"""
    if not cuts:
        return [('全部', rows)]
    def key_of(r):
        t = local(r['ts'])
        return t.hour * 60 + t.minute
    points = []
    for c in cuts:
        h, m = c.split(':')
        points.append(int(h) * 60 + int(m))
    points.sort()
    buckets = collections.OrderedDict()
    labels = ['..%s' % cuts[0]] + ['%s..%s' % (cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)] + ['%s..' % cuts[-1]]
    for lab in labels:
        buckets[lab] = []
    for r in rows:
        k = key_of(r)
        idx = 0
        for i, pt in enumerate(points):
            if k >= pt:
                idx = i + 1
        buckets[labels[idx]].append(r)
    return list(buckets.items())


def dup_groups(rows, n=12):
    """首 N 字相同 = 近重复组。返回 (组数, 涉及行数, Top5)。"""
    c = collections.Counter((r.get('note') or '')[:n] for r in rows)
    groups = [(k, v) for k, v in c.items() if v > 1 and k.strip()]
    groups.sort(key=lambda x: -x[1])
    return len(groups), sum(v for _, v in groups), groups[:5]


def audit(label, rows):
    n = len(rows)
    print('\n=== %s ===' % label)
    print('分母：%d 行' % n)
    if n == 0:
        return {'rows': 0}

    kinds = collections.Counter(r.get('kind') for r in rows)
    lanes = collections.Counter(r.get('lane') or '(空)' for r in rows)
    frames = collections.Counter(r.get('frames') for r in rows)
    tok = [r.get('tokens') or 0 for r in rows]
    skips = [r for r in rows if str(r.get('note') or '').strip() in ('', '[SKIP]')]
    leaks = [r for r in rows if '[SKIP]' in str(r.get('note') or '')]
    empty_win = [r for r in rows if not str(r.get('window') or '').strip()]
    no_mode = [r for r in rows if 'mode' not in r]
    dg_n, dg_rows, dg_top = dup_groups(rows)

    print('  kind      : %s' % dict(kinds))
    print('  lane      : %s' % dict(lanes))
    print('  帧数分布  : %s' % dict(sorted(frames.items(), key=lambda x: (x[0] is None, x[0]))))
    print('  token     : 合计 %d / 中位 %d / p90 %d' % (sum(tok), statistics.median(tok), sorted(tok)[int(len(tok) * 0.9)]))
    print('  沉默行    : %d/%d（%.1f%%）%s' % (
        len(skips), n, 100.0 * len(skips) / n,
        '' if skips else ' ← 沉默默认不落 notes（只进 ledger 的 skip 行），0 不代表从不沉默'))
    print('  [SKIP]泄漏: %d/%d（必须 0；>0 = clean_note 没生效）' % (len(leaks), n))
    for r in leaks[:3]:
        print('     ', repr((r.get('note') or '')[:48]))
    print('  空 window : %d/%d（%.1f%%；这些行走进程名归组，别和别的应用并桶）' % (len(empty_win), n, 100.0 * len(empty_win) / n))
    print('  缺 mode   : %d/%d（改动前的历史行，不是漏字段）' % (len(no_mode), n))
    print('  近重复    : %d 组 / %d 行（分母 %d；首12字相同即算一组）' % (dg_n, dg_rows, n))
    for k, v in dg_top:
        print('     %r x%d' % (k, v))

    bad = []
    for name, pred in INVARIANTS:
        offenders = [r for r in rows if not pred(r)]
        if offenders:
            bad.append('%s：%d 行违规，例 %r' % (name, len(offenders),
                                              {k: v for k, v in list(offenders[0].items())[:6]}))
    if leaks:
        bad.append('[SKIP] 泄漏 %d 行（clean_note 三形态应把它们全部消掉）' % len(leaks))
    return {'rows': n, 'bad': bad, 'leaks': len(leaks), 'dups': dg_n}


def selftest():
    """合成一份"已知答案"的笔记文件，验每一步算得对。"""
    t0 = datetime.datetime(2026, 11, 8, 12, 0, 0).timestamp()
    rows = [
        # 0: 正常 frames 行（有 mode）
        {'ts': t0 + 60, 'note': '第一句观察', 'window': 'W1', 'proc': 'a.exe', 'lane': 'pool',
         'tokens': 100, 'frames': 3, 'kind': 'frames', 'skip': False, 'mood': '', 'mode': ''},
        # 1: clip native（frames=0 + mode=native）
        {'ts': t0 + 120, 'note': '第二句观察', 'window': '录屏 · x', 'proc': 'b.exe', 'lane': 'go',
         'tokens': 200, 'frames': 0, 'kind': 'clip', 'skip': False, 'mood': '', 'mode': 'native'},
        # 2: 空 window（归组应走进程键，工具只统计）
        {'ts': t0 + 180, 'note': '第三句观察', 'window': '', 'proc': 'c.exe', 'lane': 'go',
         'tokens': 300, 'frames': 3, 'kind': 'frames', 'skip': False, 'mood': '', 'mode': ''},
        # 3: 与 0 首12字重复（构造近重复组）
        {'ts': t0 + 240, 'note': '第一句观察', 'window': 'W1', 'proc': 'a.exe', 'lane': 'pool',
         'tokens': 110, 'frames': 3, 'kind': 'frames', 'skip': False, 'mood': '', 'mode': ''},
        # 4: 历史行（缺 mode）
        {'ts': t0 + 300, 'note': '老的第五句', 'window': 'W2', 'proc': 'a.exe', 'lane': 'go',
         'tokens': 120, 'frames': 3, 'kind': 'frames', 'skip': False, 'mood': ''},
    ]
    d = tempfile.mkdtemp(prefix='notes-audit-')
    p = os.path.join(d, 'notes.jsonl')
    with open(p, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    got = load_rows([p])
    assert len(got) == 5, len(got)
    assert sum(1 for r in got if 'mode' not in r) == 1
    assert sum(1 for r in got if not str(r.get('window') or '').strip()) == 1
    n, involved, _ = dup_groups(got)
    assert (n, involved) == (1, 2), (n, involved)
    assert sum(r.get('tokens') or 0 for r in got) == 830

    seg = split_by(got, ['12:03'])
    # t0=12:00 → +60s=12:01、+120s=12:02 在切点前；+180/240/300 在切点后
    assert [len(b) for _, b in seg] == [2, 3], [(a, len(b)) for a, b in seg]

    # 泄漏检测：塞一条含 [SKIP] 的行，必须被 audit 计入 bad
    leak = dict(rows[0], ts=t0 + 360, note='这句被留下了[SKIP]')
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(leak, ensure_ascii=False) + '\n')
    got2 = load_rows([p])
    res = audit('自检', got2)
    assert res['leaks'] == 1 and any('SKIP' in b for b in res['bad']), res
    print('\n[OK] selftest 通过：行数/缺 mode/空 window/近重复/token 合计/切段/泄漏检测 全对')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='*', help='notes jsonl（可多个）')
    ap.add_argument('--split', default='', help='按本地时刻切段，如 12:41,13:57')
    ap.add_argument('--date', default='', help='只保留这一天（本地时区 YYYY-MM-DD）')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not a.files:
        ap.error('至少给一个 notes 文件（或用 --selftest）')

    rows = load_rows(a.files)
    if a.date:
        rows = [r for r in rows if local(r['ts']).strftime('%Y-%m-%d') == a.date]
    if not rows:
        print('没有匹配的行'); return 0

    span = '%s → %s' % (local(rows[0]['ts']).strftime('%Y-%m-%d %H:%M:%S'),
                        local(rows[-1]['ts']).strftime('%Y-%m-%d %H:%M:%S'))
    print('文件：%s' % ', '.join(a.files))
    print('时间跨度（本地时区）：%s，共 %d 行' % (span, len(rows)))

    cuts = [c for c in a.split.split(',') if c.strip()]
    results = []
    for label, seg in split_by(rows, cuts):
        results.append((label, audit(label, seg)))

    bad = [(lab, b) for lab, r in results for b in (r.get('bad') or [])]
    print('\n--- 汇总 ---')
    print('段数 %d，总行数 %d，硬不变量违规 %d 条' % (len(results), sum(r['rows'] for _, r in results), len(bad)))
    for lab, b in bad:
        print('  [FAIL] %s: %s' % (lab, b))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
