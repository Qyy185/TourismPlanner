# -*- coding: utf-8 -*-
"""行程 → .ics 日历文件（手机日历 / 系统日历直接导入）

为什么值得做：
    方案里每天的行程本来就带时间段（「上午 9：30-11：30｜甲秀楼」），
    导成日历后出行当天手机会自动提醒，比翻聊天记录找方案靠谱。

实现说明：
    · 纯字符串生成 RFC 5545，零依赖；
    · DTSTART / DTEND 用 floating time（不带 Z、不带 TZID）——手机日历
      会按设备本地时区显示，正是旅行时要的效果；
    · 必须 CRLF 行尾、UTF-8 无 BOM；超过 75 字节的行按规范折行
      （续行以空格开头），否则部分日历客户端会解析失败；
    · 起始日期取「明天」：方案本身只有 Day1/Day2/Day3，没有绝对日期，
      导进日历后用户可以自己拖到真实出行日。
"""

import datetime as _dt
import os
import re
import time

from core.render import (_BULLET_RE, _DAY_CN, _DAY_EN, _SECTION_MARKS,
                         _clean_stop, _cn_to_int, _strip_md)

# 时间点：09：30 / 9:30（全角半角冒号都认）
_TIME_PT = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")

# 这些行不进日历：机动/备选是「留白与退路」，不是定点活动
_SKIP_MARKS = ("🕐", "☔", "机动", "备选")

# 只给了一个时间点（如「07：30｜贵阳北站乘高铁」）时，默认按 90 分钟排
_DEFAULT_MINUTES = 90

# 行程行的形状是「时间｜站点」，所以时间只看第一个全角/半角竖线之前的部分；
# 没有竖线时才退回行首一小段——否则站点说明里括号中的附注时间
# （如「返程…（18:30发车）」）会被误当成本活动的开始时间
_HEAD_LEN = 12


def _times_in(text):
    """取一段文字里的时间点 [(h, m), ...]（按出现顺序，最多 2 个）"""
    pts = []
    for h, m in _TIME_PT.findall(str(text or "")):
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            pts.append((h, m))
        if len(pts) >= 2:
            break
    return pts


