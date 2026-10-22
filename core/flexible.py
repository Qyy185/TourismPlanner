"""行程弹性保障（纯规则 · 离线 · 可解释）

设计取舍：不调模型。弹性保障是典型的「经验 + 气候常识」活——
什么季节该防什么天气、每天该留多少机动、赶不上先舍什么，
都有稳定的行业经验可循，规则引擎秒出、零成本，而且每条建议都能追溯到依据
（月份、纬度、沿海与否、景点类型、兴趣偏好）。交给 AI 反而每次说法不一。

输出三大块，正好对应「行程不能排死」的三个层面：
1. buffer   每日留白 —— 每天留多少机动时间（按天数与行程强度）
2. weather  天气风险 —— 按目的地气候规律与当前月份，可能被迫改行程的风险点
3. plans    应变预案 —— 赶不上计划的舍弃顺序 / 雨天室内替代 / 出发前要确认的事

注意口径：这里给的是**气候规律**（长期统计意义上的「几月容易下什么」），
不是实时预报，所以文案里明确写「以出发前预报为准」，避免让用户误当天气预报。
"""

import datetime

from core.transport import _CITY, resolve_place

# ---------------------------------------------------------------------------
# 地区桶：用于把「省份 / 城市」映射到气候风险类型
# ---------------------------------------------------------------------------
_COASTAL_PROVS = {
    "海南", "广东", "广西", "福建", "浙江", "上海", "江苏", "山东",
    "辽宁", "天津", "台湾", "香港", "澳门",
}
# 面对台风最直接的几个省（内陆省份即使靠海也不列）
_TYPHOON_PROVS = {"海南", "广东", "广西", "福建", "浙江", "上海", "江苏", "台湾", "香港", "澳门"}

_SOUTHWEST_PROVS = {"四川", "重庆", "贵州", "云南"}
_SOUTHWEST_HILL_CITIES = {"丽江", "大理", "昆明", "贵阳", "成都", "重庆", "西昌", "六盘水"}

_PLUM_PROVS = {"上海", "江苏", "浙江", "安徽", "湖北", "湖南", "江西"}

_HEAT_CITIES = {"重庆", "武汉", "南京", "南昌", "长沙", "杭州", "福州", "合肥", "西安", "济南", "郑州"}

_NORTH_PROVS = {"北京", "天津", "河北", "山西", "山东", "河南"}

_NORTHWEST_PROVS = {"甘肃", "宁夏", "内蒙古", "新疆", "青海"}
_NORTHWEST_CITIES = {"敦煌", "兰州", "银川", "呼和浩特", "乌鲁木齐", "喀什", "西宁", "嘉峪关", "张掖"}

_SOUTH_DAMP_PROVS = {"广东", "广西", "海南", "福建"}

_HAZE_PROVS = {"北京", "天津", "河北", "河南", "山东", "山西", "陕西"}

_PLATEAU_PROVS = {"西藏", "青海"}
_PLATEAU_CITIES = {
    "拉萨", "林芝", "日喀则", "阿里", "昌都", "香格里拉", "稻城", "康定",
    "理塘", "玉树", "格尔木", "西宁", "中甸",
}

_COLD_CITIES = {
    "漠河", "哈尔滨", "长春", "乌鲁木齐", "呼伦贝尔", "海拉尔", "延吉",
    "长白山", "齐齐哈尔", "沈阳", "吉林", "牡丹江",
}

# 风险等级 → 排序权重与配色（配色在 app_gradio 里用）
_LEVEL_WEIGHT = {"high": 0, "mid": 1, "low": 2}


