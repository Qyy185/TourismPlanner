# -*- coding: utf-8 -*-
"""
把「按地理走向定起点」的排序逻辑抽成公共函数，
供省内跨城路线、跨省路线、多目的地编排三处共用。
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.transport import _CITY, _haversine_km


def order_cities(cities):
    """排一条不绕路的城市顺序。

    做法：
      1. 以「最西（或最南）」的城市为起点，避免最近邻从任意点出发造成回头路
         （例如从长春出发先去沈阳、再折回北边的延吉）；
      2. 最近邻逐步扩展；
      3. 2-opt 优化消除交叉（首尾固定，允许反转中间段）。

    没有坐标的城市（台中、高雄等）按输入顺序接在末尾，不参与距离计算。
    """
    if len(cities) <= 2:
        return list(cities)

    def d(a, b):
        if a not in _CITY or b not in _CITY:
            return 0.0
        return _haversine_km((_CITY[a][0], _CITY[a][1]), (_CITY[b][0], _CITY[b][1]))

    known = [c for c in cities if c in _CITY]
    unknown = [c for c in cities if c not in _CITY]
    if len(known) <= 2:
        return list(cities)

    # 起点取地理上的一端：优先最西，同经度取最南
    start = min(known, key=lambda c: (_CITY[c][1], _CITY[c][0]))
    rest = [c for c in known if c != start]
    path = [start]
    while rest:
        nxt = min(rest, key=lambda c: d(path[-1], c))
        rest.remove(nxt)
        path.append(nxt)

    def total(p):
        return sum(d(p[i], p[i + 1]) for i in range(len(p) - 1))

    improved = True
    while improved:
        improved = False
        for i in range(len(path) - 2):
            for j in range(i + 2, len(path)):
                cand = path[:i] + path[i:j + 1][::-1] + path[j + 1:]
                if total(cand) < total(path) - 1:
                    path = cand
                    improved = True
    return path + unknown
