# -*- coding: utf-8 -*-
"""
方案微调（对应导师反馈第 4 点「给出初步方案后可以微调」）

提供三个**受约束**的局部重算操作：
  1. 换这天      swap_day      替换某一天所在的城市（或该城的景点）
  2. 调整停留    adjust_nights 某城多留 / 少留 N 晚，触发天数重分配
  3. 重排顺序    reorder       按地理邻近重排（去掉回头路）

设计原则（与编排模块一致）：
  · **局部重算，不是模型自由发挥**。微调只改结构（顺序、天数、城市），
    景点、美食、住宿的描述仍由模型按新结构重新填。
  · 每次微调都返回**变更说明 + 依据**，用户能看懂改了什么、为什么。
  · 距离与时长仍由规则算，微调不会引入模型臆测的交通数据。
"""
import os, sys, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.transport import _CITY, resolve_place
from core.intercity import conn, fmt_h, price_label
from core.route_order import order_cities


class AdjustError(Exception):
    """微调请求无法执行"""


# ------------------------------------------------------------------
# 内部：把「城市 + 晚数」变成逐日骨架
# ------------------------------------------------------------------
def _build_days(alloc):
    """alloc: [(city, nights), ...] → 逐日骨架列表"""
    day_list = []
    day = 1
    legs = []
    for i, (city, nights) in enumerate(alloc):
        if i > 0:
            prev = alloc[i - 1][0]
            c = conn(prev, city)
            legs.append((prev, city, c))
            day_list.append({
                'day': day, 'type': 'transfer', 'from': prev, 'to': city,
                'mode': c['mode_name'], 'km': c['km'],
                'hours': c['hours'], 'hours_text': fmt_h(c['hours']),
                'price': price_label(c['price']), 'note': c['note'],
                'legs': c.get('legs', []),
                'brief': '%s → %s，%s，约 %d km，%s，参考票价 %s；'
                         '抵达后安排半天游览%s，傍晚入住' % (
                             prev, city, c['mode_name'], c['km'], fmt_h(c['hours']),
                             price_label(c['price']), city),
            })
        else:
            day_list.append({
                'day': day, 'type': 'arrive', 'city': city,
                'brief': '抵达%s后安排半天慢游，首日不排满，留出安顿时间' % city,
            })
        day += 1
        for k in range(max(0, nights - 1)):
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


def _notes(alloc, days, legs, transit_h, changes):
    lines = ['**路线已按规则编排，请严格遵循，不要自行更改城市顺序与停留天数：**']
    if changes:
        lines.append('')
        lines.append('**本次微调**：')
        for ch in changes:
            lines.append('- %s' % ch)
        lines.append('')
    lines.append('- 城市顺序：%s（按地理邻近排线，避免来回折返）' % ' → '.join(c for c, _ in alloc))
    lines.append('- 停留夜数：%s' % '、'.join('%s %d 晚' % (c, n) for c, n in alloc))
    lines.append('- 总行程：%d 天 %d 晚' % (days, sum(n for _, n in alloc)))
    if legs:
        lines.append('- 城际交通（共 %d 段，路上时间约 %s，占总时长约 %d%%）：'
                     % (len(legs), fmt_h(transit_h), round(transit_h / 24 / days * 100)))
        for frm, to, c in legs:
            lines.append('  - %s → %s：%s，约 %d km，%s，%s'
                         % (frm, to, c['mode_name'], c['km'], fmt_h(c['hours']), c['note']))
    lines.append('- 每天仍需保留「🕐 机动时段」与「☔ 备选」，末尾保留「🛡 弹性保障」小节。')
    lines.append('- 跨城当天要写清：早上几点出发、在哪座城市换乘、大约几点到下一座城市。')
    return '\n'.join(lines)


def _result(alloc, changes):
    day_list, legs, days = _build_days(alloc)
    transit_h = sum(l[2]['hours'] for l in legs)
    return {
        'ok': True,
        'cities': alloc,
        'days': day_list,
        'actual_days': days,
        'changes': changes,
        'notes': _notes(alloc, days, legs, transit_h, changes),
        'legs': legs,
        'transit_h': transit_h,
        'transit_pct': round(transit_h / 24 / days * 100) if days else 0,
    }


