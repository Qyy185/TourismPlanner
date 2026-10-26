# -*- coding: utf-8 -*-
"""
知识库平铺生成器 · 第一步（v4）：城市总览

v3 遗留问题：9 个省的主攻略用「首府」而非「省会」，或只写「行政区类别：省」，
导致省会解析成省名。v4 增加：
  · 兼容「首府」「行政中心」等字段名
  · 省会兜底表（取自 transport._PROVINCE_CAPITAL）
  · 台湾/香港/澳门按直辖市处理（本身就是城市）

城市归属优先级：括号标注 > 标题含城市 > 景点名即地名 > 标题含别名 > 兜底省会
"""
import io, os, re, glob, collections, sys

ROOT = r'D:\毕业\智慧旅游规划助手'
sys.path.insert(0, ROOT)
from core.transport import _CITY, _PLACE_ALIAS, _PROVINCE_CAPITAL

KB = os.path.join(ROOT, 'knowledge_base', 'destinations')
OUT = os.path.join(KB, '_generated')
VERSION = 'v1.0'
DATE = '20261025'

_SPOT_TO_CITY = {}
for _c in _CITY:
    _SPOT_TO_CITY[_c] = _c
for _a, _c in _PLACE_ALIAS.items():
    _SPOT_TO_CITY.setdefault(_a, _c)

_NOT_CITY = {'历史城区', '核心', '必尝', '最', '必', '推荐', '优选', '精华',
             '经典', '枢纽', '主打', '首选', '妈阁一带', '核心景区', '核心区'}

# 直辖市 / 特别行政区本身就是城市
_DIRECT = {'北京': '北京', '上海': '上海', '天津': '天津', '重庆': '重庆',
           '香港': '香港', '澳门': '澳门', '台湾': '台北'}


def province_cities(prov):
    return {c for c, v in _CITY.items() if v[2] == prov}


def parse_overview(text):
    overview = {}
    m = re.search(r'^##\s*城市概览\s*\n(.*?)(?=\n##\s|\Z)', text, re.S | re.M)
    if not m:
        return overview
    for line in m.group(1).split('\n'):
        line = line.strip()
        if not line.startswith('-'):
            continue
        parts = list(re.finditer(r'\*\*(.+?)\*\*[：:]\s*', line))
        for i, pm in enumerate(parts):
            key = pm.group(1).strip()
            end = parts[i + 1].start() if i + 1 < len(parts) else len(line)
            val = line[pm.end():end].strip().rstrip('。')
            if key and val:
                overview[key] = val
    return overview


def extract_cities(overview, prov):
    capital = ''
    main_cities = []
    for k, v in overview.items():
        if ('省会' in k or '首府' in k or '行政中心' in k) and '主要' not in k:
            cm = re.match(r'^([^\s，,。、（(]+)', v)
            if cm and cm.group(1) in _CITY:
                capital = cm.group(1)
        if '主要旅行城市' in k or '主要旅游片区' in k or '主要旅行地区' in k:
            raw = re.sub(r'（[^）]*）', '', v)
            raw = re.sub(r'^[^：:]*[：:]\s*', '', raw)
            for c in re.split(r'[、，,]', raw):
                c = c.strip()
                if c and c in _CITY:
                    main_cities.append(c)
    # 兜底
    if not capital:
        capital = _DIRECT.get(prov) or _PROVINCE_CAPITAL.get(prov) or prov
    return capital, main_cities


def guess_city(title, p_cities, capital):
    cm = re.search(r'（([^）]+)）\s*$', title)
    if cm:
        cand = cm.group(1).split('、')[0].split('/')[0].strip()
        if cand in _NOT_CITY:
            return capital, '括号非城市'
        if cand in p_cities:
            return cand, '括号标注'
        if cand in _SPOT_TO_CITY and _CITY.get(_SPOT_TO_CITY[cand], (0, 0, ''))[2] in p_cities:
            return _SPOT_TO_CITY[cand], '括号别名'
        for c in sorted(p_cities, key=len, reverse=True):
            if c in cand:
                return c, '括号含城市'
    else:
        for c in sorted(p_cities, key=len, reverse=True):
            if c in title:
                return c, '标题含城市'

    stem = re.sub(r'(风景区|景区|保护区|度假区|旅游区|国家公园|古城|古镇)$', '', title)
    for c in sorted(p_cities, key=len, reverse=True):
        if stem == c or stem.startswith(c) or c.startswith(stem):
            return c, '景点名即地名'

    for a, c in _SPOT_TO_CITY.items():
        if len(a) >= 2 and a in title and _CITY.get(c, (0, 0, ''))[2] in p_cities:
            return c, '标题含别名'

    return capital, '兜底省会'


