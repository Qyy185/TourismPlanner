# -*- coding: utf-8 -*-
"""
知识库平铺生成器 · 第五步：跨省路线库

现有「跨城路线」只在省内串联。本步骤补上**跨省**版本：
用城际连接表的「跨省连接」段，组合出跨两省及以上的多城路线。

典型场景（导师会问的）：
  「10 天去东北，主打自然风光」→ 哈尔滨 → 齐齐哈尔 → 五大连池 → 长春 → 沈阳
  「一周玩完云南和贵州」    → 昆明 → 大理 → 丽江 → 贵阳 → 安顺 → 桂林

编排规则（与省内跨城路线一致，纯规则）：
  1. 候选城市：取各邻省的「城市专册」中的城市
  2. 顺序：按地理邻近排一条不绕路的路径（最近邻 + 2-opt）
  3. 天数：先扣城际交通日，剩余按景点密度分配住宿夜数
  4. 校验：总天数 = 住宿夜数之和 + 1
"""
import io, os, re, glob, collections, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.transport import _CITY, _haversine_km, _PROVINCE_CAPITAL
from core.intercity import conn, fmt_h, price_label
from core.route_order import order_cities

KB = os.path.join(ROOT, 'knowledge_base', 'destinations')
OUT = os.path.join(KB, '_generated')
VERSION = 'v1.0'
DATE = '20261026'


def load_profiles():
    """读所有城市专册 → {省: {城市: [景点名]}}"""
    out = collections.defaultdict(dict)
    for f in glob.glob(os.path.join(OUT, '*—城市专册—*.md')):
        prov = os.path.basename(f).split('—')[1]
        cur = None
        for line in io.open(f, encoding='utf-8').read().split('\n'):
            if line.startswith('## '):
                name = line[3:].strip()
                cur = None if (name.startswith('全省') or name.startswith('城市索引')
                               or name.startswith('配套')) else name
                if cur and cur not in out[prov]:
                    out[prov][cur] = []
            elif line.startswith('### ') and cur:
                out[prov][cur].append(line[4:].strip())
    return out


def province_center(prov, cities):
    """该省的几何中心代表城市（用于判断邻近省）"""
    if not cities:
        return None
    xs = [_CITY[c][0] for c in cities if c in _CITY]
    ys = [_CITY[c][1] for c in cities if c in _CITY]
    if not xs:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cands = [c for c in cities if c in _CITY]
    if not cands:
        return None
    return min(cands, key=lambda c: ((_CITY[c][0] - mx) ** 2 + (_CITY[c][1] - my) ** 2))


def allocate_nights(cities, total_days, density):
    n = len(cities)
    nights_total = max(total_days - 1, n)
    long_legs = sum(1 for i in range(n - 1) if conn(cities[i], cities[i + 1])['hours'] > 4)
    budget = max(nights_total - long_legs, n)
    weights = [max(density.get(c, 1), 1) for c in cities]
    sumw = sum(weights)
    nights = [max(1, round(budget * w / sumw)) for w in weights]
    diff = budget - sum(nights)
    i = 0
    while diff != 0 and nights:
        j = i % len(nights)
        if diff > 0:
            nights[j] += 1; diff -= 1
        elif nights[j] > 1:
            nights[j] -= 1; diff += 1
        i += 1
    return list(zip(cities, nights))


def build_route(cities, total_days, density, title, provs):
    order = order_cities(cities)
    alloc = allocate_nights(order, total_days, density)
    L = []
    L.append('### %s' % title)
    L.append('')
    L.append('**路线顺序**：%s' % ' → '.join('%s（%d 晚）' % (c, n) for c, n in alloc))
    L.append('**跨越省份**：%s' % '、'.join(provs))
    L.append('')

    day = 1
    for i, (city, nights) in enumerate(alloc):
        if i == 0:
            L.append('**Day%d｜抵达 %s**' % (day, city))
            L.append('')
            L.append('- **当日安排**：抵达%s后安排半天慢游，首日不排满，留出安顿与倒时差时间' % city)
            L.append('- **住宿**：%s' % city)
            L.append('')
        else:
            prev = alloc[i - 1][0]
            c = conn(prev, city)
            L.append('**Day%d｜%s → %s**' % (day, prev, city))
            L.append('')
            L.append('- **城际交通**：%s，约 %d km，%s，参考票价 %s' % (
                c['mode_name'], c['km'], fmt_h(c['hours']), price_label(c['price'])))
            L.append('- **衔接说明**：%s' % c['note'])
            for kind, frm, to, d in c.get('legs', []):
                L.append('- **接驳段**：%s → %s 约 %d km（%s）' % (frm, to, d, kind))
            L.append('- **当日安排**：%s；抵达后安排半天游览%s，傍晚入住，次日进入完整行程'
                     % ('上午出发，路上占用半天' if c['hours'] <= 4 else '全天在路上', city))
            L.append('- **住宿**：%s' % city)
            L.append('')
        day += 1

        for k in range(max(0, nights - 1)):
            L.append('**Day%d｜%s 深度游（%d/%d）**' % (day, city, k + 2, nights))
            L.append('- **建议游览**：见「%s—城市专册」' % city)
            L.append('- **住宿**：住%s市中心或景区交通便利区域，减少每日往返时间' % city)
            L.append('- **留白**：当天留 1.5—2 小时机动时段，可补拍或休息')
            L.append('')
            day += 1

    last = alloc[-1][0]
    L.append('**末日｜%s 返程**' % last)
    L.append('')
    L.append('- **当日安排**：上午安排%s的收尾游览或采购伴手礼，下午返程' % last)
    L.append('')

    legs = [conn(alloc[i][0], alloc[i + 1][0]) for i in range(len(alloc) - 1)]
    transit_h = sum(l['hours'] for l in legs)
    L.append('**全程 %d 天 %d 晚** ｜ 城际 %d 段 ｜ 路上时间约 %s（占总时长约 %d%%）'
             % (day, sum(n for _, n in alloc), len(legs), fmt_h(transit_h),
                round(transit_h / 24 / day * 100)))
    L.append('')
    return '\n'.join(L), day, transit_h


