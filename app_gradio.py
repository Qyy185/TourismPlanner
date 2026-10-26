"""
Gradio Web 交互入口

与旧版本的关键差异：不再把表单拼成固定 Prompt 直连大模型，
而是真正交给「旅游规划 Agent」——由 Agent 决定是否调用本地知识库检索工具，
再把检索到的真实资料融合进方案，并在末尾展示命中的知识库来源。
"""
import base64
import glob
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
from collections import OrderedDict

import gradio as gr
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from core.agent import (
    DEFAULT_PROFILE,
    PROFILES,
    create_travel_agent,
    normalize_profile,
)
from core.flexible import build_flexible
from core.ical import build_ics, parse_events, plan_events
from core.multi_route import parse_destinations
from core.packing import build_packing
from core.poster import build_poster
from core.render import build_timeline_html, md_to_html
from core.store import (
    create_share,
    delete_plan,
    get_or_create_share,
    get_plan,
    init_db,
    list_plans,
    resolve_share,
    save_plan,
)
from core.transport import (
    DEFAULT_MODE,
    MODE_LABELS,
    estimate_fare,
    fare_summary,
    resolve_place,
)
from tools.travel_tools import warmup

load_dotenv()

# ---------------------------------------------------------------------------
# 目的地实景图册（图片资产层，与知识库解耦）
#
# 图片存放在 assets/images/，索引为 assets/images/index.json，
# 由 tools/fetch_spots_images.py 构建生成。knowledge_base/ 文本保持纯净，
# 不掺入任何图片语法，避免污染向量检索。
# 图片来自搜索引擎结果，版权归原作者，仅作学习与演示用途。
# ---------------------------------------------------------------------------
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "images")
_DEST_INDEX = []
_REGION_NAMES = None      # 34 个行政区名白名单（惰性构建，图册索引重载后失效）


def _region_names():
    """全部行政区名集合：解析出来的目的地必须落在这里面，否则视为无效。"""
    global _REGION_NAMES
    if _REGION_NAMES is None:
        _REGION_NAMES = {str(e.get("region", "")).strip() for e in _DEST_INDEX} - {""}
    return _REGION_NAMES


def _load_destinations():
    """启动时加载图册索引，并从主攻略提取「本地地名别名集」用于模糊匹配"""
    global _DEST_INDEX, _REGION_NAMES
    index_path = os.path.join(_ASSETS_DIR, "index.json")
    if not os.path.exists(index_path):
        _DEST_INDEX = []
        return
    with open(index_path, encoding="utf-8") as f:
        data = json.load(f)
    kb_root = os.path.join(
        os.path.dirname(os.path.dirname(_ASSETS_DIR)), "knowledge_base", "destinations"
    )
    dests = []
    for entry in data.get("destinations", []):
        imgs = [im for im in entry.get("images", []) if im.get("file")]
        if not imgs:
            continue
        # 主攻略文件名带版本号与前缀（如「智慧旅游规划助手—四川—主攻略—v1.0—20260921.md」），
        # 不能用「四川-主攻略.md」拼路径——那样 34 个省一个都读不到，
        # 别名集全空 → match_destination 只认行政区名 → 图册区永远空白。
        # 用 glob 按「区名 + 主攻略」匹配，文件名怎么改都不会坏。
        region_dir = os.path.join(kb_root, entry["category"])
        hits = glob.glob(os.path.join(region_dir, "*%s*主攻略*.md" % entry["region"]))
        fulltext = ""
        if hits:
            try:
                with open(hits[0], encoding="utf-8") as f:
                    fulltext = f.read()
            except OSError:
                fulltext = ""
        aliases = set()
        # 只取「本地相关」的小节标题做别名：热门景点 / 特色美食 / 住宿建议 / 交通指南。
        # 不用全文包含匹配——交通章节常提到邻省城市（如重庆攻略里写"高铁到成都"），
        # 全文匹配会把「成都」误配到重庆。
        for sec in ("热门景点", "特色美食", "住宿建议", "交通指南"):
            m = re.search(r"^##\s*%s\s*\n(.*?)(?=\n##\s|\Z)" % sec, fulltext, re.S | re.M)
            if m:
                aliases.update(h.strip() for h in re.findall(r"^###\s*(.+?)\s*$", m.group(1), re.M))
        dests.append({**entry, "images": imgs, "aliases": aliases})
    _DEST_INDEX = dests
    _REGION_NAMES = None


_load_destinations()


# ---------------------------------------------------------------------------
# 城市景点密度：多城路线分配住宿夜数时用
# ---------------------------------------------------------------------------
_SPOT_DENSITY = {}


def _load_spot_density():
    """从生成好的「城市总览」文件里统计各城市的景点数。

    景点多的地方值得多住几天，景点少的路过即可——
    这条规则让「住几晚」有据可依，而不是均分。
    """
    global _SPOT_DENSITY
    density = {}
    gen_dir = os.path.join(
        os.path.dirname(os.path.dirname(_ASSETS_DIR)), "knowledge_base", "destinations", "_generated"
    )
    if not os.path.isdir(gen_dir):
        _SPOT_DENSITY = {}
        return
    for name in sorted(os.listdir(gen_dir)):
        if not name.endswith(".md") or "城市总览" not in name:
            continue
        try:
            with open(os.path.join(gen_dir, name), encoding="utf-8") as f:
                cur = None
                for line in f:
                    if line.startswith("## "):
                        h = line[3:].strip()
                        cur = None if (h.startswith("全省") or h.startswith("城市索引")) else h
                        if cur:
                            density.setdefault(cur, 0)
                    elif line.startswith("### ") and cur:
                        density[cur] += 1
        except OSError:
            continue
    _SPOT_DENSITY = density


def _spot_density():
    """返回各城市景点数；未加载时按需加载一次"""
    if not _SPOT_DENSITY:
        _load_spot_density()
    return _SPOT_DENSITY


_load_spot_density()


def match_destination(user_input):
    """把用户输入匹配到图册条目。

    两级匹配：
    1. 行政区名直接匹配（「四川」「重庆」）；
    2. 本地别名集匹配（「成都」→四川、「洪崖洞」→重庆、「漠河」→黑龙江）。
    别名来自主攻略的景点/美食/住宿/交通小节标题，避免把邻省城市误认成属地。
    """
    q = str(user_input or "").strip()
    if not q:
        return None
    for entry in _DEST_INDEX:                     # 1. 区名匹配
        r = entry["region"]
        if q == r or q in r or r in q:
            return entry
    for entry in _DEST_INDEX:                     # 2. 别名集匹配
        for alias in entry.get("aliases", ()):
            if q in alias or alias in q:
                return entry
    return None


def gallery_items_for(entry, interests=None, with_region=True, with_tags=True):
    """转成 gr.Gallery 需要的 [(路径, 说明), ...]，跳过缺失文件。

    说明默认格式：`省份 · 景点 ｜ 兴趣1、兴趣2`（兴趣标签来自 index.json，
    由 tools/build_interest_gallery.py 依据知识库的「兴趣路线」章节生成）。
    手机上窄格子装不下长图注：
    - 待机轮播用「省份 · 景点」（with_tags=False），省份要用于点选回填；
    - 正式图册用「景点 ｜ 兴趣…」（with_region=False），省份已写在图册标题里。
    若传了 interests，命中用户勾选偏好的图排在前面，方便一眼看到对口的图。
    """
    rows = []
    for im in entry.get("images", []):
        if not im.get("file"):
            continue
        path = os.path.join(_ASSETS_DIR, im["file"])
        if not os.path.exists(path):
            continue
        spot = im.get("spot", "")
        if with_region:
            cap = "%s · %s" % (entry["region"], spot)
        else:
            cap = spot
        tags = list(im.get("interests") or [])
        if with_tags and tags:
            cap += " ｜ %s" % "、".join(tags)
        rows.append((path, cap, set(tags)))
    if interests:
        want = set(interests)
        rows.sort(key=lambda r: 0 if (want & r[2]) else 1)   # 命中的排前面（稳定排序）
    return [(p, c) for p, c, _ in rows]


# ---------------------------------------------------------------------------
# 待机展示：页面刚打开、还没生成方案时，右侧展示一条「缓慢左移」的实景图带，
# 避免「目的地实景图册」空板块的突兀感。
#
# 早期版本用 gr.Timer 每几秒整批换图，两个毛病：
#   1) 生硬跳变，不像「场景在流动」；
#   2) 换图与点击常撞在同一瞬间，用户会「点到的其实已是下一张」。
# 现在改为**纯 CSS 无缝横向滚动**：整个图带是一个放大的数据集在匀速平移，
# 图片位置连续变化，用户看到哪张点哪张。前端不再有任何会替换数据的定时器。
# ---------------------------------------------------------------------------

# 图带抽多少张；None = 每个行政区各出 1 张（全量，覆盖 34 个区）。
# 全量是刻意的选择：图带按「区」去重，用户转一圈就能看到全国，
# 而不是抽 9 张、平均只覆盖 8 个区、要靠运气转十几轮才碰得完。
STRIP_COUNT = None

# 图带里每张图的最大边长（像素）。图带上单张只显示 190×210，
# 却内嵌 1000px 原图（平均 126KB）纯属浪费 —— 压到 380px 宽后单张约 30KB，
# 34 张全上的载荷从 8.4MB 降到 3.1MB（比原来 9 张原图的 2.1MB 只多一点，
# 覆盖面却从 8 个区变成 34 个区）。改这个数是体积的主旋钮。
STRIP_IMAGE_MAX_W = 380
STRIP_IMAGE_QUALITY = 78

STRIP_ITEM_H = 210

# 滚动一整条（即 copy 一份）所需秒数；越大越慢。
# 34 张的图带单份宽 6800px，260 秒 ≈ 26 px/s，是舒服的「缓慢流动」速度
# （原来 9 张 110 秒只有 16 px/s，同样慢但一转就到底、显得很急）。
STRIP_LOOP_SECONDS = 260

# 图带用到了哪些文件（相对 assets/images），供 /config 前把本地图拷进 Gradio
# 可服务的临时目录用
_STRIP_FILES = []