def parse_province(path):
    text = io.open(path, encoding='utf-8').read()
    prov = os.path.basename(path).split('—')[1]
    overview = parse_overview(text)
    capital, main_cities = extract_cities(overview, prov)
    p_cities = province_cities(prov)
    if prov in _DIRECT:
        p_cities = p_cities | {capital}

    city_spots = collections.OrderedDict()
    reasons = collections.Counter()
    m = re.search(r'^##\s*热门景点\s*\n(.*?)(?=\n##\s|\Z)', text, re.S | re.M)
    if m:
        for blk in re.split(r'\n(?=###\s)', m.group(1)):
            lines = blk.split('\n')
            hm = re.match(r'###\s*(.+?)\s*$', lines[0])
            if not hm:
                continue
            title = hm.group(1)
            body = '\n'.join(lines[1:]).strip()
            city, why = guess_city(title, p_cities, capital)
            reasons[why] += 1
            city_spots.setdefault(city, []).append((title, body))

    for c in main_cities:
        city_spots.setdefault(c, [])

    others = {}
    for sec in ('特色美食', '住宿建议', '交通指南', '注意事项'):
        mm = re.search(r'^##\s*%s\s*\n(.*?)(?=\n##\s|\Z)' % sec, text, re.S | re.M)
        if mm:
            others[sec] = mm.group(1).strip()

    return prov, overview, city_spots, others, capital, main_cities, reasons


def main():
    os.makedirs(OUT, exist_ok=True)
    for old in glob.glob(os.path.join(OUT, '*.md')):
        os.remove(old)

    report = []
    tot_cities = tot_spots = 0
    tot_reasons = collections.Counter()
    problems = []

    for path in sorted(glob.glob(os.path.join(KB, '*', '*主攻略*.md'))):
        # 省级文件的省名取自文件名第 2 段；市县级独立攻略（如「齐齐哈尔—主攻略」）
        # 直接把文件名里的地名当省名处理，走同一套城市归集逻辑。
        prov, overview, city_spots, others, capital, main_cities, reasons = parse_province(path)
        tot_reasons.update(reasons)
        if capital not in _CITY:
            problems.append('%s: 省会解析为「%s」' % (prov, capital))
        empty = [c for c, sp in city_spots.items() if not sp]
        if empty:
            problems.append('%s: %d 个城市暂无景点 → %s' % (prov, len(empty), '、'.join(empty[:6])))

        lines = ['# %s · 城市总览' % prov, '',
                 '> 本文件由省级主攻略按城市下沉生成，保留原始景点字段（门票 / 开放时间 / 建议游玩 / 交通 / 实用建议）。',
                 '> 与主攻略配套：主攻略讲全省共性（气候、节奏、预算口径），本文件讲各城市具体去哪儿。',
                 '> 与城际连接表配套：本文件讲「在这座城玩什么」，城际连接表讲「怎么从这座城到下一座城」。', '']

        if overview:
            lines += ['## 全省概览', '']
            for k, v in overview.items():
                lines.append('- **%s**：%s' % (k, v))
            lines += ['']

        lines += ['## 城市索引', '']
        for c, sp in city_spots.items():
            lines.append('- **%s**（%d 个景点）' % (c, len(sp)))
        lines += ['']

        for c, sp in city_spots.items():
            lines += ['## %s' % c, '']
            if not sp:
                lines += ['> 本条目由「主要旅行城市」标记生成，景点详情待补充。', '']
                continue
            for title, body in sp:
                lines += ['### %s' % title, '']
                if body:
                    lines += [body, '']

        for sec, body in others.items():
            lines += ['## 全省 · %s' % sec if sec != '注意事项' else '## 全省注意事项', '', body, '']

        fname = '智慧旅游规划助手—%s—城市总览—%s—%s.md' % (prov, VERSION, DATE)
        io.open(os.path.join(OUT, fname), 'w', encoding='utf-8').write('\n'.join(lines))

        nc = len(city_spots)
        ns = sum(len(v) for v in city_spots.values())
        tot_cities += nc
        tot_spots += ns
        report.append((prov, nc, ns, capital))

    print('=' * 76)
    print('城市总览生成完成（v4）')
    print('=' * 76)
    print('%-10s %7s %9s %-10s' % ('省份', '城市数', '景点数', '省会'))
    print('-' * 76)
    for prov, nc, ns, cap in report:
        print('%-10s %7d %9d %-10s' % (prov, nc, ns, cap))
    print('-' * 76)
    print('%-10s %7d %9d' % ('合计', tot_cities, tot_spots))
    print()
    print('判定方式分布:')
    for k, v in tot_reasons.most_common():
        print('   %-12s %d' % (k, v))
    print()
    print('⚠️ 待补内容（这些城市在主攻略里没有景点条目）:')
    for p in problems:
        print('   ', p)
    print()
    print('输出:', OUT, '(%d 个文件)' % len(glob.glob(os.path.join(OUT, '*.md'))))


if __name__ == '__main__':
    main()