# ---------------------------------------------------------------------------
# 一、天气风险规则表
#    每条：match(city, prov, lat) 判定适用；months 为空表示全年适用
# ---------------------------------------------------------------------------
_RULES = [
    {
        "key": "typhoon",
        "match": lambda c, p, lat: p in _TYPHOON_PROVS or c in {"三亚", "海口", "厦门", "珠海", "深圳", "北海", "舟山", "温州", "汕头", "湛江"},
        "months": (6, 7, 8, 9, 10),
        "level": "high",
        "title": "台风季（6~10 月）",
        "detail": "沿海地区可能遇台风预警，航班、轮渡、海岛与跨海索道会临时停运。"
                  "① 行程首尾各留半天缓冲，别把返程排成「最后一刻到机场」；"
                  "② 出发前 48 小时盯一次气象预警，买含「行程取消」的旅行险；"
                  "③ 备一条内陆备份路线（台风天海边全面封闭时用）。",
    },
    {
        "key": "plateau",
        "match": lambda c, p, lat: p in _PLATEAU_PROVS or c in _PLATEAU_CITIES,
        "months": (),
        "level": "high",
        "title": "高原天气变脸快（全年 · 尤以 5~10 月）",
        "detail": "山区一天能过四季，午后常起风下雨，高海拔路段可能临时封路或停摆渡车。"
                  "① 把翻山、看日出这类项目排在上午；"
                  "② 下午留室内备份（寺庙、博物馆、藏文化展馆）；"
                  "③ 自驾去海拔 4000 米以上段，出发前确认路况与放行时间。",
    },
    {
        "key": "cold",
        "match": lambda c, p, lat: c in _COLD_CITIES or lat >= 42,
        "months": (12, 1, 2),
        "level": "high",
        "title": "严寒与冰雪（12~2 月）",
        "detail": "暴雪可能致高速封闭、航班延误或取消，户外项目（滑雪之外）体感极差。"
                  "① 跨城交通优先选高铁而不是自驾；"
                  "② 提前订可免费取消的住宿；"
                  "③ 电子产品低温掉电快，随身保温。",
    },
    {
        "key": "rain_sw",
        "match": lambda c, p, lat: p in _SOUTHWEST_PROVS or c in _SOUTHWEST_HILL_CITIES,
        "months": (5, 6, 7, 8, 9, 10),
        "level": "mid",
        "title": "雨季（5~10 月）",
        "detail": "午后雷阵雨是常态，山区还伴滑坡落石风险。"
                  "① 户外景点全部排上午，午后安排博物馆、古镇院落、茶馆；"
                  "② 随身带雨衣（比伞好用，山区风大）；"
                  "③ 连续降雨后避免进入峡谷、溪谷路段。",
    },
    {
        "key": "plum",
        "match": lambda c, p, lat: p in _PLUM_PROVS,
        "months": (6, 7),
        "level": "mid",
        "title": "梅雨（6~7 月）",
        "detail": "连阴雨概率高，可能一下两三天，湖景、登山类体验打折扣。"
                  "① 行程里至少安排一天室内为主的方案；"
                  "② 观景类项目（日出、游船）留出可顺延的余地；"
                  "③ 鞋袜多带一双，湿了能换。",
    },
    {
        "key": "heat",
        "match": lambda c, p, lat: c in _HEAT_CITIES,
        "months": (7, 8),
        "level": "mid",
        "title": "高温（7~8 月）",
        "detail": "午后 13:00~16:00 露天体感极热，容易中暑、也最影响体验。"
                  "① 把户外大景点放在 09:00 前和 16:30 后；"
                  "② 最热的三小时安排博物馆、商场、影院等有空调的地方；"
                  "③ 每天补水 2 升以上。",
    },
    {
        "key": "storm_north",
        "match": lambda c, p, lat: p in _NORTH_PROVS,
        "months": (7, 8),
        "level": "mid",
        "title": "主汛期短时强降雨（7~8 月）",
        "detail": "华北雨季常见一小时几十毫米的短时暴雨，山区易发地质灾害。"
                  "① 下雨当天取消山区徒步、漂流、峡谷类项目；"
                  "② 市区行程问题不大，但避开地下通道与低洼路段；"
                  "③ 关注当地文旅部门的景区临时关闭公告。",
    },
    {
        "key": "sand",
        "match": lambda c, p, lat: p in _NORTHWEST_PROVS or c in _NORTHWEST_CITIES,
        "months": (3, 4, 5),
        "level": "mid",
        "title": "沙尘与大风（3~5 月）",
        "detail": "风沙天能见度骤降，部分景区（鸣沙山、雅丹、戈壁线路）会临时关闭。"
                  "① 备 N95 口罩、防风镜、润唇膏；"
                  "② 敦煌、张掖这类线路把参观日与机动日错开；"
                  "③ 留一天备用日专门对冲风沙天。",
    },
    {
        "key": "damp",
        "match": lambda c, p, lat: p in _SOUTH_DAMP_PROVS,
        "months": (2, 3, 4),
        "level": "low",
        "title": "回南天与阴雨（2~4 月）",
        "detail": "空气湿度接近饱和，室内地面湿滑、衣物难干，海景能见度也差。"
                  "① 选通风好的住宿；"
                  "② 户外安排偏「老城漫步 + 美食」而非海岛出海；"
                  "③ 注意湿滑台阶。",
    },
    {
        "key": "haze",
        "match": lambda c, p, lat: p in _HAZE_PROVS,
        "months": (11, 12, 1, 2),
        "level": "low",
        "title": "冬季霾（11~2 月）",
        "detail": "重污染时段登高、观景类项目视野受限，也可能影响航班。"
                  "① 出发前查空气质量指数，重污染天改走高室内的博物馆线路；"
                  "② 备口罩。",
    },
]

# 通用条目：任何目的地、任何月份都提醒一次（低等级，排最后）
_GENERIC_TIP = {
    "key": "forecast",
    "level": "low",
    "title": "以出发前预报为准",
    "detail": "以上为气候规律（几月容易出什么天气），不是实时预报。"
              "出发前一晚看一眼目的地未来三天的天气，把雨天概率最高的那天换成室内行程，"
              "其余顺序都不用动。",
}


