"""行李清单生成（纯规则 · 离线 · 可解释）

设计取舍：不调模型——行李清单是典型的「规则胜过 AI」场景：
按目的地气候（纬度 + 当前月份）、行程天数、兴趣偏好三条规则就能生成一份
可解释、秒出、零成本的清单；AI 反而会每次生成得五花八门。
"""

import datetime
import re

from core.transport import resolve_place, _CITY

# ---------------------------------------------------------------------------
# 分层清单
# ---------------------------------------------------------------------------
_BASE = [  # 任何旅行都要带
    "身份证等证件（取票、入住、景区实名都要用）",
    "手机 + 充电器 + 充电宝",
    "常用药：肠胃药、感冒药、创可贴",
    "舒适的运动鞋（日均步行 1 万步起）",
    "折叠伞 / 轻便雨衣",
]

# 特殊目的地 → 追加项（按城市 / 省匹配）
_BEACH_CITIES = {"三亚", "海口", "琼海", "万宁", "北海", "厦门", "珠海", "湛江", "舟山", "台州"}
_PLATEAU_CITIES = {"拉萨", "林芝", "日喀则", "阿里", "昌都", "香格里拉", "稻城", "康定", "玉树", "格尔木", "西宁"}
_COLD_CITIES = {"漠河", "哈尔滨", "长春", "乌鲁木齐", "呼伦贝尔", "海拉尔", "延吉", "长白山", "齐齐哈尔"}

_BEACH = ["泳衣 / 沙滩鞋", "防晒霜（SPF50+）、遮阳帽、墨镜"]
_PLATEAU = [
    "红景天等抗高反药品（提前一周开始服）",
    "防晒霜 + 墨镜（高原紫外线极强）",
    "保暖分层穿着（高原昼夜温差大）",
]
_COLD_WINTER = ["羽绒服 / 厚外套", "雪地靴、手套、围巾", "暖宝宝"]

# 兴趣偏好 → 追加项
_INTEREST = {
    "户外探险": ["登山鞋 / 双肩包", "速干衣", "驱蚊液"],
    "亲子游": ["孩子的常用药与零食", "安抚小玩具 / 绘本"],
    "美食探店": ["健胃消食片（试吃清单会长）"],
    "自然风光": ["驱蚊液", "防晒霜"],
    "购物娱乐": ["可折叠购物袋", "预留行李箱空间"],
}


def _season(month):
    if month in (12, 1, 2):
        return "winter"
    if month in (6, 7, 8):
        return "summer"
    return "mild"


def build_packing(destination, days, interests, people=1):
    """生成行李清单。

    返回 dict：
      ok=False 时带 reason（目的地识别不了 / 没填目的地）；
      ok=True 时 groups=[(组名, [条目...])]，note=补充说明。
    """
    days = max(1, int(days or 1))
    people = max(1, int(people or 1))
    d_raw = str(destination or "").strip()
    if not d_raw:
        return {"ok": False, "reason": "missing"}

    r = resolve_place(d_raw)
    if not r:
        return {"ok": False, "reason": "unresolved", "dest": d_raw}
    city, prov = r

    month = datetime.date.today().month
    season = _season(month)
    lat = _CITY.get(city, (30.0,))[0]

    basics = list(_BASE)
    # 衣物数量按天数给：天数 +1 套，封顶 7 套（可洗衣就少带）
    n_clothes = min(days + 1, 7)
    basics.insert(2, "换洗衣物 × %d 套（%d 天行程）" % (n_clothes, days))

    dest_items = []
    if season == "winter":
        if lat >= 30 or city in _COLD_CITIES:
            dest_items += ["羽绒服 / 厚外套", "保暖内衣"]
        else:
            dest_items += ["厚外套 / 薄羽绒", "长袖衣物"]
        if city in _COLD_CITIES or lat >= 45:
            dest_items += _COLD_WINTER
    elif season == "summer":
        dest_items += ["透气夏装", "防晒霜、遮阳帽"]
    else:  # 春秋
        if lat >= 40:
            dest_items += ["厚外套（早晚温差大）", "长袖 + 薄毛衣"]
        else:
            dest_items += ["长袖 + 薄外套"]

    if city in _BEACH_CITIES or prov == "海南":
        dest_items += _BEACH
    if city in _PLATEAU_CITIES or prov in {"西藏", "青海"}:
        dest_items += _PLATEAU

    interest_items = []
    for it in (interests or []):
        interest_items += _INTEREST.get(str(it), [])

    # 去重、保序
    def dedup(seq):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    groups = [("🎒 人手必备", dedup(basics))]
    if dest_items:
        groups.append(("📍 %s 特别提醒" % city, dedup(dest_items)))
    if interest_items:
        groups.append(("🎯 按你的兴趣", dedup(interest_items)))

    note = "按 %d 天 · %d 人估算；多人同行时常用药与充电宝按人数备份。" % (days, people)
    if people > 1:
        note += "住宿若拼房，洗漱用品可以少带一份。"
    if season == "winter" and city in _COLD_CITIES:
        note += "目的地冬季严寒，电子产品（手机/充电宝）低温掉电快，贴身放保暖。"
    return {"ok": True, "city": city, "prov": prov, "groups": groups, "note": note}


if __name__ == "__main__":        # 自测：python -m core.packing
    for d, n, its in [
        ("三亚", 4, ["自然风光", "美食探店"]),
        ("哈尔滨", 3, ["历史文化"]),
        ("拉萨", 6, ["自然风光"]),
        ("成都", 3, ["美食探店", "户外探险"]),
        ("火星", 3, []),
        ("", 3, []),
    ]:
        info = build_packing(d, n, its)
        print("-" * 60)
        print(d or "(空)", "->", info.get("reason", "ok"))
        if info.get("ok"):
            for g, items in info["groups"]:
                print("  %s: %s" % (g, "；".join(items)))
            print("  note:", info["note"])
