# -*- coding: utf-8 -*-
"""素材盘点：每省能从「主攻略 + 路线库」里挖出多少个景点条目"""
import io, os, re, glob, collections, sys

ROOT = r'D:\毕业\智慧旅游规划助手'
sys.path.insert(0, ROOT)
from core.transport import _CITY, _PLACE_ALIAS

KB = os.path.join(ROOT, 'knowledge_base', 'destinations')

# 真实地名全集（坐标库 + 别名表 + 反向）
NAMES = set(_CITY.keys()) | set(_PLACE_ALIAS.keys())
# 去掉纯城市名（城市本身不是一个景点）
CITY_ONLY = set(_CITY.keys())


def load_prov_files(prov):
    guide = None
    route = None
    for f in glob.glob(os.path.join(KB, '*', '*%s*主攻略*.md' % prov)):
        guide = f
    for f in glob.glob(os.path.join(KB, '*', '*%s*路线库*.md' % prov)):
        route = f
    return guide, route


def structured_spots(guide):
    """主攻略里已结构化的景点 → {标题: 全文}"""
    out = {}
    t = io.open(guide, encoding='utf-8').read()
    m = re.search(r'^##\s*热门景点\s*\n(.*?)(?=\n##\s|\Z)', t, re.S | re.M)
    if not m:
        return out
    for blk in re.split(r'\n(?=###\s)', m.group(1)):
        lines = blk.split('\n')
        hm = re.match(r'###\s*(.+?)\s*$', lines[0])
        if not hm:
            continue
        out[hm.group(1)] = '\n'.join(lines[1:]).strip()
    return out


def extract_from_route(route, prov):
    """从路线库挖景点候选：返回 [(景点名, 描述, 核心看点, 最佳季节, 实用提示)]"""
    if not route:
        return []
    t = io.open(route, encoding='utf-8').read()
    out = []
    # 切出每个 ### 小节
    for sec in re.split(r'\n(?=###\s)', t):
        head = sec.split('\n')[0]
        if not head.startswith('### '):
            continue
        title = head[4:].strip()
        # 小节内的公共字段
        core = ''
        season = ''
        tips = ''
        m = re.search(r'\*\*核心看点\*\*[：:]\s*(.+)', sec)
        if m:
            core = m.group(1).strip()
        m = re.search(r'\*\*最佳季节\*\*[：:]\s*(.+)', sec)
        if m:
            season = m.group(1).strip()
        m = re.search(r'\*\*实用提示\*\*[：:]\s*(.+)', sec)
        if m:
            tips = m.group(1).strip()

        # 逐条找景点提及
        for line in sec.split('\n'):
            line = line.strip()
            if not line:
                continue
            # 去掉 Markdown 标记
            plain = re.sub(r'\*\*', '', line)
            # 找所有真实地名
            hits = []
            for nm in NAMES:
                if nm in plain and nm not in hits:
                    hits.append(nm)
            # 按出现位置排序
            hits.sort(key=lambda x: plain.find(x))
            if not hits:
                continue
            # 过滤掉明显是「城市/省」而不是景点的
            keep = []
            for nm in hits:
                # 「XX（城市）」里的城市不算景点
                if re.search(r'（[^）]*%s[^）]*）' % re.escape(nm), plain) and nm in CITY_ONLY:
                    continue
                keep.append(nm)
            if not keep:
                continue
            desc = re.sub(r'^[\s\*#|｜、,，。-]+', '', plain)
            for nm in keep[:2]:     # 每行最多取 2 个，避免噪声
                out.append({
                    'name': nm, 'desc': desc[:220],
                    'core': core, 'season': season, 'tips': tips,
                    'src': '路线库：%s' % title,
                })
    return out


def main():
    provs = sorted(set(os.path.basename(f).split('—')[1]
                       for f in glob.glob(os.path.join(KB, '*', '*主攻略*.md'))))
    print('=' * 76)
    print('每省素材盘点：已有结构化景点 + 路线库可挖景点')
    print('=' * 76)
    print('%-10s %10s %12s %10s' % ('省份', '已结构化', '路线库可挖', '去重后'))
    print('-' * 76)
    tot_s = tot_r = tot_u = 0
    rows = []
    for prov in provs:
        guide, route = load_prov_files(prov)
        if not guide:
            continue
        s = structured_spots(guide)
        r = extract_from_route(route, prov)
        # 去重：与已结构化重叠的算已覆盖
        rnames = set(x['name'] for x in r)
        new = [x for x in r if not any(x['name'] in t or t in x['name'] for t in s)]
        uniq = len(set(x['name'] for x in new))
        tot_s += len(s); tot_r += len(r); tot_u += uniq
        rows.append((prov, len(s), len(r), uniq))
    for p, a, b, c in rows:
        print('%-10s %10d %12d %10d' % (p, a, b, c))
    print('-' * 76)
    print('%-10s %10d %12d %10d' % ('合计', tot_s, tot_r, tot_u))
    print()
    print('→ 路线库去重后可补充 %d 个景点（不含手写）' % tot_u)


if __name__ == '__main__':
    main()