def _weather_risks(city, prov, lat, month):
    """命中当前月份的风险条目，按等级排序（high → mid → low）。"""
    hits = []
    for r in _RULES:
        months = r["months"]
        if months and month not in months:
            continue
        try:
            if not r["match"](city, prov, lat):
                continue
        except Exception:
            continue
        hits.append({k: r[k] for k in ("key", "level", "title", "detail")})
    hits.sort(key=lambda x: _LEVEL_WEIGHT.get(x["level"], 9))
    hits.append(dict(_GENERIC_TIP, level="low"))
    return hits


# ---------------------------------------------------------------------------
# 二、每日留白：按天数给基础值，再按行程强度加码
# ---------------------------------------------------------------------------
def _buffer_minutes(days, interests):
    days = max(1, int(days or 1))
    if days <= 2:
        base = 60          # 短途：本身就是紧行程，留太久反而浪费
    elif days <= 4:
        base = 90
    elif days <= 7:
        base = 120
    else:
        base = 150         # 长线迟早会有一天「不想动」，多留一点

    its = {str(i) for i in (interests or [])}
    why = []
    if its & {"户外探险"}:
        base += 30
        why.append("含户外项目（受天气与体力影响大）")
    if its & {"自然风光"}:
        base += 15
        why.append("含自然景观（进出景区耗时弹性大）")
    if its & {"亲子游"}:
        base += 30
        why.append("带娃出行（节奏天然更慢）")
    if its & {"美食探店"}:
        base += 15
        why.append("含探店（热门店要排队）")
    base = min(base, 180)
    return base, why


def _buffer_plan(days, interests):
    days = max(1, int(days or 1))
    mins = _buffer_minutes(days, interests)[0]
    total_h = mins * days / 60.0
    hours, rest = int(total_h), int(round((total_h - int(total_h)) * 60))
    total_txt = "%d 小时" % hours + (" %d 分钟" % rest if rest else "") if hours else "%d 分钟" % rest
    return {
        "minutes": mins,
        "total_text": total_txt,
        "label": "每天约 %d 分钟" % mins,
        "text": "建议每天留出约 <b>%d 分钟</b>不安排任何项目（%d 天合计约 %s）。"
                % (mins, days, total_txt),
    }


# ---------------------------------------------------------------------------
# 三、应变预案
# ---------------------------------------------------------------------------
def _fallback_order(days, interests):
    """赶不上计划时的舍弃顺序（从最先舍到最不能舍）。"""
    days = max(1, int(days or 1))
    items = [
        ("① 先舍「顺路看一眼」的打卡点",
         "网红机位、小型展馆、顺路的公园这类项目位置可有可无，砍掉损失最小，"
         "通常是第一个被牺牲的。"),
        ("② 再舍「排队型」项目",
         "需要现场排长队、又不接受预约的，一旦延误就直接放弃，把时间还给核心景点。"),
        ("③ 保住已预约、已购票的项目",
         "这类多限当日有效、改期成本最高（甚至不退），宁可调整别的安排也要保住它们。"),
        ("④ 核心地标只做「减法」不做「删除」",
         "不到万不得已不动核心地标；真赶不上就改成「只看主景、放弃周边小馆」，"
         "别把一整天全删掉。"),
    ]
    tail = ("⑤ 连续两天都需要砍 → 说明整体排得太满",
            "这时候别再天天赶场：把后一天的安排整体后移，或者干脆舍掉排在最后的那一天。")
    items.append(tail)
    if days <= 2:
        items.insert(0, ("⓿ 短途首要原则：只设一个「必看」",
                         "%d 天行程很容易被塞满。只锁一个必看项目，其余全部当作可选项，"
                         "当天状态好再补。" % days))
    return items


_INDOOR_COMMON = ["博物馆 / 美术馆（多数城市都有免费馆）", "古城或寺庙的室内殿宇",
                  "老字号茶馆 / 咖啡馆坐一坐", "商场与超市（顺便补给、躲雨）"]