# 手机端适配：Gradio Gallery 的 columns 只认整数，
# 窄屏下 3 列会挤成「两行半」、图注全部省略号，所以用媒体查询在小屏改两列、
# 缩小图注字号，并把图册高度让位给屏幕（桌面端完全不受影响）。
MOBILE_CSS = """
/* 待机图带的承载槽本身不加任何框，只当挂载点 */
#travel-strip-slot {
  display: block;
}
#travel-strip-box:empty {
  display: none;
}

/* ---- 待机图带：缓慢左移 + 无缝循环 ---- */
.travel-strip {
  overflow: hidden;
  height: 250px;                 /* 单张 210 + 图注约 36，一行放得下 */
  border-radius: 10px;
  background: #f6f7f9;
  padding: 8px 0;
}
/* 轨道：一行装两组（每组就是完整一份卡片），整体匀速左移 */
.travel-strip .track {
  display: flex;
  flex-wrap: nowrap;             /* 关键：绝不能换行，否则高度会爆 */
  width: max-content;
  gap: 0;                        /* 组间不能有 gap，否则 50% 定位会差一个 gap */
  padding: 0;
  animation-name: travel-strip-move;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  will-change: transform;
}
/* 一组 = 一份完整卡片；组内才有 gap，组尾用 margin 补齐接缝处的间距 */
.travel-strip .grp {
  display: flex;
  flex-wrap: nowrap;
  gap: 10px;
  flex: 0 0 auto;
  padding-right: 10px;           /* 补上组与组之间的那个 gap */
}
@keyframes travel-strip-move {
  from { transform: translateX(0); }
  to   { transform: translateX(-50%); }   /* 恰好走完一组，第二组正好落到原位 */
}
.travel-strip .card {
  position: relative;
  flex: 0 0 auto;
  width: 190px;
  margin: 0;
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  background: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.14);
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.travel-strip .card:hover {
  transform: translateY(-3px);
  box-shadow: 0 6px 14px rgba(0, 0, 0, 0.2);
}
.travel-strip .card img,
.travel-strip .card .ph {
  display: block;
  width: 190px;
  height: 210px;
  object-fit: cover;
}
.travel-strip .cap {
  font-size: 12px;
  line-height: 1.3;
  padding: 5px 7px;
  color: #374151;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.travel-strip .strip-empty {
  padding: 40px 16px;
  text-align: center;
  color: #6b7280;
  font-size: 13px;
}

/* ---- 往返路费卡：把「路费怎么算、预算扣不扣得下」摆在最显眼处 ---- */
#fare-card-slot { display: block; }
#fare-card-box:empty { display: none; }

.fare-card {
  border-radius: 10px;
  padding: 12px 14px;
  margin: 0 0 10px 0;
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-left: 6px solid #94a3b8;   /* 左侧粗色条：一眼分辨状态 */
  box-shadow: 0 1px 4px rgba(0, 0, 0, .07);
  font-size: 13.5px;
  line-height: 1.65;
  color: #1f2937;
}
.fare-card.fare-ok    { border-left-color: #16a34a; background: #f0fdf4; }
.fare-card.fare-tight { border-left-color: #f59e0b; background: #fffbeb; }
.fare-card.fare-over  { border-left-color: #dc2626; background: #fef2f2; }

.fare-card .fare-head { font-size: 15.5px; font-weight: 700; color: #0f172a; margin-bottom: 8px; }
.fare-card .fare-route { color: #1d4ed8; }
.fare-card .fare-dist { float: right; font-weight: 400; font-size: 12.5px; color: #64748b; }

.fare-card .fare-modes { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0 10px 0; }
.fare-card .fare-mode {
  flex: 1 1 140px; min-width: 132px;
  background: #ffffff; border: 1px solid #e5e7eb;
  border-radius: 8px; padding: 7px 10px;
}
.fare-card .fare-mode .m-name { font-size: 12.5px; color: #475569; white-space: nowrap; }
.fare-card .fare-mode .m-val { font-size: 15px; font-weight: 700; color: #b45309; margin-top: 2px; }
.fare-card .fare-mode .m-unit { font-size: 12px; font-weight: 400; color: #6b7280; }
.fare-card .fare-mode .m-sub { font-size: 11.5px; color: #94a3b8; margin-top: 2px; }

/* 本次实际用于核算的方式：描边高亮，避免「三个价格摆一起，不知道按哪个扣的」
   机票：虚线弱化 + 「仅参考 · 不可选」，明示它不参与预算。 */
.fare-card .fare-mode.m-on {
  background: #fffbeb; border: 1.5px solid #f59e0b;
  box-shadow: 0 1px 4px rgba(245, 158, 11, .18);
}
.fare-card .fare-mode .m-tag {
  display: inline-block; font-size: 10.5px; font-weight: 600;
  padding: 1px 5px; border-radius: 999px; margin-left: 4px;
  vertical-align: 1px; white-space: nowrap;
}
.fare-card .fare-mode .m-tag-on { background: #f59e0b; color: #fff; }
.fare-card .fare-mode .m-tag-ref { background: #f1f5f9; color: #64748b; }

.fare-card .fare-verdict {
  font-size: 14.5px; font-weight: 600; color: #0f172a;
  padding: 8px 10px; border-radius: 8px; background: rgba(255, 255, 255, .72);
}
.fare-card .fare-calc { margin-top: 6px; padding: 0 2px; color: #334155; }
.fare-card .fare-calc b { color: #0f172a; font-weight: 700; }
.fare-card.fare-tight .fare-calc b,
.fare-card.fare-over  .fare-calc b { color: #b91c1c; }
.fare-card .fare-tip {
  margin-top: 6px; font-size: 12.5px; color: #92400e;
  background: rgba(251, 191, 36, .16); border-radius: 6px; padding: 6px 9px;
}
.fare-card .fare-body { color: #334155; }
.fare-card .fare-foot { margin-top: 8px; font-size: 11.5px; color: #94a3b8; line-height: 1.5; }

/* ---- 行李清单卡：规则即时生成，蓝色系与路费卡区分 ---- */
#pack-slot { display: block; }
#pack-box:empty { display: none; }
.pack-card {
  border-radius: 10px; padding: 12px 14px; margin: 10px 0;
  background: #f0f9ff; border: 1px solid #e0f2fe; border-left: 6px solid #0ea5e9;
  font-size: 13.5px; line-height: 1.7; color: #1f2937;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .07);
}
.pack-card .pk-groups { display: flex; flex-wrap: wrap; gap: 10px; }
.pack-card .pk-group {
  flex: 1 1 220px; background: #ffffff; border: 1px solid #e0f2fe;
  border-radius: 8px; padding: 8px 12px;
}
.pack-card .pk-gname { font-weight: 700; font-size: 13px; color: #0369a1; margin-bottom: 4px; }
.pack-card ul { margin: 0; padding-left: 18px; }
.pack-card li { margin: 2px 0; }

/* ---- 日历导出反馈 ---- */
#ics-slot { display: block; }
#ics-box:empty { display: none; }
#ics-box { font-size: 13px; line-height: 1.6; color: #334155; margin: 2px 0 6px; }
#ics-file { margin: -2px 0 6px; }

/* ---- 预算去向堆叠条 ---- */
#bbar-slot { display: block; }
#bbar-box:empty { display: none; }
.bbar-card {
  border-radius: 10px; padding: 12px 14px; margin: 10px 0;
  background: #ffffff; border: 1px solid #e5e7eb; border-left: 6px solid #6366f1;
  font-size: 13.5px; color: #1f2937; box-shadow: 0 1px 4px rgba(0, 0, 0, .07);
}
.bbar-card .bb-bar {
  display: flex; height: 18px; border-radius: 9px; overflow: hidden;
  margin: 8px 0; background: #f1f5f9;
}
.bbar-card .bb-seg { height: 100%; min-width: 2px; }
.bbar-card .bb-legend { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 4px; }
.bbar-card .bb-item { font-size: 12.5px; color: #475569; }
.bbar-card .bb-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 3px;
  margin-right: 5px; vertical-align: -1px;
}

/* ---- 弹性保障卡：紫色系，回答「行程排死了怎么办」，与行李清单（蓝）区分 ---- */
#flex-slot { display: block; }
#flex-box:empty { display: none; }
.flex-card {
  border-radius: 10px; padding: 12px 14px; margin: 10px 0;
  background: #faf5ff; border: 1px solid #f3e8ff; border-left: 6px solid #7c3aed;
  font-size: 13.5px; line-height: 1.7; color: #1f2937;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .07);
}
.flex-card .fx-slogan { float: right; font-weight: 400; font-size: 12px; color: #7c3aed; }
.flex-card .fx-headline {
  background: #ffffff; border: 1px solid #f3e8ff; border-radius: 8px;
  padding: 7px 10px; margin: 2px 0 10px 0;
  font-weight: 600; color: #5b21b6; font-size: 13px;
}
.flex-card .fx-cols { display: flex; flex-wrap: wrap; gap: 10px 16px; }
.flex-card .fx-col { flex: 1 1 300px; min-width: 0; }
.flex-card .fx-title {
  font-weight: 700; font-size: 13px; color: #6d28d9;
  margin: 9px 0 5px 0; padding-bottom: 3px; border-bottom: 1px dashed #e9d5ff;
}
.flex-card .fx-col > .fx-title:first-child { margin-top: 0; }
.flex-card .fx-risk {
  background: #ffffff; border-radius: 7px; padding: 6px 9px; margin-bottom: 6px;
  border-left: 3px solid #a78bfa;
}
.flex-card .fx-risk.fx-high { border-left-color: #dc2626; background: #fffafa; }
.flex-card .fx-risk.fx-mid  { border-left-color: #f59e0b; background: #fffdf6; }
.flex-card .fx-risk.fx-low  { border-left-color: #94a3b8; background: #fbfcfe; }
.flex-card .fx-rname { font-weight: 700; font-size: 12.5px; color: #1e293b; }
.flex-card .fx-rtext { color: #475569; font-size: 12.5px; }
.flex-card .fx-buffer {
  background: #ffffff; border: 1px solid #f3e8ff; border-radius: 7px; padding: 7px 10px;
}
.flex-card .fx-buffer b { color: #7c3aed; }
.flex-card .fx-why { color: #94a3b8; font-size: 11.5px; margin-top: 3px; }
.flex-card .fx-order, .flex-card .fx-prep { margin: 0; padding-left: 0; list-style: none; }
.flex-card .fx-order li { margin: 3px 0; }
.flex-card .fx-order li b { color: #5b21b6; }
.flex-card .fx-prep li { margin: 3px 0; padding-left: 13px; position: relative; }
.flex-card .fx-prep li:before {
  content: '·'; position: absolute; left: 3px; color: #a78bfa; font-weight: 700;
}
.flex-card .fx-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.flex-card .fx-tag {
  background: #f5f3ff; border: 1px solid #e9d5ff; color: #6d28d9;
  border-radius: 20px; padding: 2px 9px; font-size: 12px;
}

/* ---- 页首横幅：标题 + 一句话说明 + 能力徽章 ---- */
#hero-slot { display: block; }
.hero {
  border-radius: 14px;
  padding: 18px 22px 15px;
  background: linear-gradient(118deg, #eef2ff 0%, #eff6ff 45%, #ecfeff 100%);
  border: 1px solid #e0e7ff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .05);
  margin-bottom: 8px;
}
.hero .hero-title {
  font-size: 25px; font-weight: 800; color: #1e293b;
  letter-spacing: .5px; line-height: 1.25;
}
.hero .hero-sub { margin-top: 5px; color: #475569; font-size: 13.5px; }
.hero .hero-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.hero .hb {
  background: rgba(255, 255, 255, .85); border: 1px solid #dbe3f8;
  border-radius: 999px; padding: 2.5px 11px; font-size: 12px;
  color: #4338ca; white-space: nowrap;
}

/* ---- 「没想好去哪儿」直达面板：常驻右栏底部，待机时补住空白 ---- */
#explore-slot { display: block; }
.xp-card {
  border-radius: 10px; padding: 13px 15px; margin-top: 12px;
  background: #ffffff; border: 1px solid #e5e7eb; border-left: 6px solid #10b981;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .07);
}
.xp-card .xp-head { font-size: 15px; font-weight: 700; color: #065f46; margin-bottom: 9px; }
.xp-card .xp-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.xp-card .xp-chip {
  cursor: pointer; flex: 1 1 128px; max-width: 190px; box-sizing: border-box;
  background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 9px;
  padding: 7px 11px;
  transition: transform .15s ease, border-color .15s ease, background .15s ease;
}
.xp-card .xp-chip:hover {
  border-color: #10b981; background: #ecfdf5; transform: translateY(-2px);
  box-shadow: 0 3px 8px rgba(16, 185, 129, .15);
}
.xp-card .xp-chip .r { font-weight: 700; font-size: 13.5px; color: #1f2937; }
.xp-card .xp-chip .s { font-size: 11.5px; color: #64748b; margin-top: 1px; }

/* 尊重系统的「减少动态效果」偏好 */
@media (prefers-reduced-motion: reduce) {
  .travel-strip .track { animation: none; }
}

@media (max-width: 640px) {
  .grid-container {
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
  }
  .grid-container .caption {
    font-size: 11px !important;
    padding: 3px 6px !important;
  }

  /* 图册（生成方案后出现）：原来给 .grid-wrap 压到 58vh + 组件固定 640px 高，
     实机（老叶手机）表现为「最后一排被拦腰截断 + 组件下方一大片留白」——
     截断是内部滚动区切的，留白是 640px 外框减去 58vh 视口剩下的。
     改成整册高度自适应：手机端不再有内部滚动，页面自然往下滚。 */
  #dest-gallery { height: auto !important; }
  #dest-gallery .grid-wrap {
    height: auto !important;
    max-height: none !important;
    overflow: visible !important;
  }
  #dest-gallery .grid-container {
    grid-template-rows: none !important;   /* 行高交给内容（缩略图 aspect-ratio 定方格） */
    grid-auto-rows: auto !important;
  }

  /* 待机图带：窄屏缩小卡片与高度（滚动速度基本跟桌面一致，屏幕窄看得少而已） */
  .travel-strip { height: 202px !important; padding: 6px 0 !important; }
  .travel-strip .track { animation-duration: 240s !important; }
  .travel-strip .grp { gap: 8px !important; padding-right: 8px !important; }
  .travel-strip .card { width: 148px !important; }
  .travel-strip .card img,
  .travel-strip .card .ph { width: 148px !important; height: 166px !important; }
  .travel-strip .cap { font-size: 11px !important; padding: 4px 6px !important; }

  /* 路费卡：窄屏收紧字号，交通方式卡竖排（避免挤成两行小字） */
  .fare-card { font-size: 12.5px !important; padding: 10px 11px !important; margin-bottom: 8px !important; }
  .fare-card .fare-head { font-size: 14px !important; }
  .fare-card .fare-dist { float: none !important; display: block !important; }
  .fare-card .fare-mode { flex: 1 1 100% !important; }
  .fare-card .fare-mode .m-val { font-size: 14px !important; }
  .fare-card .fare-verdict { font-size: 13.5px !important; }
  .pack-card, .bbar-card { font-size: 12.5px !important; }
  .pack-card .pk-group { flex: 1 1 100% !important; }
  .flex-card { font-size: 12.5px !important; padding: 10px 11px !important; }
  .flex-card .fx-col { flex: 1 1 100% !important; }
  .flex-card .fx-slogan { display: none !important; }
  .flex-card .fx-tag { font-size: 11.5px !important; }

  /* 页首横幅与直达面板：窄屏收紧 */
  .hero { padding: 14px 15px 12px !important; }
  .hero .hero-title { font-size: 20px !important; }
  .hero .hero-sub { font-size: 12.5px !important; }
  .hero .hb { font-size: 11px !important; padding: 2px 9px !important; }
  .xp-card { padding: 11px 12px !important; }
  .xp-card .xp-chip { flex: 1 1 44% !important; max-width: none !important; }
}

/* ---------------------------------------------------------------------------
   行程时间轴（core/render.build_timeline_html 生成，生成方案后出现在正文下方）
   --------------------------------------------------------------------------- */
.tl-card {
  margin: 14px 0 4px; padding: 15px 16px 13px;
  background: linear-gradient(180deg, #f0fdfa, #ffffff 62%);
  border: 1px solid #99f6e4; border-radius: 14px;
}
.tl-card .tl-head {
  font-size: 15px; font-weight: 700; color: #0f766e; margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.tl-card .tl-count {
  font-size: 11.5px; font-weight: 500; color: #0d9488;
  background: #ccfbf1; border-radius: 999px; padding: 2px 10px;
}
.tl-card .tl-list { display: flex; flex-direction: column; gap: 12px; }
.tl-card .tl-day {
  display: flex; gap: 12px; align-items: flex-start;
  background: #ffffff; border: 1px solid #e5e7eb; border-radius: 11px;
  padding: 11px 13px;
}
.tl-card .tl-badge {
  flex: 0 0 auto; font-size: 13px; font-weight: 700; color: #ffffff;
  background: #0d9488; border-radius: 9px; padding: 6px 12px;
  margin-top: 2px; letter-spacing: .5px;
}
.tl-card .tl-body { flex: 1 1 auto; min-width: 0; }
.tl-card .tl-sub {
  font-size: 12.5px; font-weight: 600; color: #0f766e;
  margin-bottom: 6px; letter-spacing: .2px;
}
.tl-card .tl-stops { display: flex; flex-direction: column; }
.tl-card .tl-empty { color: #9ca3af; font-size: 12.5px; }
.tl-card .tl-stop { display: flex; align-items: center; gap: 9px; }
.tl-card .tl-dot {
  flex: 0 0 9px; width: 9px; height: 9px; border-radius: 50%;
  background: #14b8a6; box-shadow: 0 0 0 3px #ccfbf1;
}
.tl-card .tl-name { font-size: 13.5px; color: #1f2937; line-height: 1.45; }
.tl-card .tl-link {
  width: 2px; height: 12px; background: #99f6e4;
  margin-left: 3.5px; border-radius: 1px;
}
.tl-card .tl-meta { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 9px; }
.tl-card .tl-tag {
  font-size: 11.5px; border-radius: 7px; padding: 3px 9px; line-height: 1.4;
}
.tl-card .tl-buf { color: #854d0e; background: #fef9c3; }
.tl-card .tl-bak { color: #1e40af; background: #dbeafe; }

@media (max-width: 640px) {
  .tl-card { padding: 12px 11px 10px !important; }
  .tl-card .tl-day { padding: 10px 10px !important; gap: 9px !important; }
  .tl-card .tl-badge { font-size: 12px !important; padding: 5px 9px !important; }
  .tl-card .tl-name { font-size: 12.5px !important; }
}
"""


def show_pick_hint(evt: gr.EventData):
    """图带点击 → 回填目的地并给提示。

    行政区由前端从卡片的 `data-region` 直接读出（不经过任何会漂移的状态），
    再经 `trigger('select', {region: ...})` 作为**事件数据**送到这里，
    从 `evt.region` 取。只做白名单校验；认不出来就什么都不填。

    ⚠️ 两个坑，都是真机实测踩出来的（改之前请先读完）：

    1) **绝不能用 `strip_out.select(region)`。** `js_on_load` 的代码是被
       `new Function("element","trigger","props","server","upload","watch", code)`
       执行的，作用域里**只有这 7 个参数**；`strip_out` 是 Python 侧的变量名，
       前端一概不知。那样写会抛 `ReferenceError: strip_out is not defined`，
       而且抛在 click 回调内部 —— 表现就是「点了完全没反应」。
       官方唯一姿势是 `trigger(事件名, 数据对象)`。

    2) **不要写 `inputs=[strip_out]`。** 那会让 Gradio 把图带组件的**当前值**
       （整条图带，约 3.9MB 的 HTML）当成参数回传服务端：既白白上传几 MB，
       也让这里拿到的是一坨 HTML 而不是行政区。改用 `gr.EventData` 收事件数据、
       `inputs` 留空之后，请求体只有几十个字节。

    本事件仍须 `api_visibility="undocumented"`：它是纯前端内部端点，不该进
    「Use via API」文档（否则文档示例会把大段内容复制若干份，撑大主页）。
    """
    name = str(getattr(evt, "region", "") or "").strip()
    if name not in _region_names():
        return gr.update(), gr.update()
    return name, "👆 已根据你选中的图片，把目的地填为「%s」，点击「生成旅游方案」即可出发~" % name


_thumb_cache = {}
_thumb_lock = threading.Lock()


def _image_data_uri(path, max_w=None, quality=None):
    """把本地图片读成 base64 data URI，直接嵌进 HTML。

    图片若用相对/静态 URL，前端拿到 HTML 后会去 `/gradio_api/file=...` 取图，
    任何路径或权限问题都会变成一排断图。图带只有几十张，直接内嵌最稳。

    传了 max_w 就先等比缩到该宽度再压 JPEG —— 图带上单张只显示 190px 宽，
    内嵌 1000px 原图毫无意义，纯属让页面体积翻几倍。压缩结果按
    (路径, 宽度, 质量) 缓存，避免每次重建图带都重新解码几十张图。
    """
    cache_key = (path, max_w, quality)
    with _thumb_lock:
        hit = _thumb_cache.get(cache_key)
    if hit is not None:
        return hit

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""

    mime = os.path.splitext(path)[1].lower().lstrip(".") or "jpeg"
    if mime == "jpg":
        mime = "jpeg"

    if max_w:
        try:
            from PIL import Image
            import io as _io
            im = Image.open(_io.BytesIO(raw))
            im = im.convert("RGB")
            if im.width > max_w:
                h = max(1, int(im.height * max_w / float(im.width)))
                im = im.resize((max_w, h), Image.LANCZOS)
            buf = _io.BytesIO()
            im.save(buf, "JPEG", quality=quality or 80, optimize=True,
                    progressive=True)
            raw = buf.getvalue()
            mime = "jpeg"
        except Exception:
            pass        # 压缩失败就退回原图，绝不因为缩图挂了整条图带

    uri = "data:image/%s;base64,%s" % (mime,
                                       base64.b64encode(raw).decode("ascii"))
    with _thumb_lock:
        _thumb_cache[cache_key] = uri
    return uri


