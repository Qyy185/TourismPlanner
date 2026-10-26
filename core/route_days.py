# -*- coding: utf-8 -*-
"""
逐日骨架生成（编排层各处的公共实现）

天数口径（重要，之前算错过）：
  · 「城际 ≤4 小时」算半天移动 → 移动日与到达城的**第一个游览日合并**，
    即半天在路上、半天在到达城市活动
  · 「城际 >4 小时」算一整天移动 → 移动日**独占**，到达城的游览日从次日算起

  于是总天数 = 住宿夜数之和 + 长途城际段数 + 1（末日返程）
  校验关系：住宿夜数 = 总天数 - 1 - 长途城际段数
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.intercity import conn, fmt_h, price_label


def build_days(alloc):
    """把 [(城市, 晚数), ...] 变成逐日骨架。

    返回 (day_list, legs, total_days)
      day_list 每项：day / type / city / from / to / brief / ...
      type: arrive（首城抵达）/ transfer（城际移动）/ full（完整游览日）/ return（返程）
    """
    day_list = []
    day = 1
    legs = []

    for i, (city, nights) in enumerate(alloc):
        if i == 0:
            day_list.append({
                'day': day, 'type': 'arrive', 'city': city,
                'brief': '抵达%s后安排半天慢游，首日不排满，留出安顿与倒时差时间' % city,
            })
            day += 1
            # 首城：抵达半天已算第 1 个游览日
            for k in range(max(0, nights - 1)):
                day_list.append({
                    'day': day, 'type': 'full', 'city': city,
                    'brief': '%s 完整游览日（含机动时段与雨天备选）' % city,
                })
                day += 1
            continue

        prev = alloc[i - 1][0]
        c = conn(prev, city)
        legs.append((prev, city, c))
        long_leg = c['hours'] > 4

        if long_leg:
            # 全天在路上：移动日独占，到达城的游览从次日开始
            day_list.append({
                'day': day, 'type': 'transfer', 'from': prev, 'to': city,
                'mode': c['mode_name'], 'km': c['km'],
                'hours': c['hours'], 'hours_text': fmt_h(c['hours']),
                'price': price_label(c['price']), 'note': c['note'],
                'legs': c.get('legs', []), 'full_move': True,
                'brief': '%s → %s，%s，约 %d km，%s，参考票价 %s；'
                         '**当天基本在路上，抵达后直接入住休息**' % (
                             prev, city, c['mode_name'], c['km'], fmt_h(c['hours']),
                             price_label(c['price'])),
            })
            day += 1
            # 长途到达：抵达当晚住 1 晚，之后再排 (nights-1) 个完整日，
            # 合计 nights 晚（与短段口径一致，只是游览日从次日开始）
            full_days = max(0, nights - 1)
        else:
            # 半天移动：与到达城第一个游览日合并
            day_list.append({
                'day': day, 'type': 'transfer', 'from': prev, 'to': city,
                'mode': c['mode_name'], 'km': c['km'],
                'hours': c['hours'], 'hours_text': fmt_h(c['hours']),
                'price': price_label(c['price']), 'note': c['note'],
                'legs': c.get('legs', []), 'full_move': False,
                'brief': '%s → %s，%s，约 %d km，%s，参考票价 %s；'
                         '抵达后安排半天游览%s，傍晚入住' % (
                             prev, city, c['mode_name'], c['km'], fmt_h(c['hours']),
                             price_label(c['price']), city),
            })
            day += 1
            full_days = max(0, nights - 1)

        for k in range(full_days):
            day_list.append({
                'day': day, 'type': 'full', 'city': city,
                'brief': '%s 完整游览日（含机动时段与雨天备选）' % city,
            })
            day += 1

    last = alloc[-1][0]
    day_list.append({
        'day': day, 'type': 'return', 'city': last,
        'brief': '上午安排%s的收尾游览或采购伴手礼，下午返程' % last,
    })
    return day_list, legs, day


def count_long_legs(cities):
    """统计长途城际段数（>4 小时），用于天数分配"""
    n = 0
    for i in range(len(cities) - 1):
        if conn(cities[i], cities[i + 1])['hours'] > 4:
            n += 1
    return n


def allocate_nights(cities, total_days, user_nights=None, density=None):
    """分配各城住宿夜数。

    口径（与 build_days 严格对应）：
      · 每城占「自己的 nights 晚 + nights 天」——
        到达日算 1 天 1 晚，之后每个完整游览日各占 1 天 1 晚
      · 城际 ≤4 小时：移动半天，与到达日的下午活动合并
      · 城际 >4 小时：移动整天，到达日只入住不安排游览
        （**不额外消耗天数**——当晚照样住，只是这天没得玩）
      · 因此：住宿夜数合计 = 总天数 - 1（末日返程）

    分配依据：用户指定的晚数优先；其余按各城景点密度分摊。
    """
    n = len(cities)
    if n == 0:
        return []
    nights_total = max(total_days - 1, n)
    user_nights = user_nights or {}

    fixed, flexible = {}, []
    for c in cities:
        if user_nights.get(c) is not None:
            fixed[c] = max(1, user_nights[c])
        else:
            flexible.append(c)

    used = sum(fixed.values())
    left = max(nights_total - used, len(flexible))
    if not flexible:
        # 用户把每城的晚数都指定了，但总和少于「总天数 - 1」——
        # 多出来的夜晚补给景点最多的城市（按密度挑），保证总天数不缩水
        if left > 0 and cities:
            density_map = density or {}
            target = max(cities, key=lambda c: max(density_map.get(c, 1), 1))
            fixed[target] = fixed.get(target, 1) + left
        return [(c, fixed[c]) for c in cities]

    weights = [max((density or {}).get(c, 1), 1) for c in flexible]
    sumw = sum(weights)
    alloc = {c: max(1, round(left * w / sumw)) for c, w in zip(flexible, weights)}
    diff = left - sum(alloc.values())
    i = 0
    while diff != 0 and alloc:
        k = list(alloc.keys())[i % len(alloc)]
        if diff > 0:
            alloc[k] += 1; diff -= 1
        elif alloc[k] > 1:
            alloc[k] -= 1; diff += 1
        i += 1
    return [(c, fixed.get(c, alloc.get(c, 1))) for c in cities]
