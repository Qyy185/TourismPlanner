# -*- coding: utf-8 -*-
"""
知识库平铺生成器 · 第三步（v2）：跨城路线库

把「城市总览」（每城玩什么）与「城际连接表」（怎么到下一座城）编排成逐日路线。

编排规则（纯规则，可解释、可追溯）：
  1. 顺序：地理邻近排一条不绕路的路径（最近邻构造 + 2-opt 优化）
  2. 天数：先按城际耗时扣交通日（≤4h 半天，>4h 一天），
           剩余按各城景点密度分配住宿夜数
  3. 校验：总天数 = sum(住宿夜数) + 1（最后一天返程）

v2 修正：
  · 天数多算 1 天（首日抵达已占一晚，完整游览日应为 nights-1）
  · 城市总览解析时把「全省 · xxx」段落误当成城市，景点串味
"""
import io, os, re, glob, collections, sys

ROOT = r'D:\毕业\智慧旅游规划助手'
sys.path.insert(0, ROOT)
from core.transport import _CITY, _haversine_km, _PROVINCE_CAPITAL
from core.rail_network import rail_link, transfer_hub, RAIL_LINES

KB = os.path.join(ROOT, 'knowledge_base', 'destinations')
OUT = os.path.join(KB, '_generated')
VERSION = 'v1.0'
DATE = '20261025'

_SPEED = {'road': 65, 'hsr': 240, 'flight': 700}
_TRANSFER = {'road': 0.3, 'hsr': 0.8, 'flight': 2.0}
_LINE_CITIES = set()
for _l in RAIL_LINES.values():
    _LINE_CITIES.update(_l)


def fmt_h(hours):
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h += 1; m = 0
    if h and m:
        return '%d 小时 %d 分' % (h, m)
    if h:
        return '%d 小时' % h
    return '%d 分钟' % m


def conn(a, b):
    la, lo, _ = _CITY[a]
    lb, lop, _ = _CITY[b]
    air_km = _haversine_km((la, lo), (lb, lop))
    rail_km = air_km * 1.27
    link = rail_link(a, b)
    hub_a, hub_b = transfer_hub(a), transfer_hub(b)

    if link['kind'] == 'direct':
        mode = 'hsr'
    elif link['kind'] == 'transfer':
        mode = 'hsr' if rail_km <= 1200 else 'flight'
    elif link['kind'] == 'partial':
        if not (hub_a or hub_b) and not (a in _LINE_CITIES or b in _LINE_CITIES):
            mode = 'road' if rail_km <= 200 else 'flight'
        else:
            mode = 'hsr' if rail_km <= 1200 else 'flight'
    else:
        mode = 'road' if rail_km <= 1200 else 'flight'

    hours = rail_km / _SPEED[mode] + _TRANSFER[mode]
    if link['kind'] == 'transfer':
        hours += 1.2
    if link['kind'] == 'partial':
        hours += 1.5
    if mode == 'hsr':
        if hub_a and hub_a in _CITY:
            hours += _haversine_km((la, lo), (_CITY[hub_a][0], _CITY[hub_a][1])) / _SPEED['road']
        if hub_b and hub_b in _CITY:
            hours += _haversine_km((lb, lop), (_CITY[hub_b][0], _CITY[hub_b][1])) / _SPEED['road']

    if mode == 'hsr':
        price = max(30.0, rail_km * 0.42)
    elif mode == 'road':
        price = max(30.0, round(rail_km * 0.28 / 5) * 5)
    else:
        base = max(420.0, 220.0 + air_km * 0.62)
        price = (base * 0.70, base * 1.20)

    return {'km': round(rail_km), 'hours': round(hours, 1), 'mode': mode,
            'price': price, 'note': link['note'], 'line': link['line']}


def mode_label(c):
    if c['mode'] == 'hsr':
        return '高铁%s' % ('（%s）' % c['line'] if c['line'] else '（需换乘）')
    if c['mode'] == 'road':
        return '公路 / 长途客车'
    return '飞机'


def price_label(c):
    p = c['price']
    if isinstance(p, tuple):
        return '%d—%d 元' % (round(p[0]), round(p[1]))
    return '%d 元' % round(p)