def _escape(text):
    """图注进 HTML 属性/正文前转义，避免景点名里的符号截断结构"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_strip_seq = 0


def build_strip(count=None, pool=None, order="shuffle"):
    """拼一条「缓慢左移、无缝循环」的实景图带 HTML。

    抽图规则：**按行政区去重，每个区最多 1 张**。默认（count=None）是
    「每个区各 1 张」全量上——34 个区就是 34 张，用户转一圈看到的是全国，
    而不是「抽 9 张、平均只覆盖 8 个区、剩下 26 个要靠运气」。这是刻意的取舍：
    图带的目的就是让人一眼看出「这里什么都有」，不是随机放几张。

    无缝的做法：把同一批卡片复制两份首尾相接，动画从 0 平移到 -50%；
    第一份移出可视区时第二份正好补位，于是永远在流动、不会跳回起点。
    注意两点（都实测过，改错就跳变）：
      1) 两份之间**不能有 gap**，否则 50% ≠ 单份宽度，循环时跳 5~10px；
      2) 绝不能 flex-wrap:wrap，否则卡片折行、高度暴涨被容器裁掉。

    base64 图片只写**一次**（放在一个隐藏的 `<svg>` 里，每张一个 `<symbol id>`），
    补位那份用 `<use href="#id">` 引用 —— 否则同一张图内嵌两遍，体积白白翻倍。
    """
    global _STRIP_FILES, _strip_seq

    # 按区归拢候选，每区一条列表
    if pool is None:
        by_region = OrderedDict()
        for entry in _DEST_INDEX:
            region = entry.get("region") or "?"
            items = gallery_items_for(entry, with_tags=False)
            if items and region not in by_region:
                by_region[region] = list(items)
        pool_by_region = by_region
    else:
        pool_by_region = OrderedDict()
        for path, cap in pool:
            pool_by_region.setdefault(cap.split("·")[0].strip(), []).append((path, cap))

    if not pool_by_region:
        return "<div class='travel-strip'><div class='strip-empty'>暂无实景图</div></div>"

    regions = list(pool_by_region.keys())
    if order == "shuffle":
        random.shuffle(regions)

    if count is None:
        chosen_regions = regions                   # 全量：每个区都上
    else:
        chosen_regions = regions[:max(1, int(count))]

    picks = [random.choice(pool_by_region[r]) for r in chosen_regions]

    _STRIP_FILES = list(picks)

    _strip_seq += 1
    gid = _strip_seq                      # 用序号保证 id 不与页面上其它元素冲突

    def one_card(i, cap, uri=None, ref=None):
        region = _escape(cap.split("·")[0].strip())
        cap_e = _escape(cap)
        if uri:
            img = "<img src='%s' alt='%s' loading='lazy'>" % (uri, cap_e)
        else:
            img = "<svg class='ph' viewBox='0 0 190 210' role='img' aria-label='%s'>" \
                  "<use href='#stripimg-%d-%d'></use></svg>" % (cap_e, gid, i)
        return ("<figure class='card' data-region='%s'>%s"
                "<figcaption class='cap'>%s</figcaption></figure>"
                % (region, img, cap_e))

    bank = []        # 图片本体（每张只有一份）
    originals = []   # 第一份：直接用 <img>
    clones = []      # 第二份：用 <use> 引用，不重复内嵌数据
    for i, (path, cap) in enumerate(picks):
        uri = _image_data_uri(path, STRIP_IMAGE_MAX_W, STRIP_IMAGE_QUALITY)
        if not uri:
            continue
        bank.append("<symbol id='stripimg-%d-%d'>"
                    "<image href='%s' width='190' height='210'"
                    " preserveAspectRatio='xMidYMid slice'/></symbol>"
                    % (gid, i, uri))
        originals.append(one_card(i, cap, uri=uri))
        clones.append(one_card(i, cap, ref=i))

    if not originals:
        return "<div class='travel-strip'><div class='strip-empty'>暂无实景图</div></div>"

    # 无缝的关键在这里，务必别改回「一个 track 里塞两份」的写法：
    #   transform:translateX(-50%) 的百分比是相对**元素自身宽度**算的，
    #   所以「track 宽度的一半」必须**恰好等于一组卡片的宽度**。
    #   若把两份卡片直接摊在同一个 flex（gap:10px）里，两个半份之间会多出
    #   一个 gap，于是 50% ≠ 单组宽度，循环到接缝那一瞬间会跳 5~10px。
    #   实测（1000px 视口、9 张 190px 卡）：摊平写法 track=3590/一半=1795，
    #   单组只有 1790 → 误差 5px；而下面「两组 + 组内 gap」的写法误差为 0。
    #   另外 track 必须 flex-wrap:nowrap 且宽度 max-content：一旦允许换行，
    #   卡片会被折成多行、高度暴涨（实测 2115px，几乎全被容器裁掉）。
    return (
        "<svg class='bank' width='0' height='0' aria-hidden='true'"
        " style='position:absolute'>%s</svg>"
        "<div class='travel-strip'>"
        "<div class='track' style='animation-duration:%ds'>"
        "<div class='grp'>%s</div>"
        "<div class='grp' aria-hidden='true'>%s</div>"
        "</div></div>" % ("".join(bank), STRIP_LOOP_SECONDS,
                          "".join(originals), "".join(clones))
    )


_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|avif)$", re.I)


def _extract_region(text):
    """从任意文本（图片路径 / 图注）里认出行政区名，认不出返回空串。

    图片路径形如 `assets/images/省/西藏/1-布达拉宫.jpg`，目录名就是行政区；
    轮播图注形如「西藏 · 布达拉宫」，前缀也是行政区。两条通道互为兜底。
    """
    if not text:
        return ""
    s = str(text)
    hits = [n for n in _region_names() if n in s]
    if not hits:
        return ""
    hits.sort(key=len, reverse=True)          # 万一命中多个，取最具体的那个
    return hits[0]


def _unpack_pick(obj):
    """把事件里的一个候选对象拆成 (图片路径, 图注)；格式不认识就给空值。"""
    if isinstance(obj, dict):
        img = obj.get("image") if isinstance(obj.get("image"), dict) else obj
        cap = obj.get("caption") or img.get("caption")
        path = img.get("path") or img.get("url") or ""
        return (path if isinstance(path, str) else "",
                cap if isinstance(cap, str) else "")
    if isinstance(obj, (list, tuple)):
        p = obj[0] if len(obj) > 0 else ""
        c = obj[1] if len(obj) > 1 else ""
        return (p if isinstance(p, str) else "", c if isinstance(c, str) else "")
    if isinstance(obj, str):
        return (obj, None)
    return ("", None)


def _pick_candidates(evt, items):
    """按「最像用户真正点中的那张图」的顺序，产出候选 (路径, 图注)。

    只用 select 事件自带的数据；gr.State 里的图册列表可能已被换掉，
    所以放在最后兜底 —— 「点西藏却填了贵州」就是这么来的。
    """
    value = getattr(evt, "value", None)
    idx = getattr(evt, "index", None)
    seq = []
    if isinstance(value, (list, tuple)):
        first = value[0] if value else None
        single = len(value) == 2 and isinstance(first, str) and _IMG_EXT_RE.search(first)
        if single:                                  # 就是 (路径, 图注) 本身
            seq.append(value)
        elif isinstance(idx, int) and 0 <= idx < len(value):
            seq.append(value[idx])                  # 整个图册 → 取被点的那一项
        else:
            seq.extend(list(value)[:12])
    elif value is not None:
        seq.append(value)
    if isinstance(items, list) and isinstance(idx, int) and 0 <= idx < len(items):
        seq.append(items[idx])                      # 最后兜底：可能已过期
    for obj in seq:
        yield _unpack_pick(obj)


def on_image_pick(evt: gr.SelectData, current_region=""):
    """点选正式图册 → 把对应行政区填进「目的地」输入框。

    行政区一律过「行政区白名单」校验，认不出来就什么都不填
    （宁可不动，也绝不填错）。

    图册的图注形如「景点 ｜ 兴趣」，**不带省份**；事件里的图片路径线上还会
    变成 `/tmp/gradio/<hash>/xxx.jpg`（不含行政区目录）。所以主通道是图册自身
    的 `gallery_region` 状态——它就是这份图册对应的行政区。

    ⚠️ 这里原本还有一条「按 `evt.index` 去 `_STRIP_FILES` 取行政区」的兜底，
    本意是给待机图带用。待机图带改走 `show_pick_hint` 之后，那条兜底就只剩
    图册在用：点图册第 N 张时会拿**图带第 N 张**的省份填进去，与图册内容毫无
    关系——实测「点图册第 5 张」被填成「内蒙古」。
    已删除：按下标取值只对「构建后不再变的静态列表」成立，gr.Gallery 不满足，
    这正是「点西藏却填贵州」的病根，别再往回加。
    """
    def ok(name):
        name = str(name or "").strip()
        return name if name in _region_names() else ""

    # ① 事件里那张图的路径 / 图注若直接带省份，优先用它
    region = ""
    for path, cap in _pick_candidates(evt, None):
        region = _extract_region(path) or _extract_region(cap)
        if region:
            break
    # ② 主通道：图册自身的行政区状态
    if not region:
        region = ok(current_region)
    if not region:
        return gr.update(), gr.update()
    return region, "👆 已根据你选中的图片，把目的地填为「%s」，点击「生成旅游方案」即可出发~" % region


# ---------------------------------------------------------------------------
# 出发地 → 目的地「往返路费」：把城际大交通纳入预算口径
#
# 需求（老叶原话）：「加上初始地到旅游地的路费，预算够的就在预算里减，
# 预算不够就单独算，但要和顾客说明白，说在醒目的地方」。
#
# 落地成三态判定（算法见 core/transport.py:_plan）：
#   ok    预算够  → 路费从预算里扣，并说明扣完还剩多少
#   tight 偏紧    → 路费装得下，但目的地部分要压缩，给出建议加多少钱
#   over  不够    → 路费本身就超出预算，单独列支，给出要另外准备的总额
#
# 「出发地」是**选填**：不填就不显示这张卡，原有体验完全不变。
# ---------------------------------------------------------------------------
def _fare_html(info):
    """把路费估算渲染成一张醒目卡片；返回空串表示「不该显示」。"""
    if not info.get("ok"):
        if info.get("reason") != "unresolved":
            return ""
        return (
            "<div class='fare-card fare-note'>"
            "<div class='fare-head'>💸 往返路费</div>"
            "<div class='fare-body'>没能识别「%s」或「%s」是哪里。"
            "出发地 / 目的地请填城市名或省份名（如「北京」「成都」「四川」）。</div>"
            "</div>" % (_escape(info.get("origin", "")), _escape(info.get("dest", "")))
        )

    head = "💸 往返路费估算 ｜ <span class='fare-route'>%s → %s</span>" % (
        _escape(info["origin"]), _escape(info["dest"]),
    )
    if info.get("air_km"):
        head += "<span class='fare-dist'>直线约 %d 公里</span>" % info["air_km"]

    if info.get("same_city"):
        # 出发地填「成都」、目的地填「四川」时，两边解析到同一座城市。
        # 直接把标题写成「成都 → 成都」会让人摸不着头脑，这里用用户原话说明白。
        oi = _escape(info.get("origin_input") or info["origin"])
        di = _escape(info.get("dest_input") or info["dest"])
        if oi == di:
            msg = ("出发地和目的地都是「%s」，往返路费按 <b>0 元</b> 计，预算不用扣。"
                   % oi)
        else:
            msg = ("出发地「%s」和目的地「%s」其实指向同一座城市（%s），"
                   "往返路费按 <b>0 元</b> 计，预算不用扣。"
                   % (oi, di, _escape(info["origin"])))
        return (
            "<div class='fare-card fare-ok'>"
            "<div class='fare-head'>💸 往返路费估算 ｜ "
            "<span class='fare-route'>%s → %s</span></div>"
            "<div class='fare-verdict'>%s</div>"
            "</div>" % (oi, di, msg)
        )

    plan = info.get("plan") or {}
    state = plan.get("state", "unknown")
    cls = {"ok": "fare-ok", "tight": "fare-tight", "over": "fare-over"}.get(state, "fare-note")

    chosen_key = info.get("mode") or ""
    chosen_name = info.get("mode_name") or ""

    # 分交通方式：三种都展示出来（高铁 / 普速 / 机票），
    # 但只有「可勾选」的方式能参与预算核算 —— 当前选中的那张卡高亮并打标「本次核算」，
    # 机票永远只是参考（波动太大，不能拿去扣预算），明确标「仅参考」。
    mode_html = []
    for m in info.get("modes", []):
        if m["low"] == m["high"]:
            val = "约 %d" % m["low"]
        else:
            val = "约 %d~%d" % (m["low"], m["high"])
        on = (m["key"] == chosen_key)
        tag = ""
        if on:
            tag = "<span class='m-tag m-tag-on'>✅ 本次核算</span>"
        elif m["key"] == "air":
            tag = "<span class='m-tag m-tag-ref'>仅参考 · 不可选</span>"
        mode_html.append(
            "<div class='fare-mode%s'>"
            "<div class='m-name'>%s %s%s</div>"
            "<div class='m-val'>%s <span class='m-unit'>元/人</span></div>"
            "<div class='m-sub'>往返 · 单程约 %.1f 小时</div>"
            "</div>" % (" m-on" if on else "", m["icon"], _escape(m["name"]), tag, val, m["hours"])
        )

    b = plan.get("budget", 0)
    fare = info["fare_low"]
    per_day = int(round((b - fare) / float(max(1, plan.get("days", 1)))))

    if state == "ok":
        verdict = (
            "<div class='fare-verdict'>✅ <b>预算够用，路费已从预算里扣掉</b></div>"
            "<div class='fare-calc'>"
            "人均预算 <b>%d 元</b> − 往返路费约 <b>%d 元</b>（按 <b>%s</b> 算）"
            "＝ 目的地可用 <b>%d 元/人</b>，"
            "%d 天里每天约 <b>%d 元</b>，高于经济型参考线 %d 元/天。"
            "</div>"
            % (b, fare, _escape(chosen_name), b - fare,
               plan.get("days", 0), per_day, plan.get("daily", 0))
        )
    elif state == "tight":
        suggest = b + plan.get("gap", 0)
        verdict = (
            "<div class='fare-verdict'>⚠️ <b>预算偏紧：按 %s 算，路费装得下，"
            "但目的地部分要压一压</b></div>"
            "<div class='fare-calc'>"
            "人均预算 <b>%d 元</b> − 往返路费约 <b>%d 元</b> ＝ 只剩 "
            "<b>%d 元/人</b> 用于 %d 天，每天约 <b>%d 元</b>，"
            "低于经济型参考线 %d 元/天。"
            "<br>建议：人均预算提到 <b>%d 元</b> 左右；或换成更省的方式"
            "（在卡片上方切换）、缩短天数。"
            "</div>"
            % (_escape(chosen_name), b, fare, b - fare, plan.get("days", 0), per_day,
               plan.get("daily", 0), suggest)
        )
    elif state == "over":
        verdict = (
            "<div class='fare-verdict'>🚨 <b>按 %s 算，路费超出预算，"
            "这笔钱要单独准备</b></div>"
            "<div class='fare-calc'>"
            "人均预算 <b>%d 元</b> ＜ 往返路费约 <b>%d 元/人</b>，"
            "人均还差 <b>%d 元</b>。"
            "<br>按 %d 人同行算，<b>光往返路费就要另外准备约 %d 元</b>。"
            "建议把路费单独列支——或换成更省的方式（在卡片上方切换）再算一遍。"
            "</div>"
            % (_escape(chosen_name), b, fare, fare - b,
               plan.get("people", 1), plan.get("extra_total", 0))
        )
    else:      # unknown：还没填预算
        verdict = (
            "<div class='fare-verdict'>按 <b>%s</b> 核算，往返路费约 "
            "<b>%d~%d 元/人</b>。"
            "填上左侧「人均预算」，这里会立刻算出它占多少、扣完还剩多少。</div>"
            % (_escape(chosen_name), info["fare_low"], info["fare_high"] or info["fare_low"])
        )

    tips = "".join("<div class='fare-tip'>⚠️ %s</div>" % _escape(t) for t in info.get("special", []))

    # 底部说明：把「能选什么 / 什么只是参考」讲清楚，别让人以为机票也能拿去扣预算
    if info.get("mode_fixed"):
        howto = ("该目的地无铁路直达，<b>只能乘飞机</b>往返，路费按机票估算；"
                 "机票受季节与班次影响波动较大，<b>仅供预算参考</b>。")
    else:
        only = [m["name"] for m in info.get("mode_options", [])]
        howto = ("可在卡片上方切换<b>高铁二等座 / 普速硬卧</b>，预算会按所选方式重算。")
        if len(only) == 1:
            howto = ("该目的地无高铁（%s），路费按<b>%s</b>核算。" % (
                "海南无跨海高铁" if info.get("dest_prov") == "海南" else
                "拉萨暂无高铁" if info.get("dest_prov") == "西藏" else "无铁路直达",
                _escape(only[0])))
        howto += "机票价格波动大，<b>仅作参考、不参与预算核算</b>。"

    return (
        "<div class='fare-card %s'>"
        "<div class='fare-head'>%s</div>"
        "<div class='fare-modes'>%s</div>"
        "%s%s"
        "<div class='fare-foot'>※ %s<br>"
        "※ 按城市直线距离与常见票价折算的区间估算，实际以购票平台票价为准。</div>"
        "</div>" % (cls, head, "".join(mode_html), tips, verdict, howto)
    )


def update_fare(origin, destination, budget, days, people, mode=DEFAULT_MODE):
    """表单 / 交通方式变化时即时刷新路费卡与方式单选项（纯本地计算，毫秒级返回）。

    返回 (方式单选项, 路费卡) 两个 update：
      · 单选项只在「有两种可选方式」时出现（高铁 / 普速）；海南、西藏没有跨海铁路，
        只剩一种就藏起来（卡片里照旧把三种都列出来）；
      · 机票永远不可选，所以不进单选项。
    """
    info = estimate_fare(origin, destination, budget, days, people, mode)
    html = _fare_html(info)

    options = info.get("mode_options") or []
    if not info.get("ok") or len(options) < 2:
        radio = gr.update(visible=False)
    else:
        choices = [MODE_LABELS[m["key"]] for m in options]
        cur = MODE_LABELS.get(info.get("mode") or "", choices[0])
        radio = gr.update(choices=choices, value=cur, visible=True)

    if not html:
        return radio, gr.update(value="", visible=False)
    return radio, gr.update(value=html, visible=True)


# ---------------------------------------------------------------------------
# 行李清单：纯规则生成（core/packing.py），填了目的地就即时显示
# ---------------------------------------------------------------------------
def _packing_html(info):
    groups = "".join(
        "<div class='pk-group'><div class='pk-gname'>%s</div><ul>%s</ul></div>"
        % (_escape(g), "".join("<li>%s</li>" % _escape(i) for i in items))
        for g, items in info.get("groups", [])
    )
    return (
        "<div class='pack-card'>"
        "<div class='fare-head'>🎒 行李清单 · %s（%s）</div>"
        "<div class='pk-groups'>%s</div>"
        "<div class='fare-foot'>※ %s</div>"
        "</div>" % (_escape(info["city"]), _escape(info["prov"]), groups, _escape(info["note"]))
    )


# ---------------------------------------------------------------------------
# 弹性保障卡：纯规则（core/flexible.py）
# 回答「行程排死了怎么办」——天气风险、每天留多少空、赶不上先舍什么、雨天改去哪儿
# ---------------------------------------------------------------------------
_FX_LEVEL_CLASS = {"high": "fx-high", "mid": "fx-mid", "low": "fx-low"}


def _flex_html(info):
    """把弹性保障渲染成一张卡；返回空串表示不该显示。"""
    if not info.get("ok"):
        return ""

    risks = "".join(
        "<div class='fx-risk %s'>"
        "<div class='fx-rname'>%s</div>"
        "<div class='fx-rtext'>%s</div>"
        "</div>"
        % (_FX_LEVEL_CLASS.get(w.get("level"), "fx-low"),
           _escape(w.get("title")), _escape(w.get("detail")))
        for w in info.get("weather", [])
    )

    buf = info["buffer"]
    why = info.get("buffer_why") or []
    buf_html = (
        "<div class='fx-buffer'>%s"
        "<div class='fx-why'>%s</div></div>"
        % (buf["text"],
           "依据：" + _escape("、".join(why)) if why else
           "依据：常规节奏留白，用来消化交通延误与临时排队")
    )

    order = "".join(
        "<li><b>%s</b>%s</li>" % (_escape(t), _escape(d))
        for t, d in info.get("fallback", [])
    )

    tags = "".join(
        "<span class='fx-tag'>%s</span>" % _escape(x) for x in info.get("indoor", [])
    )

    prep = "".join("<li>%s</li>" % _escape(x) for x in info.get("prepare", []))

    return (
        "<div class='flex-card'>"
        "<div class='fare-head'>🛡 弹性保障 · %s（%s）"
        "<span class='fx-slogan'>行程别排死 · 留活口</span></div>"
        "<div class='fx-headline'>%s</div>"
        "<div class='fx-cols'>"
        "<div class='fx-col'>"
        "<div class='fx-title'>🌦 可能被迫改行程的天气</div>%s"
        "<div class='fx-title'>🕐 每天留多少机动</div>%s"
        "</div>"
        "<div class='fx-col'>"
        "<div class='fx-title'>🔀 赶不上计划，按这个顺序舍</div>"
        "<ul class='fx-order'>%s</ul>"
        "<div class='fx-title'>☔ 天气不好改走这些（室内）</div>"
        "<div class='fx-tags'>%s</div>"
        "<div class='fx-title'>✅ 出发前先确认</div>"
        "<ul class='fx-prep'>%s</ul>"
        "</div>"
        "</div>"
        "<div class='fare-foot'>※ 天气部分是按「目的地气候规律 + 当月」给出的经验提示，"
        "不是实时预报；出发前一晚请再看一次目的地天气，据此微调当天顺序。</div>"
        "</div>"
        % (_escape(info["city"]), _escape(info["prov"]),
           _escape(info["headline"]), risks, buf_html, order, tags, prep)
    )


# 两张「行前准备卡」的视图构造：目的地 / 天数 / 人数 / 兴趣 任一变化就一起刷新。
# 合并成一次请求（一个事件 outputs 两个组件），避免同一批输入触发两次往返。
def _pack_view(destination, days, people, interests):
    info = build_packing(destination, days, interests, people)
    if not info.get("ok"):
        return gr.update(value="", visible=False)
    return gr.update(value=_packing_html(info), visible=True)


def _flex_view(destination, days, people, interests):
    info = build_flexible(destination, days, people, interests)
    html = _flex_html(info)
    if not html:
        return gr.update(value="", visible=False)
    return gr.update(value=html, visible=True)


def update_prep_cards(destination, days, people, interests):
    """即时刷新「行李清单 + 弹性保障」两张卡（纯规则，毫秒级，不调模型）。"""
    return _pack_view(destination, days, people, interests), \
        _flex_view(destination, days, people, interests)


# ---------------------------------------------------------------------------
# 预算明细条：生成结束后，从方案正文的「💰 预算」小节抠出分项画成堆叠条
# ---------------------------------------------------------------------------
_BUDGET_SEGS = [           # (方案里的关键词, 显示名, 颜色)
    ("门票", "门票", "#6366f1"),
    ("交通", "当地交通", "#0ea5e9"),
    ("住宿", "住宿", "#f59e0b"),
    ("餐饮", "餐饮", "#10b981"),
]
_FARE_SEG_COLOR = "#ef4444"      # 往返路费
_REST_SEG_COLOR = "#cbd5e1"      # 预算扣完还剩的


def _grab_budget_item(text, key):
    """抓「关键词 … 数字 [元] [/晚|/天]」。返回 (数值, 是否按晚/天计) 或 None。"""
    m = re.search(key + r"[^\d\n]{0,12}([0-9][0-9,，]*)", text)
    if not m:
        return None
    try:
        val = int(m.group(1).replace(",", "").replace("，", ""))
    except ValueError:
        return None
    tail = text[m.end():m.end() + 10]
    per_unit = bool(re.search(r"/\s*(晚|天|日)|每\s*(晚|天|日)", tail))
    return val, per_unit


def _budget_bar_html(md_text, fare_info, days, budget):
    """预算堆叠条；解析不出足够分项时返回空串（宁可不出图也不瞎画）。"""
    days = max(1, int(days or 1))
    nights = max(1, days - 1)
    segs = []
    if fare_info and fare_info.get("ok") and fare_info.get("fare_low"):
        segs.append(("往返路费", fare_info["fare_low"], _FARE_SEG_COLOR))
    picked = 0
    for key, label, color in _BUDGET_SEGS:
        got = _grab_budget_item(md_text, key)
        if not got:
            continue
        val, per_unit = got
        if per_unit and key == "住宿":
            val *= nights
        elif per_unit:
            val *= days
        if not (0 < val < 200000):
            continue
        segs.append((label, val, color))
        picked += 1
    if picked < 2:
        return ""

    total = sum(v for _, v, _ in segs)
    b = int(budget or 0)
    note = ""
    if b > 0:
        rest = b - total
        if rest > 0:
            segs.append(("预算剩余", rest, _REST_SEG_COLOR))
        elif rest < 0:
            note = "分项合计已超出人均预算约 %d 元，建议删减项目或提高预算。" % (-rest)

    width_total = sum(v for _, v, _ in segs) or 1
    bars, legend = [], []
    for label, val, color in segs:
        pct = val * 100.0 / width_total
        if pct <= 0:
            continue
        bars.append(
            "<div class='bb-seg' style='width:%.2f%%;background:%s' title='%s 约 %d 元'></div>"
            % (pct, color, _escape(label), val)
        )
        legend.append(
            "<span class='bb-item'><span class='bb-dot' style='background:%s'></span>"
            "%s <b>约 %d 元</b>（%.0f%%）</span>" % (color, _escape(label), val, pct)
        )
    head = "💰 预算去向（人均）" + (" ｜ 人均预算 <b>%d 元</b>" % b if b else "")
    return (
        "<div class='bbar-card'>"
        "<div class='fare-head'>%s</div>"
        "<div class='bb-bar'>%s</div>"
        "<div class='bb-legend'>%s</div>"
        "%s"
        "<div class='fare-foot'>※ 往返路费为系统估算；门票/住宿/餐饮/当地交通取自方案预算小节"
        "（住宿按 %d 晚、餐饮按 %d 天折算），均为约数。</div>"
        "</div>" % (head, "".join(bars), "".join(legend),
                    ("<div class='fare-tip'>⚠️ %s</div>" % note) if note else "",
                    nights, days)
    )


# ---------------------------------------------------------------------------
# 方案导出：Markdown / Word（HTML 版 .doc，WPS 与 Word 都能直接打开，零依赖）
# ---------------------------------------------------------------------------
_EXPORT_DIR = os.path.join(tempfile.gettempdir(), "travel_exports")

_DOC_TMPL = (
    '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
    'xmlns:w="urn:schemas-microsoft-com:office:word"><head>'
    '<meta charset="utf-8"><title>旅游方案</title>'
    "<style>body{font-family:'微软雅黑','Microsoft YaHei',sans-serif;font-size:11pt;"
    "line-height:1.75;color:#1f2937}h1{font-size:17pt;color:#1d4ed8}h2{font-size:14pt;"
    "border-bottom:1px solid #e5e7eb;padding-bottom:4px;margin-top:18px}h3{font-size:12pt}"
    "blockquote{background:#fffbeb;border-left:4px solid #f59e0b;margin:8px 0;"
    "padding:6px 12px}hr{border:none;border-top:1px solid #e5e7eb;margin:14px 0}"
    "li{margin:2px 0}table{border-collapse:collapse}td,th{border:1px solid #d1d5db;"
    "padding:4px 8px;font-size:10.5pt}</style>"
    "<!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View>"
    "</w:WordDocument></xml><![endif]--></head><body>%s</body></html>"
)


def _md_basic_html(md):
    """极简 Markdown→HTML：标题 / 加粗 / 列表 / 引用 / 分隔线，够排版方案正文。"""
    out = []
    in_ul = False
    for line in str(md).splitlines():
        s = line.rstrip()
        if not s.strip():
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue
        esc = _escape(s)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        if re.match(r"^#\s", s):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("<h1>%s</h1>" % esc[2:])
        elif re.match(r"^##\s", s):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("<h2>%s</h2>" % esc[3:])
        elif re.match(r"^###\s", s):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("<h3>%s</h3>" % esc[4:])
        elif re.match(r"^\s*[-*]\s+", s):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append("<li>%s</li>" % re.sub(r"^\s*[-*]\s+", "", esc))
        elif s.startswith(">"):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("<blockquote>%s</blockquote>" % esc.lstrip("> ").strip())
        elif re.match(r"^-{3,}\s*$", s):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("<hr>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("<p>%s</p>" % esc)
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _export_header(origin, destination, days, budget, people, interests=None, mode=DEFAULT_MODE):
    lines = [
        "# 🌍 智慧旅游规划助手 · 旅游方案",
        "",
        "- 目的地：%s" % (destination or "未填"),
        "- 出行天数：%s 天 ｜ 出行人数：%s 人" % (days, people),
        "- 人均预算：%s 元" % (budget or "不限"),
    ]
    if str(origin or "").strip():
        lines.append("- 出发地：%s" % origin)
        info = estimate_fare(origin, destination, budget, days, people, mode)
        if info.get("ok"):
            lines.append("- 往返路费：%s" % fare_summary(info))
    # 弹性保障一行摘要：纸面上也能一眼看到「这天该留多少机动、什么天气要改行程」
    fx = build_flexible(destination, days, people, interests)
    if fx.get("ok"):
        top = [w["title"] for w in fx["weather"] if w["level"] in ("high", "mid")]
        seg = ["建议每天留 %d 分钟机动" % fx["buffer"]["minutes"]]
        if top:
            seg.insert(0, "当季需留意" + "、".join(top[:2]))
        lines.append("- 弹性保障：%s ｜ %s" % (fx["city"], " ｜ ".join(seg)))
    lines += ["- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M"), "",
              "> 方案由 AI 检索本地旅游知识库生成，价格与开放时间以景区官方公布为准。", "", "---", ""]
    return "\n".join(lines)


def _is_real_plan(md):
    s = str(md or "").strip()
    return bool(s) and not s.startswith(("❌", "请先填写目的地", "抱歉"))


def _save_export(content, dest, ext):
    os.makedirs(_EXPORT_DIR, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "", str(dest or "方案"))[:12] or "方案"
    path = os.path.join(_EXPORT_DIR, "旅游方案-%s-%s.%s" % (safe, time.strftime("%Y%m%d-%H%M%S"), ext))
    with open(path, "w", encoding="utf-8-sig" if ext == "doc" else "utf-8") as f:
        f.write(content)
    return path


def export_plan_md(output, origin, destination, days, budget, people, interests=None,
                   mode=DEFAULT_MODE):
    """导出 Markdown 文件（含路费与弹性保障摘要的文件头 + 方案正文）"""
    if not _is_real_plan(output):
        return None
    content = _export_header(origin, destination, days, budget, people, interests, mode) + str(output)
    return _save_export(content, destination, "md")


def export_plan_doc(output, origin, destination, days, budget, people, interests=None,
                    mode=DEFAULT_MODE):
    """导出 Word（HTML 版 .doc：WPS / Word 直接打开，无需额外依赖）"""
    if not _is_real_plan(output):
        return None
    body = _md_basic_html(
        _export_header(origin, destination, days, budget, people, interests, mode)
        .replace("\n", "  \n")
    )
    body += _md_basic_html(str(output))
    return _save_export(_DOC_TMPL % body, destination, "doc")


def export_plan_ics(output, meta):
    """导出日历（.ics）：把每天的行程按时间段导进手机日历，出行当天自动提醒

    参数取生成时的快照（与海报同一套），保证导出的是「这份方案」的行程。

    返回 (下载按钮值, 提示, 兜底文件) 三路：
        · 成功   → (ics 路径, 绿色提示「已导出 N 条日程」, 文件卡片)
        · 失败   → (None, 说明原因的黄色提示, 隐藏)

    为什么要带提示：DownloadButton 是「静默下载型」——拿不到文件时前端什么都不做，
    用户看到的就是「点了没反应」（2026-10-14 老叶线上反馈）。加了提示后，
    「还没生成方案」「方案里没有按天行程」这类情况都会明确说出来；
    再配一个 gr.File 兜底，浏览器拦了自动下载也能手动点着存。
    """
    md = str(output or "")
    if not _is_real_plan(md):
        return (None,
                gr.update(value="⚠️ 请先生成一个旅游方案，再来导出日历。", visible=True),
                gr.update(value=None, visible=False))

    # 先数一遍能识别出多少条按天行程：0 条就别生成空日历，直接说清楚
    # 这里用 plan_events（已合并同点并列项），保证提示条数和文件里的事件数一致
    try:
        n = len(plan_events(md))
    except Exception:
        n = 0
    if not n:
        return (None,
                gr.update(value="⚠️ 这份方案里没找到可识别的「按天行程」，导不出日历。"
                                "（需要方案里带 Day1／Day2 这类每日行程，"
                                "可以切到「均衡」或「详细」档重新生成一份）",
                          visible=True),
                gr.update(value=None, visible=False))

    destination, origin, days, people = _poster_params(meta)
    try:
        path = build_ics(md, destination=destination, days=days, people=people,
                         out_dir=_EXPORT_DIR)
    except Exception as exc:
        return (None,
                gr.update(value="❌ 日历生成失败：%s（请重试）" % exc, visible=True),
                gr.update(value=None, visible=False))
    if not path:
        return (None,
                gr.update(value="❌ 日历生成失败，请重试。", visible=True),
                gr.update(value=None, visible=False))
    return (path,
            gr.update(value="✅ 已导出 <b>%d</b> 条日程，正在下载（若浏览器没弹出下载，"
                            "请点下方文件保存，或查看浏览器的下载栏）。"
                            "手机打开该 .ics 文件即可导入日历。" % n,
                      visible=True),
            gr.update(value=path, visible=True))


# ---------------------------------------------------------------------------
# 历史方案 + 分享 + 海报（2026-10-11 老叶「all in」一批）
#
# 原实现是服务器内存留最近 5 份（重启即清空）；现升级为 SQLite 持久化：
#   · 存多少不限（下拉里只展示最近 HISTORY_SHOW 条，全量在管理页可查）；
#   · 重启不丢，数据库文件在 data/plans.db；
#   · 每份方案生成后立即拿到一个 plan_id，点「生成分享链接 / 海报」直接用。
# ---------------------------------------------------------------------------
_HISTORY_SHOW = 20        # 下拉里展示的条数
_LAST_PLAN_ID = None      # 最近一次成功生成的方案 id（供分享/海报按钮免再查）
_ADJ_STATE = {"alloc": None, "dest_text": None, "days": None}


# ---------------------------------------------------------------------------
# 方案微调（导师反馈第 4 点）
# ---------------------------------------------------------------------------
def _alloc_to_text(alloc):
    """把 [(城市, 晚数)] 编码回目的地文本，供重新生成时复用。

    例：[(齐齐哈尔, 4), (五大连池, 1), (哈尔滨, 4)]
    → 「齐齐哈尔4晚, 五大连池1晚, 哈尔滨4晚」
    """
    return ", ".join("%s%d晚" % (c, n) for c, n in alloc)


def _current_alloc(destination, days):
    """按当前表单重新算出编排结果（微调基于它，不依赖历史状态）"""
    try:
        from core.multi_route import build_multi_route
        parsed = parse_destinations(destination)
        if len(parsed) < 2:
            return None
        return build_multi_route(
            destination, days=days, interests=None,
            spot_density=_spot_density(),
        )
    except Exception as exc:
        print("[adjust] 编排失败:", exc)
        return None


def _adjust_result_html(res, ok_msg=""):
    """把微调结果渲染成 Markdown"""
    if not res.get("ok"):
        return "⚠️ %s" % res.get("error", "调整失败")
    lines = ["### ✅ %s" % (ok_msg or "已调整")]
    lines.append("")
    for ch in res.get("changes", []):
        lines.append("- %s" % ch)
    lines.append("")
    lines.append("**调整后的路线**：%s" % " → ".join(
        "%s（%d 晚）" % (c, n) for c, n in res["cities"]))
    lines.append("")
    lines.append("**总行程**：%d 天 ｜ 路上时间占比约 %d%%"
                 % (res["actual_days"], res["transit_pct"]))
    lines.append("")
    lines.append("<details><summary>查看逐日安排</summary>")
    lines.append("")
    for d in res["days"]:
        lines.append("- **Day%d** %s" % (d["day"], d["brief"]))
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append("> 点下面的「用新结构重新生成」按钮，直接按调整后的路线出完整方案。")
    return "\n".join(lines)


def adjust_swap(day_no, place, destination, days):
    """微调：换掉某一天的城市"""
    base = _current_alloc(destination, days)
    if not base:
        return "⚠️ 当前目的地不是多城写法（需要用逗号分隔多个城市），无法微调。"
    try:
        from core.route_adjust import swap_day
        res = swap_day(base["cities"], int(day_no or 1), place)
        _ADJ_STATE["alloc"] = res["cities"]
        _ADJ_STATE["dest_text"] = _alloc_to_text(res["cities"])
        _ADJ_STATE["days"] = res["actual_days"]
        return _adjust_result_html(res, "已把第 %d 天换成「%s」" % (int(day_no or 1), place))
    except Exception as exc:
        return "⚠️ %s" % exc


def adjust_nights(city, delta_text, destination, days):
    """微调：调整某城停留天数"""
    base = _current_alloc(destination, days)
    if not base:
        return "⚠️ 当前目的地不是多城写法（需要用逗号分隔多个城市），无法微调。"
    sign = -1 if str(delta_text).startswith("少") else 1
    n = 1
    if "2" in str(delta_text):
        n = 2
    try:
        from core.route_adjust import adjust_nights as _adj
        res = _adj(base["cities"], city, sign * n)
        _ADJ_STATE["alloc"] = res["cities"]
        _ADJ_STATE["dest_text"] = _alloc_to_text(res["cities"])
        _ADJ_STATE["days"] = res["actual_days"]
        return _adjust_result_html(res, "已调整「%s」的停留" % city)
    except Exception as exc:
        return "⚠️ %s" % exc


def adjust_reorder(destination, days):
    """微调：重排顺序消除折返"""
    base = _current_alloc(destination, days)
    if not base:
        return "⚠️ 当前目的地不是多城写法（需要用逗号分隔多个城市），无法微调。"
    try:
        from core.route_adjust import reorder
        res = reorder(base["cities"])
        _ADJ_STATE["alloc"] = res["cities"]
        _ADJ_STATE["dest_text"] = _alloc_to_text(res["cities"])
        _ADJ_STATE["days"] = res["actual_days"]
        return _adjust_result_html(res, "已重排城市顺序")
    except Exception as exc:
        return "⚠️ %s" % exc


def apply_adjust_and_generate(origin, budget, people, interests, additional_request,
                               profile, mode):
    """用微调后的新结构重新生成完整方案。

    直接复用 travel_planner —— 把调整后的城市序列与晚数编码回目的地文本
    （如「齐齐哈尔4晚, 五大连池1晚, 哈尔滨4晚」），
    由 build_query 重新解析出同一套编排约束，模型按新结构填内容。
    """
    dest_text = _ADJ_STATE.get("dest_text")
    if not dest_text:
        yield ("还没有做过微调。先在上面调整一次，再点这个按钮。",
               gr.update(value="", visible=False), gr.update(), gr.update(),
               gr.update(), gr.update())
        return
    days = _ADJ_STATE.get("days") or 3
    for item in travel_planner(origin, dest_text, days, budget, people,
                               interests, additional_request, profile, mode):
        yield item


def after_generate(output, origin, destination, days, budget, people, interests,
                   mode=DEFAULT_MODE, profile=DEFAULT_PROFILE):
    """生成结束后的统一收尾：预算明细条 + 行李清单 + 弹性保障 + 历史记录。

    挂在 submit_btn.click(...).then(...) 上：等生成器完整跑完才执行一次，
    因此 travel_planner 本身的 outputs 不用因为这几个功能而变长。
    """
    global _LAST_PLAN_ID
    md = str(output or "")
    failed = not _is_real_plan(md)

    if failed:
        bar_upd = gr.update(value="", visible=False)
    else:
        fare_info = estimate_fare(origin, destination, budget, days, people, mode)
        bar = _budget_bar_html(md, fare_info, days, budget)
        bar_upd = (gr.update(value=bar, visible=True) if bar
                   else gr.update(value="", visible=False))

    # 两张行前准备卡（行李清单 / 弹性保障）在这里再刷一次，保证生成结束后一定在位
    pack_upd = _pack_view(destination, days, people, interests)
    flex_upd = _flex_view(destination, days, people, interests)

    # 行程时间轴：解析方案里的 Day 行渲染成卡片；解析不出就不打扰正文
    if failed:
        tl_upd = gr.update(value="", visible=False)
    else:
        tl = build_timeline_html(md, destination)
        tl_upd = (gr.update(value=tl, visible=True) if tl
                  else gr.update(value="", visible=False))

    dd_upd = gr.update()
    share_code = ""
    if not failed:
        title = "%s · %s天 · %s人 · %s" % (
            str(destination).strip()[:16], int(days or 1), int(people or 1),
            time.strftime("%H:%M"),
        )
        # 存库（含出行参数，历史回看 / 分享页 / 海报都要用）
        pid = save_plan(title, md, destination=destination, origin=origin,
                        days=days, people=people, budget=budget,
                        interests=interests, mode=mode, profile=profile)
        if pid:
            _LAST_PLAN_ID = pid
            # 顺手把分享短码也建好（幂等）——海报上的二维码要用它，
            # 这样「下载海报」一步就能出带码的图，不必先点一次「生成分享链接」
            share_code = get_or_create_share(pid) or ""
        rows = list_plans(_HISTORY_SHOW)
        dd_upd = gr.update(choices=[r["title"] for r in rows], value=None)

    # 生成那一刻的参数快照：海报/分享用它，不用输入框的现值——
    # 否则生成后又点了图册（目的地被回填）再点海报，会出现「目的地黑龙江、
    # 正文澳门」这种图文错位（2026-10-12 老叶反馈）
    meta = {"destination": destination, "origin": origin,
            "days": days, "people": people,
            "plan_id": _LAST_PLAN_ID, "share_code": share_code}
    return bar_upd, tl_upd, pack_upd, flex_upd, dd_upd, meta


def load_history(evt: gr.EventData):
    """下拉选中历史方案 → 把那份方案正文放回输出区

    连同库里存的出行参数一起返回（写入 gen_meta 状态）：
    之后点海报用的是「那份方案自己的参数」，不是输入框现值。
    """
    title = getattr(evt, "value", None) or getattr(evt, "selected", None)
    if not title:
        return gr.update(), {}
    for row in list_plans(_HISTORY_SHOW):
        if row["title"] == title:
            plan = get_plan(row["id"])
            if plan:
                meta = {"destination": plan.get("destination") or "",
                        "origin": plan.get("origin") or "",
                        "days": plan.get("days") or 0,
                        "people": plan.get("people") or 0,
                        "plan_id": plan.get("id"),
                        "share_code": get_or_create_share(plan.get("id")) or ""}
                return plan["content"], meta
    return gr.update(), {}


def _share_url(code, request):
    """短码 → 绝对分享链接

    request 由 Gradio 自动注入，用 Host 头拼出绝对链接——
    本地打开就是 127.0.0.1:7860/s/xx，线上打开就是正式域名，无需配置。
    """
    code = str(code or "").strip()
    if not code:
        return ""
    host = "127.0.0.1:7860"
    proto = ""
    if request is not None:
        try:
            host = request.headers.get("host") or host
            proto = request.headers.get("x-forwarded-proto") or ""
        except Exception:
            pass
    if not proto:
        proto = "https" if ("workbuddy" in host or ".app." in host) else "http"
    return "%s://%s/s/%s" % (proto, host, code)


def make_share_link(output, request: gr.Request):
    """「生成分享链接」：把当前方案入库（若尚未入库）并发一个只读短码"""
    global _LAST_PLAN_ID
    md = str(output or "")
    if not _is_real_plan(md):
        return gr.update(value="⚠️ 请先生成一个有效的旅游方案，再来生成分享链接。",
                         visible=True)
    pid = _LAST_PLAN_ID
    if not (pid and get_plan(pid) and get_plan(pid)["content"] == md):
        pid = save_plan("分享方案 · %s" % time.strftime("%m-%d %H:%M"), md)
        _LAST_PLAN_ID = pid
    # 幂等：同一份方案复用同一个短码，二维码与链接永远指向一处
    code = (get_or_create_share(pid) or create_share(pid)) if pid else None
    if not code:
        return gr.update(value="❌ 分享链接生成失败（数据库异常），请重试。", visible=True)
    url = _share_url(code, request)
    return gr.update(
        value=("✅ 分享链接已生成（只读，可直接发给朋友）：<br>"
               "<b>%s</b>" % _escape(url)),
        visible=True,
    )


def _poster_params(meta):
    """从 gen_meta 快照取海报参数（空快照时全部留空，海报自动省略对应行）"""
    meta = meta or {}
    return (meta.get("destination") or "", meta.get("origin") or "",
            meta.get("days") or 0, meta.get("people") or 0)


def make_poster(output, meta, request: gr.Request = None):
    """「生成海报」：把当前方案渲染成可下载的 PNG 长图（参数来自生成时快照）"""
    md = str(output or "")
    if not _is_real_plan(md):
        return gr.update(value=None, visible=False), "⚠️ 请先生成一个有效的旅游方案。"
    destination, origin, days, people = _poster_params(meta)
    qr = _share_url((meta or {}).get("share_code"), request)
    try:
        path = build_poster(md, destination=destination, days=days, people=people,
                            origin=origin, qr_payload=qr)
    except Exception as exc:
        return gr.update(value=None, visible=False), "❌ 海报生成失败：%s" % exc
    return gr.update(value=path, visible=True), "✅ 海报已生成，点击图片即可下载。"


def make_poster_file(output, meta, request: gr.Request = None):
    """DownloadButton 版：直接返回海报文件路径触发浏览器下载

    带上二维码（= 该方案的固定分享短码），扫一下就能在手机上看完整行程。
    """
    md = str(output or "")
    if not _is_real_plan(md):
        return None
    destination, origin, days, people = _poster_params(meta)
    qr = _share_url((meta or {}).get("share_code"), request)
    try:
        return build_poster(md, destination=destination, days=days, people=people,
                            origin=origin, qr_payload=qr)
    except Exception:
        return None


# Agent 缓存：Gradio 每次点击都会调用，按档位各缓存一份，避免重复构建
_AGENTS = {}

INTEREST_CHOICES = ["自然风光", "历史文化", "美食探店", "购物娱乐", "亲子游", "户外探险"]


def get_agent(profile=DEFAULT_PROFILE):
    """取（或创建）指定档位的 Agent；不同档位模型与提示词不同，需分别缓存"""
    key = normalize_profile(profile)
    if key not in _AGENTS:
        _AGENTS[key] = create_travel_agent(profile=key)
    return _AGENTS[key]


def build_query(destination, days, budget, people, interests, additional_request,
                origin="", fare_info=None):
    """把表单内容整理成给 Agent 的自然语言需求

    填了出发地时，会把「往返路费估算 + 预算扣减口径」一并写进需求：
    否则模型看到「人均预算 3000」会按整趟行程去分配，等于把往返大交通漏算或重复算，
    与页面上给用户的「已从预算里扣掉路费」自相矛盾。

    **多目的地**：填了多个城市时，交给 core.multi_route 编排，
    把「城市顺序 + 停留夜数 + 城际交通」作为硬约束写进需求，
    模型只负责填内容，不允许自行改顺序与天数。
    """
    days = int(days or 1)
    nights = max(days - 1, 0)
    has_fare = bool(fare_info and fare_info.get("ok"))

    # ---- 多目的地编排（先于单目的地分支）----
    multi = None
    try:
        from core.multi_route import build_multi_route
        parsed = parse_destinations(destination)
        if len(parsed) > 1:
            multi = build_multi_route(
                destination, days=days, interests=interests,
                budget=budget, people=people, origin=origin,
                spot_density=_spot_density(),
            )
    except Exception as exc:      # 编排失败不应拖垮整个生成流程
        print("[multi_route] 编排失败，降级为单目的地：", exc)
        multi = None

    if multi and multi.get("ok"):
        lines = [
            "请为我制定一条**多城串联**的 %d 天旅游方案，涉及以下城市：%s" % (
                days, "、".join(c for c, _ in multi["cities"])),
            "",
            multi["notes"],
            "",
        ]
    else:
        lines = [f"请为我制定一个「{destination}」的 {days} 天 {nights} 晚旅游方案。"]

    if origin and has_fare:
        lines.append(f"- 出发地：{origin}")
        lines.append(f"- 往返大交通（系统已估算，仅供参考）：{fare_summary(fare_info)}")
        mode_name = fare_info.get("mode_name") or ""
        if mode_name:
            lines.append(
                "- 交通方式：用户已选择【%s】，预算按这一方式核算；"
                "不要改按机票或其他方式重新算一遍，也不要假设用户会临时改坐飞机。"
                % mode_name
            )
        lines.append(
            "- 预算口径：人均预算【已包含出发地往返路费】，"
            "方案里的门票/住宿/餐饮/当地交通等目的地花费，请按扣除往返路费后的余额来安排，"
            "不要重复计算往返机票或高铁票价。"
        )
    lines += [
        f"- 人均预算：{budget or '不限'} 元" + ("（已含往返路费）" if (origin and has_fare) else ""),
        f"- 出行人数：{int(people or 1)} 人",
        f"- 兴趣偏好：{'、'.join(interests) if interests else '无特别偏好'}",
        f"- 额外要求：{additional_request or '无'}",
    ]
    if not (origin and has_fare):
        lines.append("- 说明：用户未提供出发地，请勿臆测其出发城市；预算部分请注明「不含出发地往返大交通」。")
    if multi and multi.get("ok"):
        lines.append(
            "- **多城口径**：行程按上面给出的城市顺序与停留夜数执行；"
            "城际交通的时间与票价已由系统算出，直接引用，不要另算一套。"
            "预算里要**单列城际交通费**（各城之间的大交通），与各城内部的当地花费分开。"
        )
    # 弹性保障在用户消息里再钉一次：系统提示里的硬性结构，模型偶尔会为守字数砍掉，
    # 用户消息里的显式提醒服从性明显更高（实测缺失后补上这条即可回归）。
    lines.append(
        "- 硬性要求：每天行程要有🕐机动时段，重点项目带☔备选，"
        "末尾必须有「🛡 弹性保障」小节（舍弃顺序 + 天气应变 + 出发前确认），不能省略。"
    )
    return "\n".join(lines)


def extract_answer(messages):
    """取最后一条含正文的 AI 消息（跳过只带 tool_calls 的中间消息）

    Web 端已改为流式输出（见 travel_planner），此函数保留给
    离线评估脚本与非流式（invoke）调用场景使用。
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        content = msg.content
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if text.strip():
                return text
    return "抱歉，暂时无法生成方案，请稍后重试。"