def _add_minutes(hm, mins):
    total = (hm[0] * 60 + hm[1] + mins) % (24 * 60)
    return (total // 60, total % 60)


def _time_head(txt):
    """取「该看时间的那一段」：竖线之前（行程行是「时间｜站点」）；
    没有竖线时退回行首 _HEAD_LEN 个字符"""
    head = str(txt or "").split("｜")[0].split("|")[0]
    return head if head != txt else txt[:_HEAD_LEN]


def parse_events(md):
    """把方案解析成 [{"day":1, "start":(9,30), "end":(11,30), "title":"甲秀楼",
                     "extra":["九道关军事防御体系", ...]}]

    start/end 为 None 表示这行没写时间（那天只有先后顺序），
    生成日历时按 09:00 起、默认时长排。

    extra 里装的是「没写时间、归属上一个有时段行程」的小项——详尽档常这么写：

        - 上午 9：00-12：00｜**天龙屯堡古镇**
          - **九道关军事防御体系**
          - **沈万三故居**

    三个子项本身没有时间，硬给它们排 09:00 会和主行程撞车（日历报重复），
    所以直接挂到主行程上。
    """
    events = []
    day_no = 0
    slot = None                 # 当前「有时段的行程」，无时间的行归它

    for raw in str(md or "").splitlines():
        s = raw.strip()
        if not s:
            continue

        m = _DAY_EN.match(s) or _DAY_CN.match(s)
        if m:
            num = m.group(1)
            try:
                day_no = int(num)
            except (TypeError, ValueError):
                day_no = _cn_to_int(num)
            slot = None

            # 行内式（快速/均衡档）把行程写在同一行：
            #   - **Day1**：宽窄巷子 → 人民公园（盖碗茶）→ 锦里古街（夜景）｜🕐 机动 60 分钟
            # 若不在这里就地拆开，这天的行程会随 Day 头一起被吃掉 → 导出日历 0 条
            # （2026-10-14 发现：均衡档是默认档，影响面最大）。
            # 块头式（详尽档）的剩余部分是副标题（以 ** 收尾），不是行程，跳过。
            rest = s[m.end():].strip()
            if rest and not rest.endswith("**"):
                body = re.split(r"[｜|]", rest)[0].strip()
                stops = [x.strip().rstrip("*").strip()
                         for x in re.split(r"→|➔|->|—>", body) if x.strip()]
                if len(stops) > 1:
                    for stp in stops:
                        t = _clean_stop(stp) or _strip_md(stp)
                        if not t:
                            continue
                        pts = _times_in(_time_head(t))
                        st = pts[0] if pts else None
                        en = pts[1] if len(pts) > 1 else None
                        if st and en and _mins(en) <= _mins(st):
                            en = None
                        events.append({"day": day_no, "start": st, "end": en,
                                       "title": t, "extra": []})
            continue

        # 进入「预算 / 美食 / 住宿」等小节后，后面的列表行不再属于任何一天
        if s.startswith(_SECTION_MARKS):
            day_no = 0
            slot = None
            continue

        if day_no <= 0 or not _BULLET_RE.match(s):
            continue

        item = _BULLET_RE.sub("", s, count=1)
        txt = _strip_md(item).strip()
        if any(k in txt[:8] for k in _SKIP_MARKS):
            continue

        title = _clean_stop(item) or txt[:24]
        if not title:
            continue

        pts = _times_in(_time_head(txt))
        start = pts[0] if pts else None
        end = pts[1] if len(pts) > 1 else None
        if start and end and (end[0] * 60 + end[1]) <= (start[0] * 60 + start[1]):
            end = None                     # 脏时间（如 17:00-00-19:00 被切坏）时丢弃结束点

        # 没写时间的小项 → 归入上一个有时段的行程，不单独排时间
        if start is None and slot is not None:
            if title != slot["title"] and title not in slot["extra"]:
                slot["extra"].append(title)
            continue

        ev = {"day": day_no, "start": start, "end": end,
              "title": title, "extra": []}
        events.append(ev)
        if start:                          # 只有有时段的条目才当「归属目标」
            slot = ev

    return events


def _eff_end(ev):
    """有效结束时间：没写结束点的按默认时长兜底（与 build_ics 的排法一致）"""
    st = ev["start"] or (9, 0)
    return ev["end"] or _add_minutes(st, _DEFAULT_MINUTES)


def _mins(hm):
    return hm[0] * 60 + hm[1]


def merge_events(events):
    """把「同一天 + 同一开始时间」的并列项合并成一条主事件

    为什么需要（2026-10-14 老叶反馈）：
        详尽档里大景点会把自己的子项一起列出来——

            - 上午 9：00-12：00｜**天龙屯堡古镇（距贵阳1.5小时车程）**
              - **九道关军事防御体系**（石头迷宫式巷道）
              - **沈万三故居**（明代商帮文化缩影）
              - **地戏堂**（现场观看傩面地戏排练）

        解析后是 4 条同点事件（都从 09:00 开始），导进手机日历会互相报
        「时间重叠」，看着很乱。合并后只留一条 9:00-12:00 的主事件，
        子项折进 DESCRIPTION，日历干净且不丢信息。

    合并规则：
        · 只在「显式写了相同开始时间」的条目之间合并；
        · 跨度最长的那条当主事件（原是子项更长的被顶上来的情况也能处理）；
        · 纯时间点相同的重复项（同标题）直接去重。

    ⚠️ 没写时间（start=None，排日历时按 09:00 兜底）的事件不参与合并——
       否则一天里所有没写时间的行程会被错误压成一条。
    """
    out = []
    seen = {}                       # (day, start) -> out 里的下标

    for ev in events:
        key = (ev["day"], ev["start"])
        if ev["start"] is None or key not in seen:
            out.append({"day": ev["day"], "start": ev["start"], "end": ev["end"],
                        "title": ev["title"], "extra": list(ev.get("extra") or [])})
            if ev["start"] is not None:
                seen[key] = len(out) - 1
            continue

        main = out[seen[key]]
        if _mins(_eff_end(ev)) > _mins(_eff_end(main)):
            # 新来的这条跨度更长 → 它当主事件，原来那条降为子项
            demoted = main["title"]
            main["end"] = ev["end"] or _eff_end(ev)
            main["title"] = ev["title"]
            main["extra"] = [t for t in [demoted] + main["extra"]
                             if t != ev["title"]]
        elif ev["title"] != main["title"] and ev["title"] not in main["extra"]:
            main["extra"].append(ev["title"])
        for t in (ev.get("extra") or []):
            if t != main["title"] and t not in main["extra"]:
                main["extra"].append(t)

    return out


def _clamp_ends(events):
    """把没有时间/只有开始点的事件排成不重叠的一天

    两类情况：

    1. 完全没写时间（行内式的「A → B → C」、或只列了站点的日子）——
       按顺序从 09:00 起、每条默认 90 分钟往后排，而不是全部挤在 09:00
       （全都 09:00 会让日历互相报「时间重叠」，跟没排一样）。
    2. 只写了开始点、没写结束点（「07：30｜乘高铁」）——默认 90 分钟，
       但若下一条行程更早开始，就收口到下一条之前，避免造假重叠。
       方案里明确写了的结束时间不动。
    """
    by_day = {}
    for ev in events:
        by_day.setdefault(ev["day"], []).append(ev)

    for lst in by_day.values():
        cursor = 9 * 60                       # 当天「顺排」用的游标
        for i, ev in enumerate(lst):
            if not ev["start"]:
                st = max(cursor, 9 * 60)
                ev["start"] = (st // 60, st % 60)
                ev["_derived"] = True

            st = _mins(ev["start"])
            if ev["end"]:
                cursor = max(cursor, _mins(ev["end"]))        # 下一条接着上一条排
                continue

            dflt = st + _DEFAULT_MINUTES
            nxt = None
            for later in lst[i + 1:]:
                if later["start"] and _mins(later["start"]) > st:
                    nxt = _mins(later["start"])
                    break
            if nxt is not None and nxt < dflt:
                dflt = nxt
            ev["end"] = (dflt // 60, dflt % 60)
            cursor = max(cursor, dflt)
    return events


def plan_events(md):
    """解析 → 合并同点并列项 → 收口默认时长，得到最终事件表

    导出日历和「已导出 N 条日程」的计数都走这里，保证数字与文件一致。
    """
    return _clamp_ends(merge_events(parse_events(md)))


def _esc_ics(s):
    """RFC 5545 转义"""
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r", "").replace("\n", "\\n"))


def _fold(line):
    """按 75 字节折行（续行以空格开头）——中文一个字 3 字节，必须按字节算"""
    out, cur = [], ""
    for ch in str(line):
        if cur and len(cur.encode("utf-8")) + len(ch.encode("utf-8")) > 73:
            out.append(cur)
            cur = " "                      # 续行的第一个字符是空格
        cur += ch
    out.append(cur)
    return out


def _fmt_local(date, hm):
    return "%04d%02d%02dT%02d%02d00" % (date.year, date.month, date.day, hm[0], hm[1])


def build_ics(md, destination="", days=0, people=0, start_date=None, out_dir=None):
    """生成 .ics 并返回文件路径；方案里解析不出任何按天行程时返回 None"""
    events = plan_events(md)          # 已合并同点并列项
    if not events:
        return None

    base = start_date or (_dt.date.today() + _dt.timedelta(days=1))
    stamp = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    cal_name = "%s行程" % (destination or "旅行")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Smart Travel Planner//Travel Plan//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:%s" % _esc_ics(cal_name),
    ]

    for i, ev in enumerate(events):
        day = base + _dt.timedelta(days=max(int(ev["day"]) - 1, 0))
        st = ev["start"] or (9, 0)
        en = ev["end"] or _add_minutes(st, _DEFAULT_MINUTES)
        desc = "第%d天 · %s（由智慧旅游规划助手生成）" % (ev["day"], cal_name)
        if ev.get("extra"):
            desc += "；本时段还含：%s" % "、".join(ev["extra"])
        lines += [
            "BEGIN:VEVENT",
            "UID:travel-%s-%d@smart-travel-planner" % (stamp, i),
            "DTSTAMP:%s" % stamp,
            "DTSTART:%s" % _fmt_local(day, st),
            "DTEND:%s" % _fmt_local(day, en),
            "SUMMARY:%s" % _esc_ics(ev["title"]),
            "DESCRIPTION:%s" % _esc_ics(desc),
        ]
        if destination:
            lines.append("LOCATION:%s" % _esc_ics(destination))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    text = "".join("\r\n".join(_fold(ln)) + "\r\n" for ln in lines)

    out_dir = out_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "exports")
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "", str(destination or "行程"))[:12] or "行程"
    path = os.path.join(out_dir, "行程日历-%s-%s.ics" % (
        safe, time.strftime("%Y%m%d-%H%M%S")))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path
