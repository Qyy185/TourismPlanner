# -*- coding: utf-8 -*-
"""
行程时间轴 / 分享页 / 海报 的 HTML 渲染

为什么单独成一个模块：
    这三块都是「把方案 Markdown 变成好看的 HTML」，纯字符串处理、无副作用、
    好单测；塞进 app_gradio.py 会让那个已经 2000 行的文件更难维护。

解析的格式来自 core/prompts.py 里钉死的输出约定，形如：

    - **Day1**：宽窄巷子 → 人民公园（盖碗茶）→ 锦里古街（夜景）｜🕐 机动 60 分钟
    - **Day2**：大熊猫繁育研究基地 → 春熙路/太古里｜☔ 备选：成都博物馆 + 太古里室内线

所以按「Day 行 + 全角竖线分段」解析即可，不用做通用 Markdown 解析。
"""

import html as _html
import re

# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def esc(s):
    return _html.escape(str(s or ""), quote=True)


def _strip_md(s):
    """去掉行内的 **加粗** 与 `代码` 标记，留下纯文本"""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", str(s or ""))
    return s.replace("`", "").strip()


def _extract_title(md):
    """取方案标题：优先第一个 ### 标题行，退而求其次用第一行非空文本"""
    for line in str(md or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return _strip_md(s.lstrip("#").strip())
    for line in str(md or "").splitlines():
        if line.strip():
            return _strip_md(line)[:40]
    return "旅游方案"


def _extract_budget(md):
    """取预算行（形如 💰 **预算（人均）**：...）

    注意两点（2026-10-12 老叶反馈海报预算框出现「### 澳门…」生标题）：
    1. 标题行（### 澳门3天2晚…人均预算6000元…）同时含「预算」和「人均」，
       会被误抓成预算行 —— 必须跳过 # 开头的行；
    2. 详尽档的 💰 行是节标题（没有金额），金额在下面的分项列表里 ——
       此时把「合计」行拼在后面，海报上才看得到数。
    """
    lines = [ln.strip() for ln in str(md or "").splitlines()]
    head = ""
    for s in lines:
        if not s or s.startswith("#"):
            continue
        # 「预算」必须是行首主角（去 emoji/加粗/列表前缀后）——正文导语
        # 「您偏好…预算充裕（人均6000元）」这种不能抢（2026-10-12 二次踩坑）
        probe = _strip_md(s.lstrip("-* ").strip()).lstrip("🕐☔💰 ").strip()
        if "💰" in s or probe.startswith("预算"):
            head = _strip_md(s)
            break
    if not head:
        return ""
    # 💰 行里没有「合计」才去补合计行 —— 详尽档的节标题常带「人均6000元」
    # （那是用户给的预算上限，不是估算花费），有金额≠有合计
    if "合计" not in head:
        for s in lines:
            if not s or s.startswith("#"):
                continue
            t = _strip_md(s).lstrip("- ").strip()
            if "合计" in t and re.search(r"\d", t):
                head = head.rstrip("：: ") + "，" + t
                break
    return head


# ---------------------------------------------------------------------------
# 行程时间轴
# ---------------------------------------------------------------------------

# Day 头两种写法分开认（LLM 实际输出会混用：前两天 Day1/Day2，第三天变「第3天|」）
_DAY_EN = re.compile(
    r"^\s*(?:[-*•]\s*)?\**\s*Day\s*([0-9０-９]{1,2})\s*\**\s*(?:[：:｜|]\s*)?", re.I)
_DAY_CN = re.compile(
    r"^\s*(?:[-*•]\s*)?\**\s*第\s*([0-9０-９一二三四五六七八九十]{1,3})\s*天"
    r"\s*\**\s*(?:[：:｜|]\s*)?")

# bullet 行：无序（- * •）与有序（1. 2、3)）都算 —— 详尽档真实输出两种都有
_BULLET_RE = re.compile(r"^(?:[-*•]|\d{1,2}[.、)])\s+")

# 详尽档里，进入这些小节后，下面的「- 」列表行就不再属于任何一天
_SECTION_MARKS = ("#", "💰", "🍜", "🏨", "⚠", "🛡", "📚", "🖼", "📍", "📅", "---")

_CN_DIGITS = "零一二三四五六七八九"


def _cn_num(n):
    """1..99 → 一/十二/二十一（badge 统一显示「第X天」）"""
    n = int(n)
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_DIGITS[n % 10]
    return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")


def _badge(label):
    """Day1 / 第3天 → 第一天 / 第三天"""
    m = re.match(r"^(?:Day|第)\s*([0-9０-９]+)\s*天?$", str(label), re.I)
    if m:
        return "第%s天" % _cn_num(m.group(1))
    return str(label)


def _tag_tail(txt):
    """取「：」后的说明文字，截到第一个句号（时间轴/海报标签要短）"""
    tail = re.split(r"[：:]", txt, 1)[1].strip() if re.search(r"[：:]", txt) else txt
    return tail.split("。")[0].strip()


# ---- 块头式站点行清洗：LLM 真实输出比约定的脏得多 ----
# 「上午 9：30-11：30｜甲秀楼」「16： 00-17： 00|陡坡塘瀑布」「17：00-00-19：00|00]青岩古镇」
_PERIOD_WORDS = ("上午", "中午", "下午", "傍晚", "晚上", "清晨", "凌晨",
                 "早上", "全天", "白天", "夜晚")

_TIME_HEAD = re.compile(
    r"^(?:[-—–~至]+\s*)?"                                 # 区间中间段「-19：00」也能剥
    r"[0-9０-９]{1,2}\s*[:：]\s*[0-9０-９]{0,2}"           # 09:30 / 16： 00
    r"(?:\s*[-—–~至]+\s*[0-9０-９\s:：]*)?")               # -11：30 / -00-19：00（贪到下个非时间字符）

_RESIDUE = re.compile(r"^[0-9０-９]{1,2}\s*[\]）)|｜]\s*|^\]\s*")   # 「00]」「00|」类残渣

_NOISE_START = ("交通", "贴士", "参考", "提示", "注意", "价格", "费用",
                "预约", "备注", "说明", "重点看")

_STOP_MAX = 42


def _clean_stop(raw):
    """把一颗站点候选行清洗成干净名字（剥时段词/脏时间串/残渣/解释尾巴）"""
    t = _strip_md(raw).strip()
    for _ in range(8):                      # 头部 token 循环剥，直到不再变化
        t0 = t
        for w in _PERIOD_WORDS:
            if t.startswith(w):
                t = t[len(w):].strip()
                break
        else:
            m = _TIME_HEAD.match(t)
            if m:
                t = t[m.end():].strip()
            else:
                m = _RESIDUE.match(t)
                if m:
                    t = t[m.end():].strip()
                else:
                    m = re.match(r"^[｜|:：,，、;；\s]+", t)
                    if m:
                        t = t[m.end():].strip()
        if t == t0:
            break
    # 「——」后面是游览说明，时间轴只留主体
    t = re.split(r"——|--", t)[0].strip()
    t = t.split("。")[0].strip()
    t = t.strip(" ｜|，,；;")
    if len(t) > _STOP_MAX:
        t = t[:_STOP_MAX - 1] + "…"
    return t


def _split_inline(rest, cur):
    """行内式：站点直接跟在 Day 行（宽窄巷子 → 人民公园｜🕐 机动 60 分钟）"""
    buffer_txt, backup_txt = "", ""
    for marker, key in (("☔", "backup"), ("🕐", "buffer")):
        idx = rest.find(marker)
        if idx >= 0:
            tail = rest[idx + 1:].lstrip("：: ").strip()
            tail = tail.split("｜")[0].split("|")[0].strip()
            if key == "backup":
                backup_txt = re.sub(r"^备选\s*[：:]?\s*", "", tail).strip()
            else:
                buffer_txt = re.sub(r"^机动\s*[：:]?\s*", "", tail).strip()
            rest = rest[:idx]

    rest = rest.split("｜")[0].split("|")[0].strip().rstrip("。；;")
    stops = [x.strip() for x in re.split(r"→|➔|->|—>", rest) if x.strip()]
    cur["stops"] = [_strip_md(x) for x in stops]
    cur["buffer"] = buffer_txt
    cur["backup"] = backup_txt


def parse_days(md):
    """把方案正文解析成每日行程

    支持两种输出格式：

    1. 行内式（快速/均衡档）——站点写在 Day 行内，用 → 串联：
       - **Day1**：宽窄巷子 → 人民公园｜🕐 机动 60 分钟

    2. 块头式（详尽档）——Day 行只有副标题，站点在下面的列表行里：
       **Day1｜贵阳老城·山水文脉初识**
       - 上午 9：30-11：30｜**甲秀楼 & 南明河步道**
       - 🕐 机动：19:30后自由安排90分钟

    块头式站点行按「整行清洗」提取（2026-10-12 老叶反馈真实方案里出现
    时段词头、全角冒号带空格「16： 00」、残渣「00]青岩古镇」、Day/第X天
    混用等，靠抓加粗片段会大量漏站）。

    返回 [{"label": "Day1", "subtitle": "...", "stops": [...],
           "buffer": "...", "backup": "..."}, ...]
    """
    days = []
    cur = None

    for line in str(md or "").splitlines():
        s = line.strip()
        if not s:
            continue

        # ---- Day 行（Day1 / 第3天 两种头都认） ----
        m = _DAY_EN.match(s) or _DAY_CN.match(s)
        if m:
            num = m.group(1)
            if not re.match(r"^[0-9０-９]+$", num):
                num = _cn_to_int(num)
            cur = {"label": "Day%s" % num, "subtitle": "",
                   "stops": [], "buffer": "", "backup": ""}
            rest = s[m.end():].strip()

            if re.search(r"→|➔|->|—>", rest):
                # 行内式：站点直接跟在 Day 行
                _split_inline(rest, cur)
            else:
                # 块头式：rest 是副标题（可能行内还挂了 ☔/🕐 标记）
                for marker, key in (("☔", "backup"), ("🕐", "buffer")):
                    idx = rest.find(marker)
                    if idx >= 0:
                        tail = rest[idx + 1:].lstrip("：: ").strip()
                        if key == "backup":
                            cur["backup"] = re.sub(r"^备选\s*[：:]?\s*", "",
                                                   tail.split("｜")[0]).strip()
                        else:
                            cur["buffer"] = re.sub(r"^机动\s*[：:]?\s*", "",
                                                   tail.split("｜")[0]).strip()
                        rest = rest[:idx]
                cur["subtitle"] = _strip_md(rest).strip(" ｜|*")
            if cur["stops"] or cur["buffer"] or cur["backup"] or cur["subtitle"]:
                days.append(cur)
            else:
                cur = None
            continue

        # ---- 小节切换：后面不再是某一天的站点 ----
        if s.startswith(_SECTION_MARKS):
            cur = None
            continue

        # ---- 块头式的列表行：给当前 Day 补站点 / 机动 / 备选 ----
        if cur is not None and _BULLET_RE.match(s):
            item = _BULLET_RE.sub("", s, count=1)
            txt = _strip_md(item).strip()
            core = txt.lstrip("🕐☔ ").strip()
            if txt.startswith("🕐") or core.startswith("机动"):
                if not cur["buffer"]:
                    cur["buffer"] = _tag_tail(re.sub(r"^机动\s*[：:]?\s*", "", core))
                continue
            if txt.startswith("☔") or core.startswith("备选"):
                if not cur["backup"]:
                    cur["backup"] = _tag_tail(re.sub(r"^备选\s*[：:]?\s*", "", core))
                continue
            stop = _clean_stop(item)
            if (stop and not stop.startswith(_NOISE_START)
                    and stop not in cur["stops"]):
                cur["stops"].append(stop)

    return [d for d in days if d["stops"] or d["buffer"] or d["backup"]]


def _cn_to_int(s):
    """一二三 / 十二 / 二十一 → 整数；纯数字直接转"""
    s = str(s).strip()
    if re.match(r"^[0-9０-９]+$", s):
        return int(s.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    total, num = 0, 0
    for ch in s:
        if ch == "十":
            total += (num or 1) * 10
            num = 0
        else:
            d = _CN_DIGITS.find(ch)
            if d > 0:
                num = d
    return total + num


def build_timeline_html(md, destination=""):
    """把每日行程渲染成时间轴卡片；没有可用 Day 行时返回空串（前端据此隐藏）"""
    days = parse_days(md)
    if not days:
        return ""

    cards = []
    for i, d in enumerate(days):
        stops_html = []
        for j, stop in enumerate(d["stops"]):
            stops_html.append(
                "<div class='tl-stop'>"
                "<span class='tl-dot'></span>"
                "<span class='tl-name'>%s</span>"
                "</div>" % esc(stop)
            )
            if j < len(d["stops"]) - 1:
                stops_html.append("<div class='tl-link'></div>")
        if not stops_html:
            stops_html.append("<div class='tl-stop tl-empty'>（本日行程见上方正文）</div>")

        meta = []
        if d["buffer"]:
            meta.append("<span class='tl-tag tl-buf'>🕐 机动 %s</span>" % esc(d["buffer"]))
        if d["backup"]:
            meta.append("<span class='tl-tag tl-bak'>☔ 备选 %s</span>" % esc(d["backup"]))

        sub = ("<div class='tl-sub'>%s</div>" % esc(d["subtitle"])) if d.get("subtitle") else ""

        cards.append(
            "<div class='tl-day'>"
            "<div class='tl-badge'>%s</div>"
            "<div class='tl-body'>"
            "%s"
            "<div class='tl-stops'>%s</div>"
            "%s"
            "</div></div>"
            % (esc(_badge(d["label"])), sub, "".join(stops_html),
               ("<div class='tl-meta'>%s</div>" % "".join(meta)) if meta else "")
        )

    head = "🗓 行程时间轴"
    if destination:
        head += " · %s" % esc(destination)
    return (
        "<div class='tl-card'>"
        "<div class='tl-head'>%s<span class='tl-count'>共 %d 天</span></div>"
        "<div class='tl-list'>%s</div>"
        "</div>" % (head, len(days), "".join(cards))
    )


# ---------------------------------------------------------------------------
# 简单 Markdown → HTML（分享页 / 海报共用）
# ---------------------------------------------------------------------------

def md_to_html(md, keep_tables=False):
    """把方案正文渲染成干净的 HTML 片段（标题 / 列表 / 引用 / 加粗 / 分隔线）

    只覆盖方案实际用到的语法，不做通用解析器——通用解析器要么引依赖，
    要么边界多到测不完，这里够用就好。
    """
    out = []
    in_ul = False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in str(md or "").splitlines():
        s = raw.rstrip()
        if not s.strip():
            close_ul()
            continue

        e = esc(s)
        e = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", e)
        e = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", e)

        if re.match(r"^#\s", s):
            close_ul(); out.append("<h1>%s</h1>" % e[1:].strip())
        elif re.match(r"^##\s", s):
            close_ul(); out.append("<h2>%s</h2>" % e[2:].strip())
        elif re.match(r"^###\s", s):
            close_ul(); out.append("<h3>%s</h3>" % e[3:].strip())
        elif re.match(r"^\s*[-*]\s+", s):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append("<li>%s</li>" % re.sub(r"^\s*[-*]\s+", "", e))
        elif re.match(r"^-{3,}\s*$", s):
            close_ul(); out.append("<hr>")
        elif s.startswith(">"):
            close_ul(); out.append("<blockquote>%s</blockquote>" % e.lstrip("> ").strip())
        else:
            close_ul(); out.append("<p>%s</p>" % e)
    close_ul()
    return "\n".join(out)