def collect_sources(messages):
    """从 Agent 执行轨迹里收集知识库检索命中的片段来源"""
    sources = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.name == "travel_knowledge_search_tool":
            for line in str(msg.content).splitlines():
                stripped = line.strip()
                if stripped.startswith("[参考"):
                    sources.append(stripped)
    # 去重并保持顺序
    return list(dict.fromkeys(sources))


def _chunk_text(content):
    """从流式消息的 content 中取出纯文本（兼容 str 与多模态列表两种形态）"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def travel_planner(origin, destination, days, budget, people, interests, additional_request,
                   profile=DEFAULT_PROFILE, mode=DEFAULT_MODE):
    """主流程：流式调用 Agent → 边生成边输出 → 附知识库来源与实景图册

    采用 Agent 的 messages 流模式，把「等全部生成完」拆成两段可见进度：
    1. 检索阶段：只推送检索进度，不推送模型的寒暄（避免「说一句话就干等半分钟」的观感）；
    2. 撰写阶段：检索一完成就逐段即时输出正文，首屏几乎不用等。

    profile 为方案详略档位（fast / balanced / detailed），档位同时决定模型与篇幅策略。

    origin 为选填的出发地；mode 为交通方式（高铁二等座 / 普速硬卧，机票不可选）。
    两者一起决定路费卡的结论，并写进提问让模型的预算口径保持一致。
    """
    profile = normalize_profile(profile)
    profile_label = PROFILES[profile]["label"]

    # 路费：纯本地估算，先算好（不依赖 Agent，也不受生成成败影响）
    fare_info = estimate_fare(origin, destination, budget, days, people, mode)
    fare_html = _fare_html(fare_info)
    fare_view = (
        gr.update(value=fare_html, visible=True) if fare_html
        else gr.update(value="", visible=False)
    )

    if not destination or not str(destination).strip():
        yield ("请先填写目的地，例如「成都」「西安」。",
               gr.update(value="", visible=False), gr.update(), gr.update(), gr.update())
        return

    query = build_query(destination, days, budget, people, interests, additional_request,
                        origin, fare_info)
    # 生成期间图带保持流动（本来就是纯 CSS 动画，不必动它）
    yield ("🔍 正在检索本地旅游知识库……（%s）" % profile_label,
           fare_view, gr.update(), gr.update(), "")

    try:
        agent = get_agent(profile)
    except Exception as exc:
        yield (
            f"❌ 生成失败：{exc}\n\n请检查网络、API Key 以及是否已运行 `python init_kb.py` 构建知识库。",
            fare_view,
            gr.update(),
            gr.update(),
            "",
        )
        return

    shown = ""          # 已展示的正文
    pending = ""        # 检索阶段模型的话，先攒着不展示
    sources = []
    rounds = 0          # 检索轮次
    ready = False       # 是否已进入「撰写正文」阶段
    last_flush = 0.0
    error = None

    try:
        for chunk, _meta in agent.stream(
            {"messages": [HumanMessage(content=query)]}, stream_mode="messages"
        ):
            if isinstance(chunk, ToolMessage):
                # 一次检索结束：收集来源，并把检索前的寒暄作废
                if chunk.name == "travel_knowledge_search_tool":
                    rounds += 1
                    for line in str(chunk.content).splitlines():
                        stripped = line.strip()
                        if stripped.startswith("[参考") and stripped not in sources:
                            sources.append(stripped)
                pending = ""
                ready = True
                if not shown:
                    yield (
                        "✍️ 已检索 %d 段资料，正在撰写方案……" % rounds,
                        gr.update(),
                        gr.update(),
                        gr.update(),
                        "",
                    )
                continue

            if not isinstance(chunk, AIMessageChunk):
                continue
            text = _chunk_text(chunk.content)
            if not text:
                continue

            if not ready:
                pending += text
                continue

            shown += text
            now = time.time()
            # 节流：公网下推送过于频繁反而会拖慢前端渲染
            if now - last_flush >= 0.1 or text.endswith("\n"):
                last_flush = now
                yield shown, gr.update(), gr.update(), gr.update(), ""
    except Exception as exc:
        error = exc

    # 兜底：模型没有检索就直接回答（或流式未产出正文）时，展示它说的话
    if not shown and pending.strip():
        shown = pending

    if not shown:
        message = (
            f"❌ 生成失败：{error}\n\n请检查网络、API Key 以及是否已运行 `python init_kb.py` 构建知识库。"
            if error is not None
            else "抱歉，暂时无法生成方案，请稍后重试。"
        )
        yield message, fare_view, gr.update(), gr.update(), ""
        return

    if error is not None:
        shown += "\n\n> ⚠️ 生成过程中断：%s\n\n以上为已完成的部分。" % error

    if sources:
        shown += "\n\n---\n\n**📚 本地知识库命中来源**\n\n" + "\n".join(
            f"- {item}" for item in sources
        )
    else:
        shown += "\n\n---\n\n> 本次未命中本地知识库，方案基于模型通用知识生成，仅供参考。"

    # 图册：按目的地匹配；匹配到就换成正式图册（按勾选的兴趣偏好优先排序），
    # 匹配不到不硬凑 —— 图带本来就是「随机漫游」，保持原样即可
    dest_entry = match_destination(destination)
    if dest_entry:
        items = gallery_items_for(dest_entry, interests, with_region=False)
        if items:
            covered = sorted({t for im in dest_entry.get("images", [])
                              for t in (im.get("interests") or [])})
            shown += ("\n\n---\n\n**🖼 实景图册**（%s）：共 %d 张，覆盖兴趣 %s。"
                      "图片来源网络，版权归原作者，仅作演示。"
                      % (dest_entry["region"], len(items), "、".join(covered)))
            yield (shown,
                   fare_view,
                   gr.update(
                       visible=True, value=items,
                       label="🖼 目的地实景图册 · %s" % dest_entry["region"]
                   ),
                   strip_out,
                   dest_entry["region"])
            return

    yield shown, fare_view, gr.update(), gr.update(), ""


def clear_all():
    """一键清空所有输入与输出（档位回到默认档，交通方式回到高铁），右侧回到随机漫游图带"""
    return (
        "", "", 3, "", 2, ["美食探店", "历史文化"], "", DEFAULT_PROFILE,
        "", gr.update(value="", visible=False),
        # 交通方式：收起单选项并回到默认（高铁二等座）
        gr.update(choices=[MODE_LABELS[k] for k in ("hsr", "slow")],
                  value=MODE_LABELS[DEFAULT_MODE], visible=False),
        gr.update(visible=False),
        gr.update(), "", "",
        # 预算条 / 行李清单 / 弹性保障：清空后一并收起（历史记录保留）
        gr.update(value="", visible=False),
        gr.update(value="", visible=False),
        gr.update(value="", visible=False),
        # 时间轴 / 分享提示 / 海报预览 / 日历提示与文件 / 参数快照：一并收起
        gr.update(value="", visible=False),
        gr.update(value="", visible=False),
        gr.update(value=None, visible=False),
        gr.update(value="", visible=False),
        gr.update(value=None, visible=False),
        {},
    )


# ---------------------------------------------------------------------------
# 页首横幅 + 「没想好去哪儿」直达面板
#
# 2026-09-22 老叶两条反馈一起落地：
#   1) 页首两行素 Markdown 太素 → 改成渐变横幅卡，徽章数字直接从图册索引统计，
#      以后增删图片无需改这里；
#   2) 桌面端右栏待机时只有图带和两个下载按钮，下面一大片空白 → 加一块
#      「热门目的地直达」面板常驻右栏底部：待机时正好补位，生成方案后
#      它排在右栏最尾（pick_hint 之后），不抢正文与图册的位置。
#
# 面板芯片的点击**复用图带那条事件通道**：前端 trigger('select', {region})，
# 服务端同一个 show_pick_hint 回填目的地。不另起新事件类型，白名单校验也只有一份。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 2026-10-11 老叶反馈：直达面板芯片想「随机化」——不用定时轮换，每次刷新网页
# 换一批就行。实现：34 个区全量入池（各配一句特色标语），_explore_html() 每次
# random.sample 抽 12 个；再挂 demo.load，页面每次加载/刷新时服务端重抽一遍，
# 因此同一批芯片只在本次会话内固定，刷新即换。
# ---------------------------------------------------------------------------
_EXPLORE_POOL = [
    ("四川", "🐼 大熊猫与火锅"),
    ("北京", "故宫与长城"),
    ("陕西", "兵马俑与古城墙"),
    ("重庆", "洪崖洞夜景"),
    ("海南", "亚龙湾踏浪"),
    ("云南", "石林奇景"),
    ("湖南", "岳麓山与湘江"),
    ("江苏", "中山陵与秦淮"),
    ("浙江", "西湖十景"),
    ("山西", "平遥古城"),
    ("甘肃", "敦煌莫高窟"),
    ("西藏", "布达拉宫"),
    ("澳门", "大三巴与葡式蛋挞"),
    ("香港", "维港夜景与茶餐厅"),
    ("上海", "外滩与迪士尼"),
    ("天津", "五大道与天津之眼"),
    ("台湾", "日月潭与夜市小吃"),
    ("吉林", "长白山天池"),
    ("安徽", "黄山与宏村"),
    ("山东", "崂山与栈桥"),
    ("广东", "广州塔与早茶"),
    ("江西", "滕王阁与庐山"),
    ("河北", "承德避暑山庄"),
    ("河南", "少林寺与龙门石窟"),
    ("湖北", "黄鹤楼与东湖"),
    ("福建", "鼓浪屿与武夷山"),
    ("贵州", "黄果树瀑布"),
    ("辽宁", "沈阳故宫"),
    ("青海", "青海湖与茶卡盐湖"),
    ("黑龙江", "冰雪大世界"),
    ("内蒙古", "呼伦贝尔大草原"),
    ("宁夏", "沙坡头与西夏王陵"),
    ("广西", "桂林山水与漓江"),
    ("新疆", "喀纳斯与天山天池"),
]
_EXPLORE_N = 12          # 每次刷新展示的芯片数


def _count_gallery_images():
    return sum(len([im for im in e.get("images", []) if im.get("file")])
               for e in _DEST_INDEX)


def _hero_html():
    n_reg = len(_region_names())
    badges = [
        "🗺 覆盖 %d 个省级行政区" % n_reg,
        "🖼 %d 张目的地实景图" % _count_gallery_images(),
        "💸 路费预算即时核算",
        "🛡 行程弹性保障",
        "🎒 行李清单自动生成",
    ]
    return (
        "<div class='hero'>"
        "<div class='hero-title'>🌍 智能旅游规划助手</div>"
        "<div class='hero-sub'>输入出行需求，AI 会先检索本地旅游知识库，再为你定制专属方案。</div>"
        "<div class='hero-badges'>%s</div>"
        "</div>" % "".join("<span class='hb'>%s</span>" % b for b in badges)
    )


def _explore_html():
    # 从全池随机抽 12 个（每次调用都重抽；配合 demo.load 实现「刷新换一批」）
    valid = [(r, d) for r, d in _EXPLORE_POOL if r in _region_names()]
    picks = random.sample(valid, min(_EXPLORE_N, len(valid)))
    chips = []
    for region, desc in picks:
        chips.append(
            "<div class='xp-chip' data-region='%s' title='点击填入目的地'>"
            "<div class='r'>%s</div><div class='s'>%s</div></div>"
            % (region, region, desc)
        )
    return (
        "<div class='xp-card'>"
        "<div class='xp-head'>🎲 没想好去哪儿？点一下直达</div>"
        "<div class='xp-grid'>%s</div>"
        "<div class='fare-foot'>※ 共覆盖 %d 个省级行政区 · %d 张实景图；"
        "点击任意芯片或上方图带图片，都会把目的地填进左侧表单。</div>"
        "</div>" % ("".join(chips), len(_region_names()), _count_gallery_images())
    )


with gr.Blocks(title="智能旅游规划助手") as demo:
    # 页首横幅（替代原来两行素 Markdown）。注意 value 是 HTML 字符串时
    # 必须走 html_template 形态——直接放 value 会被纯文本转义（见下方图带处的注释）。
    gr.HTML(
        value=_hero_html(),
        html_template="${value}",
        js_on_load=None,
        elem_id="hero-slot",
    )

    with gr.Row():
        with gr.Column(scale=1):
            destination = gr.Textbox(
                label="目的地（可填多个，用逗号分隔）",
                placeholder="单一：例如：成都、西安、三亚　｜　多城：哈尔滨, 齐齐哈尔, 五大连池",
                info=(
                    "填多个城市即进入**多城路线规划**模式：系统会按地理邻近排出不绕路的顺序，"
                    "按城际耗时分配各城停留天数，并给出城际交通方式、时长与票价。"
                    "也可以写成「哈尔滨2晚, 齐齐哈尔1晚」直接指定停留。"
                ),
            )
            # 出发地是**选填**：填了才算往返路费、才从预算里扣；不填就完全不介入
            origin = gr.Textbox(
                label="出发地（选填）",
                placeholder="例如：北京、上海；留空则不计算往返路费",
                info="填了就算往返路费，并从人均预算里扣掉；留空则只算目的地花费",
            )
            days = gr.Number(label="出行天数", value=3, minimum=1, maximum=15, precision=0)
            budget = gr.Textbox(label="人均预算", placeholder="例如：1500、3000")
            people = gr.Number(label="出行人数", value=2, minimum=1, precision=0)
            interests = gr.CheckboxGroup(
                label="兴趣偏好",
                choices=INTEREST_CHOICES,
                value=["美食探店", "历史文化"],
            )
            additional_request = gr.Textbox(
                label="额外要求",
                placeholder="例如：喜欢安静的地方、不要太赶的行程",
            )
            profile = gr.Radio(
                label="方案详略",
                choices=[
                    (
                        "%s（%s）" % (cfg["label"], cfg["eta"])
                        if cfg.get("eta")
                        else cfg["label"],
                        key,
                    )
                    for key, cfg in PROFILES.items()
                ],
                value=DEFAULT_PROFILE,
                info="快速更省时间；详细内容最全，但要多等一会儿",
            )
            # 历史方案：服务器内存留最近 5 份，下拉即可回看（重启即清空）
            history_dd = gr.Dropdown(
                label="🕘 历史方案（最近 20 条 · 存在数据库，重启不丢）",
                choices=[],
                allow_custom_value=False,
                interactive=True,
                info="生成过的方案都留了底，点选即可回看对比；存在 SQLite 里，重启也不丢",
            )

            with gr.Row():
                submit_btn = gr.Button("生成旅游方案", variant="primary")
                clear_btn = gr.Button("清空所有输入", variant="secondary")

            # ---- 方案微调（多城模式）----
            # 导师反馈第 4 点「给出初步方案后可以微调」。
            # 三个受约束的局部重算：换这天 / 调整停留天数 / 重排顺序。
            # 不是让模型自由发挥——只改结构（顺序、天数、城市），
            # 景点与描述由模型按新结构重新填。
            with gr.Accordion("微调方案（多城路线可用）", open=False):
                gr.Markdown(
                    "生成后可以在这里做**局部调整**：换掉某一天的城市、"
                    "给某城多留几晚、或者重排顺序消除折返。\n\n"
                    "每次调整都会重新计算城际交通与天数分配，并说明改了什么、为什么。"
                )
                with gr.Row():
                    adj_day = gr.Number(label="第几天", value=1, minimum=1, precision=0,
                                        info="要换掉第几天的安排")
                    adj_place = gr.Textbox(label="换成哪里", placeholder="例如：牡丹江、扎龙")
                adj_swap_btn = gr.Button("换这天", variant="secondary")
                with gr.Row():
                    adj_city = gr.Textbox(label="调整哪个城市", placeholder="例如：齐齐哈尔")
                    adj_delta = gr.Radio(label="调整方式", choices=["多留 1 晚", "少留 1 晚",
                                                                  "多留 2 晚", "少留 2 晚"])
                adj_nights_btn = gr.Button("调整停留", variant="secondary")
                adj_reorder_btn = gr.Button("重排顺序（消除折返）", variant="secondary")
                adj_result = gr.Markdown("")
                adj_apply_btn = gr.Button("用新结构重新生成方案", variant="primary")

        with gr.Column(scale=2):
            # 待机图带放在右栏**最顶部**：首屏（不用滚动）就能看到全国实景，
            # 点图片直接回填目的地。此前它被放在两个栏之外（整页最底部），
            # 用户要滑一屏多才能看见——2026-09-22 老叶实测反馈后挪到这里。
            #
            # 用 gr.HTML 的「模板 + js_on_load」形态（Gradio 6 的正确姿势）：
            #
            # 踩过的两个坑，记下来省得再试：
            #   1) gr.HTML / gr.Markdown 直接放 HTML 字符串都会被**纯文本转义**
            #      （`>` 变 `\u003e`、`'` 变 `\u0027`），标签原样显示成字符，图带压根出不来；
            #      而且整段 base64 还会被塞进 config 和页面壳里，页面从 48KB 涨到 4MB。
            #   2) 正确做法是让 HTML 组件只负责「渲染模板」：value 只放真正的内容，
            #      再靠 js_on_load 在组件挂载后绑事件 —— 事件里用 closest('.card')
            #      拿到被点的卡片，读出 data-region，再用 trigger() 发给服务端。
            #
            # ⚠️ js_on_load 里**没有**「自动 import 的组件实例」。它的执行方式是
            #    new Function("element","trigger","props","server","upload","watch", code)，
            #    作用域里只有这 7 个名字。想引用 Python 变量（如 strip_out）必然
            #    ReferenceError，而且错在回调里、页面上没有任何提示 —— 2026-09-22
            #    线上「点图片不填目的地」就是这个原因。触发事件的唯一入口是 trigger()。
            strip_out = gr.HTML(
                value=build_strip(),
                html_template="<div id='travel-strip-box'>${value}</div>",
                js_on_load="""
                const box = element.querySelector('#travel-strip-box');
                if (box && !box.__bound) {
                  box.__bound = true;

                  // 每次打开页面都随机一次卡片顺序：两份副本同步做同一个排列，
                  // 等宽性不变（translateX(-50%) 依旧无缝），传输量为零。
                  const g1 = box.querySelector('.travel-strip .grp');
                  const g2 = g1 && g1.nextElementSibling;
                  if (g1 && g2 && !box.__shuffled) {
                    box.__shuffled = true;
                    const a = Array.prototype.slice.call(g1.children);
                    const b = Array.prototype.slice.call(g2.children);
                    for (let i = a.length - 1; i > 0; i--) {
                      const j = Math.floor(Math.random() * (i + 1));
                      [a[i], a[j]] = [a[j], a[i]];
                      [b[i], b[j]] = [b[j], b[i]];
                    }
                    a.forEach(card => g1.appendChild(card));
                    b.forEach(card => g2.appendChild(card));
                  }

                  // 触发 select 事件，把行政区作为事件数据发出去。
                  // 只能用官方给的 trigger()：js_on_load 的作用域里没有 Python 变量名，
                  // 写成 strip_out.select(...) 会抛 ReferenceError 且点击静默失效。
                  box.addEventListener('click', (e) => {
                    const fig = e.target.closest('.travel-strip .card');
                    if (!fig) return;
                    const region = fig.dataset.region || '';
                    if (region) trigger('select', {region: region});
                  });
                }
                """,
                elem_id="travel-strip-slot",
            )
            # 交通方式：用户自己选按哪种方式核算路费（机票不可选——波动太大，
            # 只展示区间作参考）。只有填了出发地 + 目的地能识别时才出现。
            fare_mode = gr.Radio(
                label="🚉 交通方式（路费按哪种核算）",
                choices=[MODE_LABELS[k] for k in ("hsr", "slow")],
                value=MODE_LABELS[DEFAULT_MODE],
                visible=False,
                info="机票价格波动大、班次差异也大，卡片里列出区间仅供参照，不参与预算核算",
            )
            # 往返路费卡：填了出发地才出现，放在方案正上方（页面最醒目的位置）。
            # 用户一填完「出发地 + 目的地 + 预算」就即时显示，不必等到生成方案。
            fare_box = gr.HTML(
                value="",
                html_template="<div id='fare-card-box'>${value}</div>",
                js_on_load=None,          # 这张卡不需要任何交互，省掉默认的 click 绑定
                visible=False,
                elem_id="fare-card-slot",
            )
            output = gr.Markdown(label="你的专属旅游方案")
            # 行程时间轴：按天拆成卡片（纯渲染层，解析失败自动隐藏，不影响正文）
            timeline_box = gr.HTML(
                value="",
                html_template="<div id='tl-box'>${value}</div>",
                js_on_load=None,
                visible=False,
                elem_id="tl-slot",
            )
            # 预算去向堆叠条：生成结束后由 after_generate 解析方案预算小节画出
            budget_bar = gr.HTML(
                value="",
                html_template="<div id='bbar-box'>${value}</div>",
                js_on_load=None,
                visible=False,
                elem_id="bbar-slot",
            )
            # 目的地实景图册：紧跟方案正文/预算条（2026-10-12 老叶反馈「图册
            # 孤在页面最底下不应景」→ 挪进规划区，生成后与正文连成一体）。
            # 点图依然会回填目的地（gallery.select 事件不受挪位影响）。
            gallery = gr.Gallery(
                label="🖼 目的地实景图册",
                columns=3,
                height=420,
                object_fit="cover",
                interactive=False,
                visible=False,          # 未生成方案时藏起来，让图带占位
                elem_id="dest-gallery",  # 手机端 CSS 靠它把固定高度改成自适应
            )
            with gr.Row():
                dl_md = gr.DownloadButton("⬇️ 下载 Markdown", variant="secondary")
                dl_doc = gr.DownloadButton("⬇️ 下载 Word 文档", variant="secondary")
                dl_pic = gr.DownloadButton("⬇️ 下载分享海报（含二维码）", variant="secondary")
                dl_ics = gr.DownloadButton("📅 导出日历 (.ics)", variant="secondary")
            # 日历导出的反馈位：DownloadButton 自己不会报错，成功/失败都在这儿说明
            ics_msg = gr.HTML(
                value="",
                html_template="<div id='ics-box'>${value}</div>",
                js_on_load=None,
                visible=False,
                elem_id="ics-slot",
            )
            # 兜底下载位：万一浏览器拦了自动下载（电脑端常见：下载只进下载栏不弹窗），
            # 这个文件卡片还能手动点着存（2026-10-14 老叶「点了没反应」后补）
            ics_file = gr.File(
                label="⬇️ 保存日历文件（.ics）",
                visible=False,
                interactive=False,
                elem_id="ics-file",
            )
            share_btn = gr.Button("🔗 生成分享链接（发给朋友直接看）", variant="secondary")
            share_msg = gr.HTML(
                value="",
                html_template="<div id='share-box'>${value}</div>",
                js_on_load=None,
                visible=False,
                elem_id="share-slot",
            )
            # 海报预览：生成后显示在这里，页面内可直接右键/长按保存
            poster_img = gr.Image(
                label="🖼 分享海报预览",
                visible=False,
                interactive=False,
                elem_id="poster-slot",
            )
            # 行李清单：目的地 / 天数 / 人数 / 兴趣一变就即时刷新（纯规则，不调模型）
            packing_box = gr.HTML(
                value="",
                html_template="<div id='pack-box'>${value}</div>",
                js_on_load=None,
                visible=False,
                elem_id="pack-slot",
            )
            # 弹性保障：和行李清单同源刷新（纯规则）。回答「行程排死了怎么办」——
            # 天气风险 / 每天留多少机动 / 赶不上先舍什么 / 雨天改走哪儿 / 出发前确认什么
            flex_box = gr.HTML(
                value="",
                html_template="<div id='flex-box'>${value}</div>",
                js_on_load=None,
                visible=False,
                elem_id="flex-slot",
            )
            pick_hint = gr.Markdown("")
            # 「没想好去哪儿」直达面板：常驻右栏底部。待机时右栏只有图带 +
            # 两个下载按钮，下面一大片空白（2026-09-22 老叶截图反馈），它正好补位；
            # 生成方案后排在最尾，不抢正文与图册。芯片点击走 trigger('select')，
            # 与图带共用 show_pick_hint 回填目的地。
            explore = gr.HTML(
                value=_explore_html(),
                html_template="<div id='explore-box'>${value}</div>",
                js_on_load="""
                const el = element.querySelector('#explore-box');
                if (el && !el.__bound) {
                  el.__bound = true;
                  el.addEventListener('click', (e) => {
                    const chip = e.target.closest('.xp-chip');
                    if (!chip) return;
                    const region = chip.dataset.region || '';
                    if (region) trigger('select', {region: region});
                  });
                }
                """,
                elem_id="explore-slot",
            )
            # gallery_region 记录正式图册对应的行政区（其图注不带省份，靠它回填目的地）
            gallery_region = gr.State("")
            # 生成那一刻的参数快照（目的地/出发地/天数/人数）：海报与历史回看用。
            # 输入框之后被改（比如又点了图册回填）不影响已生成方案的海报。
            gen_meta = gr.State({})

    gr.Markdown(
        "> 方案由 AI Agent 检索本地知识库生成，具体价格与开放时间请以景区官方公布为准。"
        "图册图片来源网络，版权归原作者所有，仅作学习与演示用途。　"
        "[🖼 浏览全部 187 张实景图册总览](/gallery)"
    )

    # 图带卡片被点：js_on_load 里发出 `trigger('select', {region: 行政区})`，
    # 这里用 gr.EventData 接住事件数据并回填目的地。
    #
    # 注意这里**故意不写 inputs**：
    #   · 写 inputs=[strip_out] 会让 Gradio 把整条图带（约 3.9MB HTML）
    #     当作参数回传服务端，既浪费带宽，fn 拿到的也是 HTML 而非行政区；
    #   · 数据走事件数据（gr.EventData）就够了，请求体只有几十字节。
    # 传 inputs=[] + 普通形参曾触发「Expected 1 arguments... received 0」告警，
    # 用 gr.EventData 类型注解则不会（Gradio 会自动注入事件数据）。
    #
    # api_visibility="undocumented"：这个端点只给前端 JS 用，
    # 不进「Use via API」文档（否则文档示例会把大段内容复制若干份，撑大主页）。
    strip_out.select(
        fn=show_pick_hint,
        outputs=[destination, pick_hint],
        api_visibility="undocumented",
    )

    # 直达面板芯片：与图带同一套 trigger('select', {region}) → show_pick_hint，
    # 同一个白名单校验、同一处回填提示，不另起新事件类型。
    explore.select(
        fn=show_pick_hint,
        outputs=[destination, pick_hint],
        api_visibility="undocumented",
    )

    # 「刷新换一批」：页面每次加载（含刷新）触发一次 demo.load，服务端重抽
    # 12 个芯片。不做定时器——只在刷新时变，会话中间不跳变（2026-10-11 老叶需求）。
    demo.load(
        fn=_explore_html,
        outputs=[explore],
        api_visibility="undocumented",
    )

    # 正式图册的点选：事件里是 GalleryImage 对象，且 caption 不带省份，
    # 所以行政区由图册自身的 gallery_region 状态给出
    gallery.select(
        fn=on_image_pick,
        inputs=[gallery_region],
        outputs=[destination, pick_hint],
    )

    # 路费卡 + 交通方式单选项：出发地 / 目的地 / 预算 / 天数 / 人数 / 方式 任一变化就即时重算。
    # 纯本地乘加运算、毫秒级返回，不碰模型，所以可以放心挂 change（数字组件没有 blur）。
    # 这几个输入都是小文本/数字/单选，不像图带那样会撑大主页，但仍标 undocumented：
    # 这是纯 UI 端点，没必要出现在「Use via API」文档里。
    _fare_inputs = [origin, destination, budget, days, people, fare_mode]
    for _comp in (origin, destination, budget, days, people, fare_mode):
        _comp.change(
            fn=update_fare,
            inputs=_fare_inputs,
            outputs=[fare_mode, fare_box],
            api_visibility="undocumented",
        )

    # 行前准备两张卡（行李清单 + 弹性保障）：目的地 / 天数 / 人数 / 兴趣任一变化就即时刷新
    # （纯规则计算，毫秒级）。合并成一次请求 outputs 两个组件，不会因为多了张卡就多一次往返。
    # 从图带点图回填目的地也会走到这里（destination.change 触发）。
    for _comp in (destination, days, people, interests):
        _comp.change(
            fn=update_prep_cards,
            inputs=[destination, days, people, interests],
            outputs=[packing_box, flex_box],
            api_visibility="undocumented",
        )

    submit_btn.click(
        fn=travel_planner,
        inputs=[origin, destination, days, budget, people, interests, additional_request,
                profile, fare_mode],
        outputs=[output, fare_box, gallery, strip_out, gallery_region],
    ).then(
        # 生成完的统一收尾：预算明细条 + 时间轴 + 行李清单 + 弹性保障 + 历史记录。
        # 用 .then 挂在后面而不是塞进 travel_planner，主流程的 outputs 就不用变长；
        # 且只有生成器完整跑完才执行，流式中途不会反复推大块 HTML。
        fn=after_generate,
        inputs=[output, origin, destination, days, budget, people, interests,
                fare_mode, profile],
        outputs=[budget_bar, timeline_box, packing_box, flex_box, history_dd,
                 gen_meta],
    )

    # 方案微调：三个受约束的局部重算，纯规则，不碰模型
    adj_swap_btn.click(
        fn=adjust_swap,
        inputs=[adj_day, adj_place, destination, days],
        outputs=[adj_result],
        api_visibility="undocumented",
    )
    adj_nights_btn.click(
        fn=adjust_nights,
        inputs=[adj_city, adj_delta, destination, days],
        outputs=[adj_result],
        api_visibility="undocumented",
    )
    adj_reorder_btn.click(
        fn=adjust_reorder,
        inputs=[destination, days],
        outputs=[adj_result],
        api_visibility="undocumented",
    )
    # 用调整后的新结构重新出完整方案（复用 travel_planner，只是换了目的地文本）
    adj_apply_btn.click(
        fn=apply_adjust_and_generate,
        inputs=[origin, budget, people, interests, additional_request, profile, fare_mode],
        outputs=[output, fare_box, gallery, strip_out, gallery_region],
        api_visibility="undocumented",
    ).then(
        fn=after_generate,
        inputs=[output, origin, destination, days, budget, people, interests,
                fare_mode, profile],
        outputs=[budget_bar, timeline_box, packing_box, flex_box, history_dd,
                 gen_meta],
        api_visibility="undocumented",
    )

    # 导出：Markdown / Word（下载按钮的 fn 返回文件路径即触发浏览器下载）
    dl_md.click(
        fn=export_plan_md,
        inputs=[output, origin, destination, days, budget, people, interests, fare_mode],
        outputs=[dl_md],
        api_visibility="undocumented",
    )
    dl_doc.click(
        fn=export_plan_doc,
        inputs=[output, origin, destination, days, budget, people, interests, fare_mode],
        outputs=[dl_doc],
        api_visibility="undocumented",
    )
    # 日历导出：每日行程 → 手机日历事件（参数同样走生成时快照）
    # outputs 带上 ics_msg / ics_file —— 成功提示「已导出 N 条」并给可点文件，
    # 失败提示具体原因（DownloadButton 本身失败时是静默的，必须靠这两路反馈）
    dl_ics.click(
        fn=export_plan_ics,
        inputs=[output, gen_meta],
        outputs=[dl_ics, ics_msg, ics_file],
        api_visibility="undocumented",
    )
    # 海报：点下载按钮直接出图下载，同时把预览放到页面上（右键/长按也能保存）
    # 参数来自 gen_meta（生成那一刻的快照），不取输入框现值 —— 避免生成后
    # 又点图册回填目的地，导致海报「目的地」与方案内容错位
    dl_pic.click(
        fn=make_poster_file,
        inputs=[output, gen_meta],
        outputs=[dl_pic],
        api_visibility="undocumented",
    ).then(
        fn=lambda p: gr.update(value=p, visible=bool(p)),
        inputs=[dl_pic],
        outputs=[poster_img],
        api_visibility="undocumented",
    )

    # 分享链接：入库 + 发短码，显示绝对链接（本地/线上按 Host 自动拼）
    share_btn.click(
        fn=make_share_link,
        inputs=[output],
        outputs=[share_msg],
        api_visibility="undocumented",
    )

    # 历史方案回看：选中下拉项 → 那份方案正文放回输出区（参数一并写入快照）
    history_dd.select(
        fn=load_history,
        outputs=[output, gen_meta],
        api_visibility="undocumented",
    )

    clear_btn.click(
        fn=clear_all,
        outputs=[
            destination, origin, days, budget, people, interests, additional_request, profile,
            output, fare_box, fare_mode, gallery, strip_out, gallery_region, pick_hint,
            budget_bar, packing_box, flex_box,
            timeline_box, share_msg, poster_img, ics_msg, ics_file, gen_meta,
        ],
    )

# ---------------------------------------------------------------------------
# 分享只读页（/s/<短码>）
#
# 挂在 Gradio 的 FastAPI app（demo.app）上，与主界面同一个端口：
#   · 云端沙箱只给一个 HTTP 端口，不能也不需要另起服务；
#   · 只读：路由里只做 SELECT，不暴露任何写接口；
#   · 短码 8 位（去易混字符），加 hits 计数便于知道被看过几次。
# ---------------------------------------------------------------------------
_SHARE_PAGE_CSS = """
body{margin:0;background:#f6faf9;font-family:'Microsoft YaHei',system-ui,-apple-system,sans-serif;color:#1f2937}
.wrap{max-width:820px;margin:0 auto;padding:28px 18px 48px}
.brand{font-size:14px;color:#0d9488;font-weight:700;letter-spacing:.5px;margin-bottom:14px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:24px 26px;margin-bottom:16px}
h1.plan{font-size:22px;margin:0 0 10px;color:#111827;line-height:1.4}
.meta{font-size:13px;color:#64748b;margin-bottom:4px}
.meta b{color:#0f766e}
.tl-card{margin:0 0 16px;padding:15px 16px 13px;background:linear-gradient(180deg,#f0fdfa,#fff 62%);border:1px solid #99f6e4;border-radius:14px}
.tl-head{font-size:15px;font-weight:700;color:#0f766e;margin-bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tl-count{font-size:11.5px;color:#0d9488;background:#ccfbf1;border-radius:999px;padding:2px 10px}
.tl-list{display:flex;flex-direction:column;gap:12px}
.tl-day{display:flex;gap:12px;background:#fff;border:1px solid #e5e7eb;border-radius:11px;padding:11px 13px}
.tl-badge{flex:0 0 auto;font-size:13px;font-weight:700;color:#fff;background:#0d9488;border-radius:9px;padding:6px 12px}
.tl-stops{display:flex;flex-direction:column}
.tl-stop{display:flex;align-items:center;gap:9px}
.tl-dot{flex:0 0 9px;width:9px;height:9px;border-radius:50%;background:#14b8a6;box-shadow:0 0 0 3px #ccfbf1}
.tl-name{font-size:13.5px;line-height:1.45}
.tl-link{width:2px;height:12px;background:#99f6e4;margin-left:3.5px;border-radius:1px}
.tl-meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}
.tl-tag{font-size:11.5px;border-radius:7px;padding:3px 9px}
.tl-buf{color:#854d0e;background:#fef9c3}
.tl-bak{color:#1e40af;background:#dbeafe}
.body h1{font-size:19px;margin:20px 0 8px}
.body h2{font-size:16.5px;margin:18px 0 8px;color:#0f766e;border-bottom:1px solid #e5e7eb;padding-bottom:4px}
.body h3{font-size:14.5px;margin:14px 0 6px}
.body ul{margin:6px 0;padding-left:22px}
.body li{margin:3px 0;line-height:1.65}
.body p{line-height:1.7;margin:8px 0}
.body blockquote{background:#fffbeb;border-left:4px solid #f59e0b;margin:8px 0;padding:6px 12px;color:#78350f}
.body hr{border:none;border-top:1px solid #e5e7eb;margin:14px 0}
.foot{font-size:12px;color:#94a3b8;text-align:center;margin-top:18px}
.print-btn{position:fixed;right:18px;bottom:18px;z-index:9;background:#0d9488;color:#fff;
border:none;border-radius:999px;padding:12px 21px;font-size:14px;font-weight:700;
cursor:pointer;font-family:inherit;box-shadow:0 6px 18px rgba(13,148,136,.35)}
.print-btn:hover{background:#0f766e}
/* 打印：去掉屏幕上的装饰，把「元信息 + 时间轴」压成干净的一页行程单 */
@media print{
  body{background:#fff}
  .wrap{max-width:100%;padding:0}
  .print-btn,.foot{display:none!important}
  .card{border:none;border-radius:0;padding:0;margin:0 0 9px;break-inside:avoid}
  h1.plan{font-size:17pt;margin-bottom:6px}
  .brand{margin-bottom:8px}
  .tl-card{border:1px solid #cbd5e1;background:#fff;padding:10px 12px 8px;break-inside:avoid}
  .tl-day{break-inside:avoid;padding:7px 9px}
  .tl-stop{margin:1px 0}
  .tl-name{font-size:10.5pt}
  .body{font-size:10.5pt}
  .body h2{font-size:12pt;border-bottom:1px solid #94a3b8}
  .body h3{font-size:11pt}
  .body p,.body li{line-height:1.5}
  .body ul{padding-left:18px}
  .body blockquote{background:#f8fafc;border-left:3px solid #94a3b8;color:#334155}
  a{color:#0f766e;text-decoration:none}
  @page{margin:12mm}
}
"""

_SHARE_SHELL = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s · 旅游方案分享</title>
<style>%s</style>
</head><body><div class="wrap">%s
<div class="foot">本方案由「智慧旅游规划助手」AI 检索本地知识库生成 · 价格与开放时间以景区官方公布为准</div>
</div>
<button class="print-btn" onclick="window.print()">🖨 打印 / 存 PDF</button>
</body></html>"""


def _share_page_html(plan):
    """把一份方案渲染成只读分享页（时间轴 + 正文全文）"""
    md = plan["content"]
    meta_bits = []
    if plan.get("destination"):
        meta_bits.append("📍 目的地 <b>%s</b>" % _escape(plan["destination"]))
    if plan.get("origin"):
        meta_bits.append("🚩 出发地 <b>%s</b>" % _escape(plan["origin"]))
    if plan.get("days"):
        meta_bits.append("🗓 <b>%d</b> 天" % int(plan["days"] or 1))
    if plan.get("people"):
        meta_bits.append("👥 <b>%d</b> 人" % int(plan["people"] or 1))
    if plan.get("budget"):
        meta_bits.append("💰 人均预算 <b>%s</b> 元" % _escape(plan["budget"]))

    head = (
        "<div class='card'>"
        "<div class='brand'>🌍 智慧旅游规划助手 · 分享方案</div>"
        "<h1 class='plan'>%s</h1>" % _escape(_strip_md_lite(
            (md.splitlines() or ["旅游方案"])[0].lstrip("#").strip()))
    )
    if meta_bits:
        head += "<div class='meta'>%s</div>" % "　".join(meta_bits)
    head += "</div>"

    tl = build_timeline_html(md, plan.get("destination") or "")
    # 时间轴 CSS 类名与页面里共用，但分享页是独立 HTML，需带自己的样式作用域：
    # build_timeline_html 输出的类名与 _SHARE_PAGE_CSS 中一致，直接可用。
    body = "<div class='card body'>%s</div>" % md_to_html(md)
    return _SHARE_SHELL % (_escape(plan.get("title") or "旅游方案"),
                           _SHARE_PAGE_CSS, head + tl + body)


def _strip_md_lite(s):
    return re.sub(r"\*\*(.+?)\*\*", r"\1", str(s or "")).strip()


def _attach_share_route():
    """把 /s/<code> 挂到 Gradio 的 FastAPI app 上（幂等）"""
    from fastapi import Request
    from fastapi.responses import HTMLResponse

    app = demo.app
    if any(getattr(r, "path", "") == "/s/{code}" for r in app.routes):
        return

    @app.get("/s/{code}", response_class=HTMLResponse, include_in_schema=False)
    def _share_page(code: str, request: Request):
        plan, status = resolve_share(code)
        if status != "ok" or not plan:
            return HTMLResponse(_SHARE_SHELL % (
                "链接失效", _SHARE_PAGE_CSS,
                "<div class='card'><div class='brand'>🌍 智慧旅游规划助手</div>"
                "<h1 class='plan'>😢 分享链接不存在或已失效</h1>"
                "<div class='meta'>请向分享者确认链接是否完整（应为 8 位短码）。</div></div>"),
                status_code=404)
        return HTMLResponse(_share_page_html(plan))


def _attach_gallery_route():
    """把「兴趣图册总览」（34 区 × 6 兴趣 × 187 张实景图）挂成站内页 /gallery

    总览页是离线生成的静态 HTML，图片原是相对路径 assets/images/...；
    挂到站内后统一改写成 /gallery-img/... 由本路由提供，只放行图册用到的
    图片，不把整个 assets 目录对外开放（与 launch 的 allowed_paths 口径一致）。
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    app = demo.app
    if any(getattr(r, "path", "") == "/gallery" for r in app.routes):
        return

    root = os.path.dirname(os.path.abspath(__file__))
    overview = os.path.join(root, "assets", "gallery_overview.html")
    images_root = os.path.abspath(os.path.join(root, "assets", "images"))

    @app.get("/gallery", response_class=HTMLResponse, include_in_schema=False)
    def _gallery_page():
        try:
            with open(overview, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return HTMLResponse("<h3>图册总览尚未生成</h3>", status_code=404)
        return HTMLResponse(html.replace('src="assets/images/',
                                         'src="/gallery-img/'))

    @app.get("/gallery-img/{path:path}", include_in_schema=False)
    def _gallery_img(path: str):
        full = os.path.abspath(os.path.join(images_root, path))
        # 防目录穿越：只允许 assets/images 之下的真实文件
        if not full.startswith(images_root + os.sep) or not os.path.isfile(full):
            raise HTTPException(status_code=404)
        return FileResponse(full)


def _lan_ip():
    """取本机在局域网里的 IPv4 地址，供同网段的手机直连"""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不会真的发包，只是让系统按路由表挑一张对外的网卡
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


if __name__ == "__main__":
    # 三种启动模式：
    #   默认      仅本机访问（127.0.0.1）
    #   --lan     同局域网可访问（手机连同一个 Wi-Fi 即可打开，最省事）
    #   --share   申请 Gradio 自带公网链接；它依赖 frpc 二进制（走 huggingface CDN，
    #             国内实测下载不到）。需要跨网络访问请直接用 tools/start_public.py，
    #             那条走 cloudflared 隧道，已验证可用。
    argv = set(sys.argv[1:])
    lan_mode = "--lan" in argv or os.environ.get("TRAVEL_LAN") == "1"
    share_mode = "--share" in argv or os.environ.get("TRAVEL_SHARE") == "1"

    # 端口优先级：TRAVEL_PORT（本机自用）> PORT（云平台注入的约定变量）> 7860
    # 云端发布沙箱只给一个 HTTP 端口并以 PORT 注入，所以这里必须认它。
    port = int(os.environ.get("TRAVEL_PORT")
               or os.environ.get("PORT")
               or 7860)

    # 监听 0.0.0.0 才能被本机以外的设备访问。
    # 云端发布时 PORT 由平台注入，此时也必须绑 0.0.0.0，否则反向代理连不上。
    cloud_mode = bool(os.environ.get("PORT")) and not os.environ.get("TRAVEL_PORT")
    host = "0.0.0.0" if (lan_mode or share_mode or cloud_mode) else "127.0.0.1"

    # 公网链接是公开的：任何人拿到地址都能用，会消耗你的模型 API 额度。
    # 所以 share 模式默认加一道口令，想换密码设环境变量 TRAVEL_PWD 即可。
    auth = None
    if share_mode:
        auth = ("travel", os.environ.get("TRAVEL_PWD") or "travel2026")

    print("=" * 62)
    print("  智慧旅游规划助手  启动中")
    print("=" * 62)
    print("  本机访问    : http://127.0.0.1:%d" % port)
    if lan_mode or share_mode:
        print("  手机同网访问: http://%s:%d" % (_lan_ip(), port))
    if share_mode:
        print("  公网访问    : 稍等几秒，看下方输出的 *.gradio.live 链接")
        print("  公网账号    : travel    密码: %s" % auth[1])
    print("=" * 62)

    # 后台预热向量库：用户第一次点击「生成旅游方案」时就不必再等向量库加载
    threading.Thread(target=warmup, daemon=True).start()

    # 方案数据库建表（分享页路由必须在 launch 之后挂——launch 会新建 FastAPI
    # app，提前挂的路由会被丢掉，实测 /s/<code> 404 就是这个原因）
    init_db()

    # Gradio 6.0 起 theme / css 等外观参数需传给 launch()，不再放在 Blocks() 构造函数中。
    # allowed_paths 只放行「待机图带用到的那几张图」——过去放行整个 assets 目录，
    # 等于把 18MB 图库全部对外开放，没必要。
    #
    # prevent_thread_lock=True：launch 只起服务不锁线程，随后把分享页路由挂上，
    # 最后 block_thread() 阻塞主线程——对外的行为与原来的直接 launch 完全一致。
    demo.queue().launch(
        server_name=host,
        server_port=port,
        share=share_mode,
        auth=auth,
        theme=gr.themes.Soft(),
        css=MOBILE_CSS,
        allowed_paths=[p for p, _ in _STRIP_FILES],
        prevent_thread_lock=True,
    )

    _attach_share_route()
    print("  分享页路由  : /s/<短码>  （生成方案后点「生成分享链接」即可拿到）")
    _attach_gallery_route()
    print("  图册总览页  : /gallery   （34 区 × 6 兴趣 × 187 张实景图）")
    print("=" * 62)

    demo.block_thread()
