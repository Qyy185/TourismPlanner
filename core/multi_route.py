# -*- coding: utf-8 -*-
"""
多目的地路线编排模块

解决导师反馈的核心问题：单目的地场景下「优化」功能发挥空间小，
需要支持「10 天去黑龙江玩，自然风光为主」这类多城市串线需求。

设计原则（与项目既有风格一致）：
  · **规则算编排，模型填内容**。距离、时长、天数分配是「可计算的确定性问题」，
    交给规则才能保证每条建议都能追溯依据、每次结果一致；
    模型只负责填充每个城市的景点、美食、住宿描述。
  · **不编造**。编排只使用知识库里查得到的事实（城际连接表、路线库）；
    知识库里没有的城市会被明确标出，而不是硬凑一个。

输入（用户表单）
    destinations  多目的地字符串，支持三种写法：
                   「哈尔滨, 齐齐哈尔, 五大连池」   仅列城市
                   「哈尔滨2晚, 齐齐哈尔1晚, 五大连池2晚」  指定停留
                   「哈尔滨 → 齐齐哈尔 → 五大连池」  箭头链
    days          总天数
    interests     兴趣偏好
    budget        人均预算
    people        人数
    origin        出发地（可选）

输出
    逐日骨架：每天在哪座城、这一天怎么安排、城际怎么走、依据是什么
    + 编排依据说明（供 Prompt 引用，保证模型不推翻规则结论）
"""
import io, os, re, sys, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.transport import _CITY, resolve_place
from core.intercity import conn, fmt_h, price_label
from core.rail_network import transfer_hub
from core.route_order import order_cities as route_order
from core.route_days import build_days, allocate_nights as _alloc_nights

# 兴趣标签 → 匹配用关键词（用于在路线库里找匹配的主线）
_INTEREST_HINTS = {
    '自然风光': ('自然', '避暑', '火山', '湿地', '山水', '峡谷', '草原', '湖', '森林'),
    '历史文化': ('历史', '古', '寺', '博物', '故居', '遗迹', '文化', '人文'),
    '美食探店': ('美食', '小吃', '老字号', '夜市', '饮食'),
    '购物娱乐': ('购物', '商业', '娱乐', '夜生活', '特产'),
    '亲子游': ('亲子', '博物馆', '乐园', '科普'),
    '户外探险': ('徒步', '登山', '滑雪', '漂流', '越野', '探险'),
}


class MultiRouteError(Exception):
    """多目的地输入无法解析"""


# ------------------------------------------------------------------
# 1. 解析多目的地输入
# ------------------------------------------------------------------
def parse_destinations(text):
    """解析多目的地字符串。

    返回 [(城市名, 指定晚数 or None), ...]，保持用户输入顺序。
    支持写法：
      「哈尔滨, 齐齐哈尔, 五大连池」
      「哈尔滨2晚, 齐齐哈尔1晚」
      「哈尔滨 → 齐齐哈尔 → 五大连池」
      「哈尔滨 齐齐哈尔 五大连池」（空格分隔）
    """
    if not text:
        return []
    s = str(text).strip()
    # 统一分隔符
    s = re.sub(r'[→>＞、]', ',', s)
    s = re.sub(r'\s+', ' ', s)

    parts = [p.strip() for p in s.split(',') if p.strip()]
    if len(parts) == 1:
        # 空格分隔（但要避免把「哈尔滨2晚」拆开）
        parts = [p for p in re.split(r'\s+', parts[0]) if p]

    out = []
    for p in parts:
        nights = None
        m = re.search(r'(\d+)\s*[晚天日]', p)
        name = p
        if m:
            nights = int(m.group(1))
            name = (p[:m.start()] + p[m.end():]).strip()
        # 去掉常见修饰
        name = re.sub(r'^(去|到|游|游玩|想去)', '', name).strip()
        if not name:
            continue
        out.append((name, nights))
    return out


def resolve_destinations(text):
    """解析并把地名解析成标准城市名。

    返回 (resolved, unresolved)：
      resolved   [(城市名, 指定晚数 or None), ...]
      unresolved [(原始输入, None), ...]  解析不出来的，知识库里没有
    """
    items = parse_destinations(text)
    resolved, unresolved = [], []
    for name, nights in items:
        r = resolve_place(name)
        if r:
            resolved.append((r[0], nights))
        else:
            unresolved.append((name, None))
    return resolved, unresolved


# ------------------------------------------------------------------
# 2. 天数分配
# ------------------------------------------------------------------
def allocate_nights(cities, total_days, user_nights=None, spot_density=None):
    """分配各城住宿夜数（口径见 core.route_days.allocate_nights）"""
    return _alloc_nights(cities, total_days, user_nights, spot_density)


# ------------------------------------------------------------------
# 3. 城市顺序（不绕路）
# ------------------------------------------------------------------
def order_cities(cities, fixed_order=False):
    """按地理邻近排一条不绕路的顺序（见 core.route_order）。

    fixed_order 为 True 时保持用户输入顺序（用户已经想好了路线）。
    """
    if fixed_order:
        return list(cities)
    return route_order(cities)