# ------------------------------------------------------------------
# 1. 换这天
# ------------------------------------------------------------------
def swap_day(alloc, day_no, new_place):
    """把第 day_no 天改到 new_place（自动调整停留夜数）。

    new_place 可以是城市名或景点名（会先解析到城市）。
    规则：
      · 目标城市若已在路线里 → 增加它 1 晚，原城市减 1 晚（换城市）
      · 目标城市不在路线里 → 从原城市挪 1 晚过来（保持总天数不变）
    """
    r = resolve_place(new_place)
    if not r:
        raise AdjustError('知识库里没有「%s」这个地方，换个城市试试' % new_place)
    target = r[0]

    # 找到第 day_no 天落在哪个城市
    day_list, _, _ = _build_days(alloc)
    target_day = None
    for d in day_list:
        if d['day'] == day_no:
            target_day = d
            break
    if target_day is None:
        raise AdjustError('没有第 %d 天（当前行程共 %d 天）' % (day_no, len(day_list)))
    old_city = target_day.get('city') or target_day.get('to')
    if not old_city:
        raise AdjustError('第 %d 天是返程日，换不了目的地' % day_no)

    cities = [c for c, _ in alloc]
    nights = dict(alloc)
    if target in cities and target != old_city:
        # 已在路线里：它 +1 晚，原城市 -1 晚
        nights[target] += 1
        nights[old_city] = max(1, nights[old_city] - 1)
        change = '第 %d 天从「%s」改为「%s」；%s 调整为 %d 晚，%s 调整为 %d 晚' % (
            day_no, old_city, target, target, nights[target], old_city, nights[old_city])
    else:
        # 不在路线里：从原城市挪一晚
        if nights[old_city] <= 1:
            raise AdjustError('「%s」只有 1 晚，再挪就没有了；换个方式或先加天数' % old_city)
        nights[old_city] -= 1
        nights[target] = 1
        # 插到原城市后面（相邻，减少折返）
        idx = [c for c, _ in alloc].index(old_city)
        new_alloc = []
        for i, (c, n) in enumerate(alloc):
            new_alloc.append((c, nights.get(c, n)))
            if i == idx:
                new_alloc.append((target, 1))
        # 去掉原有的 target（如果有）
        seen = set()
        dedup = []
        for c, n in new_alloc:
            if c in seen:
                continue
            seen.add(c)
            dedup.append((c, n))
        new_alloc = dedup
        change = '第 %d 天从「%s」改为「%s」；%s 调整为 %d 晚，新增 %s 1 晚' % (
            day_no, old_city, target, old_city, nights[old_city], target)
        return _result(new_alloc, [change])

    new_alloc = [(c, nights.get(c, n)) for c, n in alloc]
    return _result(new_alloc, [change])


# ------------------------------------------------------------------
# 2. 调整停留天数
# ------------------------------------------------------------------
def adjust_nights(alloc, city, delta):
    """某城多留（delta>0）或少留（delta<0）N 晚。

    总天数变化 = delta，由该城自己承担（其它城不动）。
    """
    if not city:
        raise AdjustError('要调整哪个城市？')
    r = resolve_place(city)
    if not r:
        raise AdjustError('知识库里没有「%s」这个地方' % city)
    target = r[0]
    names = [c for c, _ in alloc]
    if target not in names:
        raise AdjustError('当前路线里没有「%s」，可选：%s' % (target, '、'.join(names)))

    delta = int(delta)
    if delta == 0:
        raise AdjustError('要调整多少晚？填个非 0 的数字')

    new_alloc = []
    for c, n in alloc:
        if c == target:
            nn = n + delta
            if nn < 1:
                raise AdjustError('「%s」至少要留 1 晚' % target)
            new_alloc.append((c, nn))
        else:
            new_alloc.append((c, n))
    change = '「%s」停留由 %d 晚调整为 %d 晚（%+d 晚），总行程相应变化' % (
        target, dict(alloc)[target], dict(alloc)[target] + delta, delta)
    return _result(new_alloc, [change])


# ------------------------------------------------------------------
# 3. 重排顺序
# ------------------------------------------------------------------
def reorder(alloc):
    """按地理邻近重排城市顺序，去掉回头路。"""
    cities = [c for c, _ in alloc]
    new_order = order_cities(cities)
    if new_order == cities:
        return _result(alloc, ['当前顺序已经是不绕路的地理顺序，无需调整'])
    nights = dict(alloc)
    new_alloc = [(c, nights[c]) for c in new_order]
    change = '城市顺序由「%s」重排为「%s」，消除折返' % (
        ' → '.join(cities), ' → '.join(new_order))
    return _result(new_alloc, [change])


# ------------------------------------------------------------------
# 统一入口
# ------------------------------------------------------------------
def apply_adjust(alloc, action, **kwargs):
    """按操作类型执行微调。

    action: 'swap' | 'nights' | 'reorder'
    """
    if action == 'swap':
        return swap_day(alloc, kwargs['day'], kwargs['place'])
    if action == 'nights':
        return adjust_nights(alloc, kwargs['city'], kwargs['delta'])
    if action == 'reorder':
        return reorder(alloc)
    raise AdjustError('未知的微调操作：%s' % action)