def load_city_spots(prov):
    """从城市总览读各城市的景点名。遇到「全省」段落立即停止收集。"""
    f = os.path.join(OUT, '智慧旅游规划助手—%s—城市总览—%s—%s.md' % (prov, VERSION, DATE))
    if not os.path.exists(f):
        return {}
    txt = io.open(f, encoding='utf-8').read()
    out = {}
    cur = None
    for line in txt.split('\n'):
        if line.startswith('## '):
            name = line[3:].strip()
            if name.startswith('全省') or name.startswith('城市索引') or name.startswith('全省概览'):
                cur = None            # 全省段落，不再收集景点
                continue
            cur = name
            out.setdefault(cur, [])
        elif line.startswith('### ') and cur:
            out[cur].append(line[4:].strip())
    return out


def order_cities(cities):
    if len(cities) <= 2:
        return list(cities)

    def d(a, b):
        return _haversine_km((_CITY[a][0], _CITY[a][1]), (_CITY[b][0], _CITY[b][1]))

    rest = list(cities)
    cur = rest.pop(0)
    path = [cur]
    while rest:
        nxt = min(rest, key=lambda c: d(cur, c))
        rest.remove(nxt)
        path.append(nxt)
        cur = nxt

    def total(p):
        return sum(d(p[i], p[i + 1]) for i in range(len(p) - 1))
    improved = True
    while improved:
        improved = False
        for i in range(1, len(path) - 2):
            for j in range(i + 1, len(path) - 1):
                cand = path[:i] + path[i:j + 1][::-1] + path[j + 1:]
                if total(cand) < total(path) - 1:
                    path = cand
                    improved = True
    return path


def allocate_nights(cities, total_days, spots_map):
    """分配住宿夜数。校验关系：总天数 = sum(nights) + 1"""
    n = len(cities)
    nights_total = total_days - 1
    if nights_total < n:
        nights_total = n

    # 长城际（>4h）额外占半天，从住宿夜里扣
    long_legs = 0
    for i in range(n - 1):
        c = conn(cities[i], cities[i + 1])
        if c['hours'] > 4:
            long_legs += 1
    budget = max(nights_total - long_legs, n)

    weights = [max(len(spots_map.get(c, [])), 1) for c in cities]
    sumw = sum(weights)
    nights = []
    for c, w in zip(cities, weights):
        nights.append(max(1, round(budget * w / sumw)))
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


def build_route(prov, cities, total_days, spots_map, title):
    order = order_cities(cities)
    alloc = allocate_nights(order, total_days, spots_map)
    L = []
    L.append('### %s' % title)
    L.append('')
    L.append('**路线顺序**：%s' % ' → '.join('%s（%d 晚）' % (c, nn) for c, nn in alloc))
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
                mode_label(c), c['km'], fmt_h(c['hours']), price_label(c)))
            L.append('- **衔接说明**：%s' % c['note'])
            L.append('- **当日安排**：%s；抵达后安排半天游览%s，傍晚入住，次日进入完整行程'
                     % ('上午出发，路上占用半天' if c['hours'] <= 4 else '全天在路上', city))
            L.append('- **住宿**：%s' % city)
            L.append('')
        day += 1

        # 完整游览日：首日的半天已在上面，因此这里只排 nights-1 天
        for k in range(max(0, nights - 1)):
            L.append('**Day%d｜%s 深度游（%d/%d）**' % (day, city, k + 2, nights))
            spots = spots_map.get(city, [])
            if spots:
                L.append('- **建议游览**：%s' % '、'.join(spots[:3]))
            else:
                L.append('- **建议游览**：以%s市区与近郊景点为主，详见「%s—城市总览」' % (city, prov))
            L.append('- **住宿**：住%s市中心或景区交通便利区域，减少每日往返时间' % city)
            L.append('- **留白**：当天留 1.5—2 小时机动时段，可补拍或休息')
            L.append('')
            day += 1

    last = alloc[-1][0]
    L.append('**末日｜%s 返程**' % last)
    L.append('')
    L.append('- **当日安排**：上午安排%s的收尾游览或采购伴手礼，下午返程' % last)
    L.append('')

    actual_days = day
    total_nights = sum(nn for _, nn in alloc)
    legs = [conn(alloc[i][0], alloc[i + 1][0]) for i in range(len(alloc) - 1)]
    transit_h = sum(l['hours'] for l in legs)
    L.append('**全程 %d 天 %d 晚** ｜ 城际 %d 段 ｜ 路上时间约 %s（占总时长约 %d%%）'
             % (actual_days, total_nights, len(legs), fmt_h(transit_h),
                round(transit_h / 24 / actual_days * 100)))
    L.append('')
    return '\n'.join(L), actual_days, transit_h


