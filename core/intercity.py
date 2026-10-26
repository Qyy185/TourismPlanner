# -*- coding: utf-8 -*-
"""
城际交通计算核心（城际连接表与跨城路线共用）

判定顺序（重要）：
  1. 两城直线距离 < DIRECT_KM → **直接公路/自驾**，不折算接驳站。
     （否则会出现「伊春→双峰林场 28km 却算 12 小时」这种荒谬结果：
      两端各自的接驳站相距数百公里，折算后时间爆掉）
  2. 否则看铁路可达性：
     · 同干线 → 直达（若有接驳站，另加该端的公路接驳段）
     · 不同干线但两端都有站 → 需换乘（加换乘候车余量）
     · 一端有站 → 公路接驳 + 铁路
     · 两端都无 → 公路 / 飞机
  3. 时长 = 主体行程时间 + 换乘余量 + 公路接驳段
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.transport import _CITY, _haversine_km, _hsr_price, _flight_price, _round5
from core.rail_network import rail_link, transfer_hub, RAIL_LINES

DIRECT_KM = 150          # km 以内直接公路，不绕接驳站
ROAD_MAX = 200           # km 以内公路
HSR_MAX = 1200           # km 以内铁路覆盖
_TRANSFER = {'road': 0.3, 'hsr': 0.8, 'flight': 2.0}
_SPEED = {'road': 65, 'hsr': 240, 'flight': 700}
_RAIL_FACTOR = 1.27

_LINE_CITIES = set()
for _l in RAIL_LINES.values():
    _LINE_CITIES.update(_l)


def fmt_h(hours):
    """小时数 → 「2 小时 12 分」"""
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h += 1; m = 0
    if h and m:
        return '%d 小时 %d 分' % (h, m)
    if h:
        return '%d 小时' % h
    return '%d 分钟' % m


def price_label(p):
    if isinstance(p, tuple):
        return '%d—%d 元' % (round(p[0]), round(p[1]))
    return '%d 元' % round(p)


def mode_label(mode, line):
    if mode == 'hsr':
        return '高铁%s' % ('（%s）' % line if line else '（需换乘）')
    if mode == 'road':
        return '公路 / 长途客车'
    return '飞机（需机场接驳）'


def _road_km(a, b):
    """公路里程近似：直线 × 1.25（公路绕行系数）"""
    la, lo, _ = _CITY[a]
    lb, lop, _ = _CITY[b]
    return _haversine_km((la, lo), (lb, lop)) * 1.25


def _shuttle_km(city, hub):
    """城市到接驳站的公路里程"""
    if not hub or hub not in _CITY:
        return 0.0
    la, lo, _ = _CITY[city]
    hl, ho, _ = _CITY[hub]
    return _haversine_km((la, lo), (hl, ho)) * 1.3


def conn(a, b):
    """算两城之间的城际连接信息。

    返回 dict：km / hours / mode / mode_name / price / note / line / legs
    """
    # 少数城市不在坐标库（如台中、高雄），无法算距离——返回未知连接
    if a not in _CITY or b not in _CITY:
        return {'km': 0, 'hours': 0.0, 'mode': 'road',
                'mode_name': '需现场确认', 'price': 0,
                'note': '%s 或 %s 无坐标数据，交通方式需另行确认' % (a, b),
                'line': '', 'legs': []}
    la, lo, _ = _CITY[a]
    lb, lop, _ = _CITY[b]
    air_km = _haversine_km((la, lo), (lb, lop))
    rail_km = air_km * _RAIL_FACTOR
    link = rail_link(a, b)
    hub_a, hub_b = transfer_hub(a), transfer_hub(b)

    # ---------- 情况 1：近程，直接公路 ----------
    if air_km < DIRECT_KM:
        km = round(air_km * 1.25)
        hours = km / _SPEED['road'] + 0.2
        price = _round5(max(km * 0.28, 30))
        note = '两地相距不足 %d km，直接公路/自驾最省时' % DIRECT_KM
        return {'km': km, 'hours': round(hours, 1), 'mode': 'road',
                'mode_name': '公路 / 自驾', 'price': price, 'note': note,
                'line': '', 'legs': []}

    # ---------- 情况 2：走铁路 ----------
    if link['kind'] == 'direct':
        mode = 'hsr'
    elif link['kind'] == 'transfer':
        mode = 'hsr' if rail_km <= HSR_MAX else 'flight'
    elif link['kind'] == 'partial':
        # 一端无铁路可达（既非车站城市也没登记接驳站）→ 该端只能公路
        no_rail_a = not (a in _LINE_CITIES or hub_a)
        no_rail_b = not (b in _LINE_CITIES or hub_b)
        if no_rail_a and no_rail_b:
            mode = 'road' if rail_km <= ROAD_MAX else 'flight'
        elif no_rail_a or no_rail_b:
            # 只有一端能坐火车 → 公路 + 铁路混合
            mode = 'hsr' if rail_km <= HSR_MAX else 'flight'
        else:
            mode = 'hsr' if rail_km <= HSR_MAX else 'flight'
    else:
        # 两端都无铁路车站
        mode = 'road' if rail_km <= HSR_MAX else 'flight'

    hours = rail_km / _SPEED[mode] + _TRANSFER[mode]
    legs = []

    # 公路接驳段：只有该端确实需要接驳时才计入
    if mode == 'hsr':
        if hub_a:
            d = _shuttle_km(a, hub_a)
            if d > 1:
                hours += d / _SPEED['road']
                legs.append(('公路接驳', a, hub_a, round(d)))
        if hub_b:
            d = _shuttle_km(b, hub_b)
            if d > 1:
                hours += d / _SPEED['road']
                legs.append(('公路接驳', hub_b, b, round(d)))
    if link['kind'] == 'transfer':
        hours += 1.2          # 换乘候车余量

    if mode == 'hsr':
        price = _hsr_price(rail_km)
    elif mode == 'road':
        price = _round5(max(rail_km * 0.28, 30))
    else:
        price = _flight_price(air_km)

    return {'km': round(rail_km), 'hours': round(hours, 1), 'mode': mode,
            'mode_name': mode_label(mode, link['line']), 'price': price,
            'note': link['note'], 'line': link['line'], 'legs': legs}