def _indoor_alternatives(interests):
    """雨天 / 太热 / 太冷时的室内替代，按兴趣给更贴的类型。"""
    mapping = {
        "自然风光": ["自然博物馆、地质馆", "植物园温室馆", "水族馆 / 海洋馆"],
        "历史文化": ["省市级博物馆、专题展馆", "美术馆、非遗体验馆", "古城内的室内展陈馆"],
        "美食探店": ["本地美食城 / 室内市集", "烹饪课、茶艺课（不少城市有半日体验）",
                     "老字号总店堂食（排队也在室内）"],
        "购物娱乐": ["大型商场、奥特莱斯", "影院、KTV、密室逃脱", "室内游乐场 / 电玩城"],
        "亲子游": ["科技馆、自然馆（孩子最爱）", "水族馆、室内游乐场", "图书馆少儿区"],
        "户外探险": ["攀岩馆、室内滑雪场", "室内游泳馆 / 温泉", "VR 体验馆"],
    }
    out = []
    for it in (interests or []):
        out += mapping.get(str(it), [])
    # 兴趣项最多占 4 条，给通用项留住位置——通用的「博物馆 / 商场」
    # 恰恰是雨天最常用、最容易执行的替代，不能被兴趣项全挤掉。
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    uniq = uniq[:4]
    for x in _INDOOR_COMMON:
        if len(uniq) >= 6:
            break
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:6]


def _prepare(city, prov, days, people, weather):
    """出发前要确认的事（按风险与人数动态调整）。"""
    items = [
        "把预约二维码、门票、车票截图存到手机相册（离线也能出示）",
        "下载目的地的离线地图（山区、海岛信号容易断）",
        "记下住宿与景区官方客服电话，改行程第一时间联系",
    ]
    keys = {w["key"] for w in weather}
    if "typhoon" in keys:
        items.append("台风季务必选「可免费取消」的住宿与机票，并确认旅行险含行程取消")
    if "cold" in keys:
        items.append("确认跨城交通（暴雪易封路），优先高铁；下载 12306 备用改签")
    if "plateau" in keys:
        items.append("备好抗高反药品与保暖层，确认当晚住宿海拔别升太快")
    if "heat" in keys:
        items.append("备好防晒与电解质水，确认景区有无午间闭园")
    if int(people or 1) > 3:
        items.append("多人同行：把集合时间与地点写进群公告，走散时不必互相找")
    if int(days or 1) >= 6:
        items.append("长线行程留一个「空白天」不做安排，用来消化前几天的延误")
    return items


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_flexible(destination, days, people=1, interests=None):
    """生成弹性保障方案。

    返回 dict：
      ok=False 时带 reason（目的地识别不了 / 没填）；
      ok=True 时含 headline / weather / buffer / fallback / indoor / prepare。
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

    today = datetime.date.today()
    month = today.month
    lat = _CITY.get(city, (30.0,))[0]

    weather = _weather_risks(city, prov, lat, month)
    buffer = _buffer_plan(days, interests)
    fallback = _fallback_order(days, interests)
    indoor = _indoor_alternatives(interests)
    prepare = _prepare(city, prov, days, people, weather)

    top = [w for w in weather if w["level"] in ("high", "mid")]
    if top:
        headline = "%s · %d 月：%s，建议%s" % (
            city, month, "、".join(w["title"] for w in top[:2]), buffer["label"],
        )
    else:
        headline = "%s · %d 月：当季气候相对平稳，仍建议%s，给临时变化留活口" % (
            city, month, buffer["label"],
        )

    return {
        "ok": True,
        "city": city,
        "prov": prov,
        "month": month,
        "headline": headline,
        "weather": weather,
        "top_keys": [w["key"] for w in top],
        "buffer": buffer,
        "buffer_why": _buffer_minutes(days, interests)[1],
        "fallback": fallback,
        "indoor": indoor,
        "prepare": prepare,
        "days": days,
        "people": people,
    }


if __name__ == "__main__":        # 自测：python -m core.flexible
    import time as _t
    for d, n, its in [
        ("三亚", 4, ["自然风光", "美食探店"]),
        ("重庆", 3, ["美食探店"]),
        ("哈尔滨", 3, ["历史文化"]),
        ("拉萨", 6, ["自然风光", "户外探险"]),
        ("杭州", 2, ["亲子游"]),
        ("敦煌", 5, ["历史文化"]),
        ("火星", 3, []),
        ("", 3, []),
    ]:
        info = build_flexible(d, n, 2, its)
        print("-" * 72)
        print(d or "(空)", "->", info.get("reason", "ok"))
        if not info.get("ok"):
            continue
        print("  HEAD:", info["headline"])
        for w in info["weather"]:
            print("  [%s] %s" % (w["level"], w["title"]))
        print("  BUFFER:", info["buffer"]["text"].replace("<b>", "").replace("</b>", ""))
        print("  INDOOR:", "；".join(info["indoor"]))
        print("  PREPARE:", "；".join(info["prepare"]))
    # 校验：每个月份都有兜底条目，任一目的地都不会出现空列表
    for m in range(1, 13):
        for d in ("三亚", "哈尔滨", "拉萨", "杭州", "北京", "乌鲁木齐"):
            r = resolve_place(d)
            xs = _weather_risks(r[0], r[1], _CITY.get(r[0], (30.0,))[0], m)
            assert xs, (d, m)
    print("-" * 72)
    print("OK: 12 个月 x 6 个目的地 均有天气条目（含通用兜底）")