def main():
    profiles = load_profiles()
    if not profiles:
        print('❌ 没有读到城市专册，先跑 build_city_profiles.py')
        return

    # 全局景点密度
    density = {}
    for prov, cities in profiles.items():
        for c, spots in cities.items():
            density[c] = len(spots)

    # 找「地理上连片」的省组
    provs_all = sorted(profiles.keys())
    centers = {p: province_center(p, list(profiles[p].keys())) for p in provs_all}
    centers = {p: c for p, c in centers.items() if c}

    # 邻近省（省中心距离 < 600km）
    groups = []
    used = set()
    for p in provs_all:
        if p in used or p not in centers:
            continue
        group = [p]
        used.add(p)
        for q in provs_all:
            if q in used or q not in centers:
                continue
            d = _haversine_km((_CITY[centers[p]][0], _CITY[centers[p]][1]),
                              (_CITY[centers[q]][0], _CITY[centers[q]][1]))
            if d < 600:
                group.append(q)
                used.add(q)
        if len(group) >= 2:
            groups.append(sorted(group))

    print('=' * 66)
    print('跨省路线库生成完成')
    print('=' * 66)
    print('识别到 %d 个连片省组:' % len(groups))
    for g in groups:
        print('  ', ' + '.join(g))
    print()

    total = 0
    for group in groups:
        # 取每省的代表城市（景点最多的 2 个）
        picks = []
        prov_of = {}
        for p in group:
            cs = sorted(profiles[p].items(), key=lambda x: -len(x[1]))[:2]
            for c, _ in cs:
                if c not in picks:
                    picks.append(c)
                    prov_of[c] = p
        if len(picks) < 3:
            continue

        plans = [
            (5, picks[:3], '5 日跨省主线（%s）' % '—'.join(group[:2])),
            (7, picks[:4], '7 日跨省主线（%s）' % '—'.join(group[:2])),
            (10, picks[:6], '10 日跨省主线（%s）' % '—'.join(group[:2])),
        ]

        L = []
        title = ' + '.join(group)
        L.append('# %s · 跨省路线' % title)
        L.append('')
        L.append('> 本文件是**跨省**多城串线路线，覆盖 %s。' % '、'.join(group))
        L.append('> ')
        L.append('> **编排规则**（纯规则计算，可追溯）：')
        L.append('> 1. **顺序**：按地理邻近排一条不绕路的路径（最近邻构造 + 2-opt 优化）；')
        L.append('> 2. **天数**：先按城际耗时扣交通日（城际 ≤4 小时算半天，>4 小时算一天），')
        L.append('>    剩余天数再按各城景点密度分配住宿夜数；')
        L.append('> 3. **校验**：总天数 = 住宿夜数之和 + 1（末日返程）。')
        L.append('> ')
        L.append('> 跨省行程的关键差异：**城际段更长、路上时间占比更高**，')
        L.append('> 因此每城能停留的天数更少，需要按「同主题跨省」来取舍。')
        L.append('')
        L.append('## 跨省主线')
        L.append('')

        summary = []
        for days, cs, t in plans:
            if len(cs) < 2:
                continue
            provs = []
            for c in cs:
                pv = prov_of.get(c, '')
                if pv and pv not in provs:
                    provs.append(pv)
            content, actual, transit = build_route(cs, days, density, t, provs)
            L.append(content)
            summary.append((t, actual, transit, len(cs) - 1, '、'.join(provs)))

        if summary:
            L.append('## 编排依据小结')
            L.append('')
            L.append('| 线路 | 涉及省份 | 城市数 | 城际段数 | 总天数 | 路上时间 | 路上时间占比 |')
            L.append('|---|---|---|---|---|---|---|')
            for t, actual, transit, legs, pv in summary:
                L.append('| %s | %s | %d | %d | %d | 约 %s | 约 %d%% |'
                         % (t, pv, legs + 1, legs, actual, fmt_h(transit),
                            round(transit / 24 / actual * 100)))
            L.append('')

        fname = '智慧旅游规划助手—%s—跨省路线—%s—%s.md' % ('+'.join(group), VERSION, DATE)
        io.open(os.path.join(OUT, fname), 'w', encoding='utf-8').write('\n'.join(L))
        total += 1
        print('  ✅ %s（%d 条主线）' % (fname.replace('智慧旅游规划助手—', '').replace('—v1.0—%s.md' % DATE, ''), len(summary)))

    print()
    print('生成 %d 份跨省路线' % total)


if __name__ == '__main__':
    main()