def gen_province(prov):
    spots_map = load_city_spots(prov)
    prov_cities = sorted(c for c, v in _CITY.items() if v[2] == prov)
    if len(prov_cities) < 3:
        return None

    scored = sorted(prov_cities, key=lambda c: -len(spots_map.get(c, [])))
    capital = {'台湾': '台北', '香港': '香港', '澳门': '澳门'}.get(
        prov, _PROVINCE_CAPITAL.get(prov, prov))
    picks = []
    for c in scored[:4]:
        if c not in picks:
            picks.append(c)
    if capital and capital not in picks:
        picks.insert(0, capital)
    picks = picks[:4]
    if len(picks) < 2:
        return None

    L = []
    L.append('# %s · 跨城路线' % prov)
    L.append('')
    L.append('> 本文件把「城市总览」（每城玩什么）与「城际连接表」（怎么到下一座城）编排成**可执行的逐日路线**。')
    L.append('> ')
    L.append('> **编排规则**（纯规则计算，可追溯）：')
    L.append('> 1. **顺序**：按地理邻近排一条不绕路的路径（最近邻构造 + 2-opt 优化），避免来回折返；')
    L.append('> 2. **天数**：先按城际耗时扣交通日（城际 ≤4 小时算半天，>4 小时算一天），')
    L.append('>    剩余天数再按各城景点密度分配住宿夜数；')
    L.append('> 3. **校验**：总天数 = 住宿夜数之和 + 1（末日返程）。')
    L.append('> ')
    L.append('> 配套文件：')
    L.append('> - 「%s—城市总览」讲每城玩什么；' % prov)
    L.append('> - 「%s—城际连接表」讲城际交通全表；' % prov)
    L.append('> - 「%s—主攻略」讲全省气候、节奏与预算口径。' % prov)
    L.append('')
    L.append('## 跨城主线')
    L.append('')

    plans = [
        (3, picks[:2], '3 日跨城主线（双城深度）'),
        (5, picks[:3], '5 日跨城主线（三城串联）'),
        (7, picks[:4], '7 日跨城主线（四城串联）'),
    ]
    summary = []
    for days, cs, title in plans:
        if len(cs) < 2:
            continue
        content, actual, transit = build_route(prov, cs, days, spots_map, title)
        L.append(content)
        summary.append((title, actual, transit, len(cs) - 1))

    if summary:
        L.append('## 编排依据小结')
        L.append('')
        L.append('| 线路 | 城市数 | 城际段数 | 总天数 | 路上时间 | 路上时间占比 |')
        L.append('|---|---|---|---|---|---|')
        for title, actual, transit, legs in summary:
            L.append('| %s | %d | %d | %d | 约 %s | 约 %d%% |'
                     % (title, legs + 1, legs, actual, fmt_h(transit),
                        round(transit / 24 / actual * 100)))
        L.append('')
        L.append('> 「路上时间占比」是单目的地规划回答不了的问题——')
        L.append('> 多城行程里，交通常常吃掉两成到三成的总时长，直接决定每个城能待几天。')
        L.append('')
    return '\n'.join(L)


def main():
    os.makedirs(OUT, exist_ok=True)
    provs = sorted(set(v[2] for v in _CITY.values()))
    done, skipped = 0, []
    for prov in provs:
        if prov in ('香港', '澳门', '台湾'):
            skipped.append(prov); continue
        content = gen_province(prov)
        if not content:
            skipped.append(prov); continue
        fname = '智慧旅游规划助手—%s—跨城路线—%s—%s.md' % (prov, VERSION, DATE)
        io.open(os.path.join(OUT, fname), 'w', encoding='utf-8').write(content)
        done += 1

    print('=' * 66)
    print('跨城路线库生成完成（v2）')
    print('=' * 66)
    print('生成 %d 份' % done)
    if skipped:
        print('跳过:', '、'.join(skipped))
    print()
    print('=== 抽样：黑龙江 ===')
    f = os.path.join(OUT, '智慧旅游规划助手—黑龙江—跨城路线—%s—%s.md' % (VERSION, DATE))
    txt = io.open(f, encoding='utf-8').read()
    for line in txt.split('\n'):
        if line.strip() and ('路线顺序' in line or '全程' in line or '城际交通' in line
                             or line.startswith('**Day') or '|' in line):
            print('  ', line[:140])


if __name__ == '__main__':
    main()
