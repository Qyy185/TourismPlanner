# -*- coding: utf-8 -*-
"""
知识库平铺生成器 · 第二步（v2）：城际连接表

解决导师例子里的核心知识缺口：「城与城之间怎么连」。
用真实地理与铁路数据，不编造：
  · 距离：haversine 球面距离 × 铁路绕行系数 1.27
  · 铁路可达性：core/rail_network.py 的真实高铁干线表
    （同干线 → 直达；不同干线 → 需换乘；一端有站 → 公路接驳；都无 → 公路/航空）
  · 时长：按交通方式推算，含中转与接驳余量
  · 票价：沿用 transport.py 的三档定价口径
"""
import io, os, re, glob, collections, sys

ROOT = r'D:\毕业\智慧旅游规划助手'
sys.path.insert(0, ROOT)
from core.transport import _CITY, _haversine_km, _hsr_price, _flight_price, _round5
from core.rail_network import rail_link, RAIL_LINES, transfer_hub, has_rail_reachable

KB = os.path.join(ROOT, 'knowledge_base', 'destinations')
OUT = os.path.join(KB, '_generated')
VERSION = 'v1.0'
DATE = '20261025'

_RAIL_FACTOR = 1.27
ROAD_MAX = 200          # km 以内公路直达
HSR_MAX = 1200         # km 以内铁路可覆盖
_TRANSFER = {'road': 0.3, 'hsr': 0.8, 'flight': 2.0}
_SPEED = {'road': 65, 'hsr': 240, 'flight': 700}

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


def pick_mode(rail_km, link):
    """结合铁路可达性决定交通方式"""
    kind = link['kind']
    if kind == 'direct':
        return 'hsr'
    if kind == 'transfer':
        return 'hsr' if rail_km <= HSR_MAX else 'flight'
    if kind == 'partial':
        # 一端需公路接驳：缺接驳站说明那端根本没有铁路，只能走公路或航空
        if not link.get('rail_reachable_b'):
            return 'road' if rail_km <= ROAD_MAX else 'flight'
        return 'hsr' if rail_km <= HSR_MAX else 'flight'
    # none：两地均无铁路车站
    if rail_km <= ROAD_MAX:
        return 'road'
    if rail_km <= HSR_MAX:
        return 'road'      # 长途客车
    return 'flight'


def mode_name(mode, link):
    if mode == 'road':
        if link['kind'] == 'none' and link.get('note', '').find('均未登记高铁站') >= 0:
            return '长途客车 / 自驾'
        return '公路 / 城际大巴'
    if mode == 'hsr':
        if link['kind'] == 'direct':
            return '高铁（%s直达）' % link['line'] if link['line'] else '高铁直达'
        return '高铁（需换乘）'
    return '飞机（需机场接驳）'


def estimate_conn(a, b):
    la, lo, _ = _CITY[a]
    lb, lop, _ = _CITY[b]
    air_km = _haversine_km((la, lo), (lb, lop))
    rail_km = air_km * _RAIL_FACTOR
    link = rail_link(a, b)
    link['rail_reachable_b'] = has_rail_reachable(b)
    link['rail_reachable_a'] = has_rail_reachable(a)
    mode = pick_mode(rail_km, link)
    hours = rail_km / _SPEED[mode] + _TRANSFER[mode]

    # 公路接驳段：景区级节点到其接驳站，必须单独计入
    hub_a = transfer_hub(a)
    hub_b = transfer_hub(b)
    if mode == 'hsr':
        if hub_a and hub_a in _CITY:
            d = _haversine_km((la, lo), (_CITY[hub_a][0], _CITY[hub_a][1]))
            hours += d / _SPEED['road']
        if hub_b and hub_b in _CITY:
            d = _haversine_km((lb, lop), (_CITY[hub_b][0], _CITY[hub_b][1]))
            hours += d / _SPEED['road']

    if link['kind'] == 'transfer':
        hours += 1.2          # 换乘候车余量
    if link['kind'] == 'partial':
        hours += 1.5          # 公路接驳 + 候车
    if mode == 'hsr':
        price = _hsr_price(rail_km)
    elif mode == 'road':
        price = _round5(max(rail_km * 0.28, 30))
    else:
        price = _flight_price(air_km)
    return {
        'air_km': round(air_km), 'rail_km': round(rail_km),
        'mode': mode, 'mode_name': mode_name(mode, link),
        'hours': round(hours, 1), 'price': price, 'link': link,
    }


def price_text(info):
    p = info['price']
    if isinstance(p, tuple):
        return '%d—%d 元' % (round(p[0]), round(p[1]))
    return '%d 元' % round(p)


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
    L.append('> - 「{省}—城市总览」讲**在这座城玩什么**；')
    L.append('> - 本文件讲**怎么从这座城到下一座城**；')
    L.append('> - 「{省}—跨城路线」讲**按几天分配、怎么串成一条线**。')
    L.append('> ')
    L.append('> **数据口径**：距离为球面直线距离乘以铁路绕行系数 1.27（全国均值）；')
    L.append('> 铁路可达性依据真实高铁干线（见 core/rail_network.py），')
    L.append('> 标注「直达 / 需换乘 / 需公路接驳」；时长含中转与接驳余量；')
    L.append('> 票价为二等座 / 客车 / 经济舱参考价，以实际购票为准。')
    L.append('')

    # 省内
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
                rows.append((a, b, estimate_conn(a, b)))
        rows.sort(key=lambda r: r[2]['rail_km'])
        for a, b, info in rows:
            L.append('| %s | %s | 约 %d km | %s | 约 %s | %s | %s |'
                     % (a, b, info['rail_km'], info['mode_name'],
                        fmt_h(info['hours']), price_text(info), info['link']['note']))
        L.append('')

    # 跨省
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
                    info = estimate_conn(a, b)
                    L.append('| %s | %s | %s | 约 %d km | %s | 约 %s | %s | %s |'
                             % (a, b, p2, info['rail_km'], info['mode_name'],
                                fmt_h(info['hours']), price_text(info), info['link']['note']))
        L.append('')

    # 卫星城
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
                info = estimate_conn(cap, c)
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
    print('城际连接表生成完成（v2 · 用真实高铁干线判定可达性）')
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
        if '|' in line and ('齐齐哈尔' in line or '五大连池' in line) and '---' not in line:
            print('  ', line[:150])


if __name__ == '__main__':
    main()