# ------------------------------------------------------------------
# 4. 知识库检索：找匹配的主线
# ------------------------------------------------------------------
def find_route_library(prov, interests=None, days=None):
    """在路线库文件里找与「省份 + 兴趣 + 天数」最匹配的主线标题。

    返回 (文件名, 匹配到的标题 or None)
    """
    KB = os.path.join(ROOT, 'knowledge_base', 'destinations')
    cands = []
    for d in os.listdir(KB):
        sub = os.path.join(KB, d)
        if not os.path.isdir(sub):
            continue
        for f in os.listdir(sub):
            if '路线库' in f and f.endswith('.md'):
                if prov in f or (d == '省' and f.split('—')[1] == prov):
                    cands.append(os.path.join(sub, f))
    if not cands:
        return None, None
    path = sorted(cands)[0]
    try:
        txt = io.open(path, encoding='utf-8').read()
    except OSError:
        return path, None

    hints = []
    for it in (interests or []):
        hints.extend(_INTEREST_HINTS.get(it, ()))

    best, best_score = None, 0
    for line in txt.split('\n'):
        if not line.startswith('### '):
            continue
        title = line[4:].strip()
        score = 0
        for h in hints:
            if h in title:
                score += 2
        if days:
            m = re.match(r'(\d+)\s*日', title)
            if m and abs(int(m.group(1)) - days) <= 1:
                score += 1
        if '主线' in title:
            score += 1
        if score > best_score:
            best, best_score = title, score
    return path, best


# ------------------------------------------------------------------
# 5. 主入口：生成逐日骨架
# ------------------------------------------------------------------
def build_multi_route(destination_text, days, interests=None, budget=None,
                      people=1, origin='', spot_density=None,
                      keep_user_order=False):
    """生成多目的地逐日路线骨架。

    返回 dict：
      ok           是否成功
      cities       [(城市, 晚数), ...]  编排后的城市与停留
      days         逐日安排列表
      unresolved   解析不出来的地名
      notes        编排依据说明（供 Prompt 引用）
      legs         城际段信息
      transit_h    总路上时间
      transit_pct  路上时间占总时长比例
    """
    resolved, unresolved = resolve_destinations(destination_text)
    if not resolved:
        return {'ok': False, 'error': '未能识别任何目的地',
                'unresolved': [n for n, _ in unresolved], 'cities': [], 'days': []}

    # 去重保序
    seen = set()
    cities_in = []
    user_nights = {}
    for name, nights in resolved:
        if name in seen:
            continue
        seen.add(name)
        cities_in.append(name)
        if nights is not None:
            user_nights[name] = max(1, nights - 1)   # 用户说的「N晚」= N 晚住宿

    total_days = int(days or 3)
    if total_days < 2:
        total_days = 2

    ordered = order_cities(cities_in, fixed_order=keep_user_order)
    alloc = allocate_nights(ordered, total_days, user_nights, spot_density)

    # 逐日骨架（统一走 core.route_days，天数口径一致）
    day_list, legs, actual_days = build_days(alloc)

    transit_h = sum(l[2]['hours'] for l in legs)
    notes = [
        '**路线已按规则编排，请严格遵循，不要自行更改城市顺序与停留天数：**',
        '- 城市顺序：%s（按地理邻近排线，避免来回折返）' % ' → '.join(c for c, _ in alloc),
        '- 停留夜数：%s' % '、'.join('%s %d 晚' % (c, n) for c, n in alloc),
        '- 总行程：%d 天 %d 晚' % (actual_days, sum(n for _, n in alloc)),
    ]
    if legs:
        notes.append('- 城际交通（共 %d 段，路上时间约 %s，占总时长约 %d%%）：'
                     % (len(legs), fmt_h(transit_h), round(transit_h / 24 / actual_days * 100)))
        for frm, to, c in legs:
            notes.append('  - %s → %s：%s，约 %d km，%s，%s'
                         % (frm, to, c['mode_name'], c['km'], fmt_h(c['hours']),
                            c['note']))
    if unresolved:
        notes.append('- ⚠️ 以下地名在知识库中没有对应资料，未纳入编排：%s'
                     % '、'.join(n for n, _ in unresolved))
    notes.append('- 每天仍需保留「🕐 机动时段」与「☔ 备选」，末尾保留「🛡 弹性保障」小节。')
    notes.append('- 跨城当天要写清：早上几点出发、在哪座城市换乘、大约几点到下一座城市。')

    return {
        'ok': True,
        'cities': alloc,
        'days': day_list,
        'actual_days': actual_days,
        'unresolved': [n for n, _ in unresolved],
        'notes': '\n'.join(notes),
        'legs': legs,
        'transit_h': transit_h,
        'transit_pct': round(transit_h / 24 / actual_days * 100) if actual_days else 0,
    }
