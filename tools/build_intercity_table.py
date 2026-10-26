# -*- coding: utf-8 -*-
"""
知识库平铺生成器 · 第二步（v3）：城际连接表

用 core/intercity.py 的统一判定，生成每省一份城际连接表。
距离、时长、方式、票价、铁路可达性全部来自真实地理与铁路数据。
"""
import io, os, re, glob, collections, sys

ROOT = r'D:\毕业\智慧旅游规划助手'
sys.path.insert(0, ROOT)
from core.transport import _CITY, _haversine_km, _PROVINCE_CAPITAL
from core.intercity import conn, fmt_h, price_label
from core.rail_network import RAIL_LINES

KB = os.path.join(ROOT, 'knowledge_base', 'destinations')
OUT = os.path.join(KB, '_generated')
VERSION = 'v1.0'
DATE = '20261025'

_PROV_CENTER = {}


def build_prov_center():
    byprov = collections.defaultdict(list)
    for c, v in _CITY.items():
        byprov[v[2]].append(c)
    for p, cs in byprov.items():
        if p in ('北京', '上海', '天津', '重庆', '香港', '澳门'):
            _PROV_CENTER[p] = p
            continue
        if p == '台湾':
            _PROV_CENTER[p] = '台北'
            continue
        xs = [_CITY[c][0] for c in cs]
        ys = [_CITY[c][1] for c in cs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        _PROV_CENTER[p] = min(cs, key=lambda c: ((_CITY[c][0] - mx) ** 2 + (_CITY[c][1] - my) ** 2))


def province_cities(prov):
    return sorted(c for c, v in _CITY.items() if v[2] == prov)


def neighbors(prov):
    out = []
    c1 = _PROV_CENTER.get(prov)
    if not c1:
        return []
    for p2 in set(v[2] for v in _CITY.values()):
        if p2 == prov:
            continue
        c2 = _PROV_CENTER.get(p2)
        if not c2:
            continue
        d = _haversine_km((_CITY[c1][0], _CITY[c1][1]), (_CITY[c2][0], _CITY[c2][1]))
        if d < 700:
            out.append((p2, d))
    return [p for p, _ in sorted(out, key=lambda x: x[1])]


def gen_province(prov, cities):
    L = []
    L.append('# %s · 城际连接表' % prov)
    L.append('')
    L.append('> 本文件记录本省各城市之间、以及本省与邻省之间的**城际交通连接**：距离、时长、方式、票价。')
    L.append('> 三份配套文件各管一件事：')
    L.append('> - 「%s—城市总览」讲**在这座城玩什么**；' % prov)
    L.append('> - 本文件讲**怎么从这座城到下一座城**；')
    L.append('> - 「%s—跨城路线」讲**按几天分配、怎么串成一条线**。' % prov)
    L.append('> ')
    L.append('> **数据口径**：')
    L.append('> - 距离：球面直线距离 × 1.27（铁路绕行系数）；近程公路另按 1.25 折算；')
    L.append('> - 铁路可达性：依据真实高铁干线（见 `core/rail_network.py`），')
    L.append('>   标注「直达 / 需换乘 / 需公路接驳」；')
    L.append('> - 时长：含中转换乘余量与两端公路接驳段；')
    L.append('> - 票价：二等座 / 客车 / 经济舱参考价，**以实际购票为准**。')
    L.append('')

    L.append('## 省内城市连接')
    L.append('')
    if len(cities) < 2:
        L += ['> 本省仅登记 1 个城市，无省内城际连接。', '']
    else:
        L.append('| 从 | 到 | 距离 | 交通方式 | 约需时间 | 参考票价（单人） | 铁路说明 |')
        L.append('|---|---|---|---|---|---|---|')
        rows = []
        for i, a in enumerate(cities):
            for b in cities[i + 1:]:
                rows.append((a, b, conn(a, b)))
        rows.sort(key=lambda r: r[2]['km'])
        for a, b, c in rows:
            L.append('| %s | %s | 约 %d km | %s | 约 %s | %s | %s |'
                     % (a, b, c['km'], c['mode_name'], fmt_h(c['hours']),
                        price_label(c['price']), c['note']))
        L.append('')

    L.append('## 跨省连接')
    L.append('')
    nb = neighbors(prov)
    if not nb:
        L += ['> 本省无邻近省份登记。', '']
    else:
        L.append('| 从 | 到 | 所属省 | 距离 | 交通方式 | 约需时间 | 参考票价（单人） | 铁路说明 |')
        L.append('|---|---|---|---|---|---|---|---|')
        seen = set()
        for a in cities[:5]:
            for p2 in nb[:4]:
                p2cities = province_cities(p2)
                if not p2cities:
                    continue
                near = sorted(p2cities, key=lambda c: _haversine_km(
                    (_CITY[a][0], _CITY[a][1]), (_CITY[c][0], _CITY[c][1])))[:2]
                for b in near:
                    if (a, b) in seen:
                        continue
                    seen.add((a, b))
                    c = conn(a, b)
                    L.append('| %s | %s | %s | 约 %d km | %s | 约 %s | %s | %s |'
                             % (a, b, p2, c['km'], c['mode_name'], fmt_h(c['hours']),
                                price_label(c['price']), c['note']))
        L.append('')

    cap = _PROV_CENTER.get(prov)
    if cap and cap in _CITY:
        near_scenic = []
        for c in cities:
            if c == cap:
                continue
            d = _haversine_km((_CITY[cap][0], _CITY[cap][1]), (_CITY[c][0], _CITY[c][1]))
            if d < 260:
                near_scenic.append((c, d))
        if near_scenic:
            L.append('## 卫星城与景区节点')
            L.append('')
            L.append('下列节点距%s较近，多为景区或近郊目的地，**可当日往返，也可作为中途停靠点**。' % cap)
            L.append('')
            L.append('| 节点 | 距%s | 交通方式 | 约需时间 | 说明 |' % cap)
            L.append('|---|---|---|---|---|')
            for c, d in sorted(near_scenic, key=lambda x: x[1]):
                info = conn(cap, c)
                L.append('| %s | 约 %d km | %s | 约 %s | 与%s连成一日往返线 |'
                         % (c, round(d), info['mode_name'], fmt_h(info['hours']), cap))
            L.append('')

    return '\n'.join(L)


def main():
    os.makedirs(OUT, exist_ok=True)
    build_prov_center()
    provs = sorted(set(v[2] for v in _CITY.values()))
    report = []
    total = 0
    for prov in provs:
        if prov in ('香港', '澳门', '台湾'):
            continue
        cities = province_cities(prov)
        content = gen_province(prov, cities)
        fname = '智慧旅游规划助手—%s—城际连接表—%s—%s.md' % (prov, VERSION, DATE)
        io.open(os.path.join(OUT, fname), 'w', encoding='utf-8').write(content)
        rows = content.count('\n| ')
        total += rows
        report.append((prov, len(cities), rows))

    print('=' * 70)
    print('城际连接表生成完成（v3 · 统一走 core/intercity.py）')
    print('=' * 70)
    print('%-10s %8s %10s' % ('省份', '城市数', '连接行数'))
    print('-' * 70)
    for p, c, r in report:
        print('%-10s %8d %10d' % (p, c, r))
    print('-' * 70)
    print('%-10s %8s %10d' % ('合计', '', total))
    print()
    print('登记高铁干线:', len(RAIL_LINES), '条')
    print()
    print('=== 抽样：黑龙江（导师例子的省份）===')
    f = os.path.join(OUT, '智慧旅游规划助手—黑龙江—城际连接表—v1.0—%s.md' % DATE)
    for line in io.open(f, encoding='utf-8').read().split('\n'):
        if line.startswith('| ') and '---' not in line:
            for k in ('哈尔滨', '齐齐哈尔', '五大连池', '伊春', '扎龙', '双峰林场'):
                if k in line:
                    print('  ', line[:145])
                    break


if __name__ == '__main__':
    main()
