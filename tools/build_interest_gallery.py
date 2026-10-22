# -*- coding: utf-8 -*-
"""
兴趣图册构建脚本
================
目标：让每个省级行政区在 6 类兴趣偏好（自然风光 / 历史文化 / 美食探店 /
购物娱乐 / 亲子游 / 户外探险）上**都有对应的实景图**。

信息来源（全部来自本地知识库，不凭空造景点）
------------------------------------------
1. 「XX-主攻略.md」的「热门景点」章节 —— 该区完整候选景点清单（8~14 个）。
2. 「XX-路线库.md」的「兴趣路线 · XX」章节 —— 每类兴趣路线里写到的景点，
   天然构成「兴趣 → 景点」映射（最强信号，权重 3）。
3. 「XX-主攻略.md」的「特色美食」章节 —— 作为「美食探店」的候选（权重 3）。
4. 景点名/正文关键词规则 —— 补充信号（标题命中 2，正文命中 1）。

选图策略
--------
每个区逐兴趣检查覆盖情况：
- 弱信号（<2 分）的景点**不抓图**，宁可复用同区图片并追加兴趣标签，
  避免出现「美食探店配胡杨林」这类跑题配图；
- 强信号优先抓取新图，同一景点已抓过则直接复用（一图多兴趣）。

产出
----
- 缺失配图按需抓取，追加到 assets/images/<类别>/<区>/；
- 重建 assets/images/index.json，每张图带 interests 字段，
  保证每区 6 类兴趣各自至少一张图。

用法
----
    python tools/build_interest_gallery.py --plan          # 只看缺口，不抓图
    python tools/build_interest_gallery.py                 # 补齐缺口并重建索引
    python tools/build_interest_gallery.py --regions 重庆,四川
"""
import argparse
import json
import os
import random
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_spots_images import (  # noqa: E402
    INDEX_PATH, KB_DEST, MIN_BYTES, OUT_ROOT,
    core_key, detect_ext, enlarge, http_get, pick_item, search_baidu,
)

INTERESTS = ["自然风光", "历史文化", "美食探店", "购物娱乐", "亲子游", "户外探险"]

# 兴趣 → 关键词（用于打分，不直接决定是否抓图）
KEYWORD_RULES = {
    "自然风光": ["山", "湖", "海", "江", "河", "森林", "草原", "湿地", "天池", "冰川",
                 "雪", "滩", "岛", "湾", "泉", "瀑布", "峡", "谷", "桃", "花", "梯田",
                 "日出", "日落", "风光", "林"],
    "历史文化": ["寺", "庙", "宫", "塔", "古城", "古镇", "博物", "遗址", "石窟", "陵",
                 "碑", "堂", "祠", "府", "楼", "关", "城墙", "故居", "文化", "历史",
                 "美术馆", "书院", "老城", "旧城", "老街", "牌坊", "八廓"],
    "美食探店": ["美食", "小吃", "食街", "夜市", "餐厅", "菜", "茶", "火锅", "烧烤",
                 "米线", "点心", "海鲜", "市场", "啤酒", "葡萄", "馕", "拉面", "早餐",
                 "咖啡", "甜品"],
    "购物娱乐": ["购物", "商圈", "步行街", "免税", "乐园", "影城", "演出", "剧院",
                 "夜游", "灯光", "天际", "摩天", "夜市", "商业", "巴扎", "太古里",
                 "国贸", "大街"],
    "亲子游": ["动物", "海洋", "水族", "科技", "乐园", "基地", "博物院", "博物馆",
               "植物", "公园", "广场", "欢乐"],
    "户外探险": ["徒步", "登山", "峡谷", "漂流", "滑雪", "沙漠", "索道", "攀岩",
                 "露营", "骑行", "雪场", "溶洞", "洞穴", "丛林", "越野", "山", "峡",
                 "峰", "火山", "高原", "大本营", "步道", "徒步径"],
}

SPLIT_RE = re.compile(r"[与、及]|/|＋|\+")
PAREN_RE = re.compile(r"[（(].*?[）)]")

# 「特色美食」章节里被加粗、但其实是排版提示而非菜名的，要排掉
FOOD_JUNK = ["推荐区域", "觅食", "提示", "口味", "人均", "区域", "四大天王", "代表",
             "注意", "价格", "必吃", "地图", "清单", "总结", "特色", "人均价",
             "、", "，", "。", "：", "；",
             # 短语式排版加粗（如「夜市与小吃是核心体验」「粤菜的核心区之一」）
             "核心", "之一", "体验", "是", "的", "以及", "或者", "最爱", "首选"]

# 「住宿建议」里的加粗片区，排掉明显不是商业区的（交通枢纽、纯居民区提示等）
AREA_JUNK = ["建议", "提示", "注意", "性价比", "交通", "机场", "火车站", "高铁",
             "住宿", "预算", "适合", "优点", "缺点", "综合",
             # 过于笼统、搜出来会跑题的说法
             "市中心", "市区", "老城", "县城", "景区内", "区域", "附近", "周边",
             # 住宿业态本身（客栈/酒店），以及村镇、景区门口等非商业地名
             "客栈", "酒店", "民宿", "宾馆", "餐厅", "旅馆", "度假村", "公寓",
             "景区", "门口", "镇", "村", "站"]

# 商业区候选必须带商业地标特征词，否则住宿建议里的片区名（如「寨脚/河岸客栈」）
# 会被当成商圈去搜图，搜出来的当然是村寨和酒店
AREA_MARKERS = ["街", "广场", "商圈", "中心", "区", "路", "大道", "太古里",
                "国贸", "商", "城"]

# 明显不搭的搭配：某兴趣的配图**不要**选名字里带这些词的景点
# （例外：该景点名字本身就命中该兴趣关键词时不受限，如「大连星海广场」配购物娱乐）
MISMATCH = {
    "购物娱乐": ["寺", "庙", "宫", "陵", "祠", "故居", "遗址", "石窟", "城墙",
                 "长城", "博物", "纪念馆", "草原", "湿地", "湖", "森林", "公园",
                 "山", "谷", "岛"],
    "亲子游": ["陵", "遗址", "故居", "石窟", "古城"],
    "户外探险": ["博物", "纪念馆", "故居", "剧院", "免税"],
    "自然风光": ["博物", "纪念馆", "故居", "剧院", "商圈", "免税"],
}


def log(msg):
    print(msg, flush=True)


def clean_name(title):
    """景点名规整：去括号内容、去「离岛：」这类前缀。

    注意：**不能按「和」拆**——「颐和园」「太和殿」都会被拆坏（历史遗留 bug）。
    """
    s = PAREN_RE.sub("", title).strip()
    if "：" in s or ":" in s:
        head, tail = re.split(r"[：:]", s, maxsplit=1)
        if len(tail.strip()) >= 2:
            s = tail.strip()
    s = re.split(r"\s+-\s+", s)[0].strip()
    return s or title.strip()


def name_tokens(title):
    """生成用于匹配的名字片段集合。

    只有当拆分后每段都 ≥2 字时才使用拆分结果，否则保留原名
    （避免把「颐和园」拆成「颐」「园」）。
    """
    base = clean_name(title)
    toks = {base}
    parts = [p.strip() for p in SPLIT_RE.split(base) if p.strip()]
    if len(parts) > 1 and all(len(p) >= 2 for p in parts):
        toks.update(parts)
    # 带城市前缀的长名，补一个去掉前 2 字的版本（「天水麦积山石窟」→「麦积山石窟」）
    for t in list(toks):
        if len(t) >= 5:
            toks.add(t[2:])
            core = core_key(t)[0]
            if core and core != t and len(core) >= 2:
                toks.add(core)
    return {t for t in toks if len(t) >= 2}


def parse_guide(region_path):
    """主攻略 → 热门景点清单 + 特色美食清单 + 商业/觅食区清单

    美食章节各目的地写法不一：有的按城市分 `###`，有的直接编号列菜名。
    统一从**加粗菜名**里取候选（`**北京烤鸭**`、`**桂林米粉**`），
    并过滤「推荐区域」这类排版用加粗。

    另外从「住宿建议」取加粗片区名（如「国贸 / 三里屯」「前门 / 王府井片区」）
    作为购物娱乐候选——这些商业区是知识库自己写明的，不是凭空编的名字。
    """
    text = open(region_path, encoding="utf-8").read()
    spots, foods, areas = [], [], []
    sec = re.search(r"^##\s*热门景点\s*\n(.*?)(?=\n##\s|\Z)", text, re.S | re.M)
    if sec:
        for block in re.finditer(r"^###\s*(.+?)\s*\n(.*?)(?=\n###\s|\Z)", sec.group(1), re.S | re.M):
            spots.append({"title": block.group(1).strip(),
                          "text": block.group(1) + "\n" + block.group(2)})
    fsec = re.search(r"^##\s*特色美食\s*\n(.*?)(?=\n##\s|\Z)", text, re.S | re.M)
    if fsec:
        seen = set()
        for name in re.findall(r"\*\*(.+?)\*\*", fsec.group(1)):
            cand = PAREN_RE.sub("", name).strip()
            if not (2 <= len(cand) <= 10) or cand in seen:
                continue
            if any(bad in cand for bad in FOOD_JUNK):
                continue
            seen.add(cand)
            foods.append(cand)
    hsec = re.search(r"^##\s*住宿建议\s*\n(.*?)(?=\n##\s|\Z)", text, re.S | re.M)
    if hsec:
        for name in re.findall(r"\*\*(.+?)\*\*", hsec.group(1)):
            cand = PAREN_RE.sub("", name).strip()
            if not (2 <= len(cand) <= 12) or cand in areas:
                continue
            if any(bad in cand for bad in AREA_JUNK) or "元" in cand:
                continue
            if not any(mk in cand for mk in AREA_MARKERS):
                continue
            areas.append(cand)
    return spots, foods, areas[:4]


def parse_route_lib(route_path):
    """路线库 → {兴趣: 该兴趣路线章节正文}"""
    if not os.path.exists(route_path):
        return {}
    text = open(route_path, encoding="utf-8").read()
    sections = {}
    for m in re.finditer(r"^##\s*兴趣路线\s*[·・\.]\s*(\S+?)\s*\n(.*?)(?=\n##\s|\Z)",
                         text, re.S | re.M):
        name = m.group(1).strip()
        if name in INTERESTS:
            sections[name] = m.group(2)
    return sections


def tag_spots(region, guide_path, route_path):
    """给景点打兴趣标签（收敛版，避免「一图贴 5 类兴趣」）。

    计分：路线库命中 3 分、标题关键词 2 分、正文关键词 1 分。
    最终标签规则：
      1. 保留**标题关键词**命中的兴趣（最强语义信号，如「峨眉山」→ 自然风光）；
      2. 再补**一个**路线库提及最多的兴趣（出现在 2 个以上兴趣节里的多是
         沿途共用站点，如「厦门大学」，不能算作某一类的专属图）；
      3. 标签最多 3 个。
    """
    spots, foods, areas = parse_guide(guide_path)
    sections = parse_route_lib(route_path)
    for sp in spots:
        sp["kind"] = "spot"
        sp["scores"] = {it: 0 for it in INTERESTS}
        sp["route_cnt"] = {it: 0 for it in INTERESTS}
        toks = name_tokens(sp["title"])
        for it, body in sections.items():
            cnt = sum(body.count(t) for t in toks)
            if cnt:
                sp["route_cnt"][it] = cnt
                sp["scores"][it] = max(sp["scores"][it], 3)
        for it, keys in KEYWORD_RULES.items():
            if any(k in sp["title"] for k in keys):
                sp["scores"][it] = max(sp["scores"][it], 2)
                sp.setdefault("title_hit", set()).add(it)
            elif any(k in sp["text"] for k in keys):
                sp["scores"][it] = max(sp["scores"][it], 1)

        tags = set(sp.get("title_hit", set()))
        if not tags:                                   # 标题没命中，取路线库最强的那个
            best = sorted(INTERESTS, key=lambda it: -sp["route_cnt"][it])
            if sp["route_cnt"][best[0]]:
                tags.add(best[0])
            else:                                      # 全靠正文关键词
                body_hits = [it for it in INTERESTS if sp["scores"][it] >= 1]
                tags.add(body_hits[0] if body_hits else "历史文化")
        else:                                          # 标题有命中，再补一个路线库兴趣
            rest = sorted([it for it in INTERESTS if it not in tags],
                          key=lambda it: -sp["route_cnt"][it])
            if rest and sp["route_cnt"][rest[0]] >= 2:
                tags.add(rest[0])
        sp["interests"] = set(list(tags)[:3])
        if not sp["interests"]:
            sp["interests"] = {"历史文化"}
    for f in foods[:4]:
        spots.append({"title": f, "text": f, "kind": "food",
                      "scores": {**{it: 0 for it in INTERESTS}, "美食探店": 3},
                      "interests": {"美食探店"}})
    for a in areas:
        spots.append({"title": a, "text": a, "kind": "area",
                      "scores": {**{it: 0 for it in INTERESTS}, "购物娱乐": 2},
                      "interests": {"购物娱乐"}})
    return spots


def collect_regions():
    out = []
    for cat in sorted(os.listdir(KB_DEST)):
        cat_dir = os.path.join(KB_DEST, cat)
        if not os.path.isdir(cat_dir):
            continue
        for fn in sorted(os.listdir(cat_dir)):
            if not fn.endswith("-主攻略.md"):
                continue
            region = fn[: -len("-主攻略.md")]
            out.append({"region": region, "category": cat,
                        "guide": os.path.join(cat_dir, fn),
                        "route": os.path.join(cat_dir, "%s-路线库.md" % region)})
    return out


def load_index():
    if os.path.exists(INDEX_PATH):
        return json.load(open(INDEX_PATH, encoding="utf-8"))
    return {"source": "百度图片搜索（版权归原作者，仅限学习与内部演示）",
            "destinations": []}


def spot_interests(name, spots):
    """把一张图的景点名映射回知识库条目，取兴趣标签"""
    hits = set()
    for sp in spots:
        for tok in name_tokens(sp["title"]):
            if tok and (tok in name or name in tok):
                hits |= set(sp["interests"])
                break
    return hits


def pick_target(cands, have_now):
    """从候选中挑一个还没配图的景点；只看强信号（≥2 分）"""
    for sp in cands:
        nm = clean_name(sp["title"])
        if any(nm in im["spot"] or im["spot"] in nm for im in have_now):
            continue
        return sp
    return None


def fetch_one(region, cat, picked, entry, have_now, index_no, target):
    """抓一张图并登记到 entry，成功返回 True。target 为本次补的興趣标签"""
    spot = clean_name(picked["title"])
    # 商业区名常写成「前门 / 王府井片区」，检索时只取第一个地名，命中率更高
    search_term = re.split(r"[/／、]", spot)[0].strip() or spot
    core, keys = core_key(search_term)
    base = "%02d-%s" % (index_no, re.sub(r'[\\/:*?"<>|]', "_", spot))
    for kw in ("%s %s" % (search_term, region), search_term):
        try:
            items = search_baidu(kw)
            item, conf = pick_item(items, keys)
            if not item:
                continue
            data = http_get(enlarge(item["url"]), ref="https://image.baidu.com/")
            if len(data) < MIN_BYTES:
                data = http_get(item["url"], ref="https://image.baidu.com/")
            ext = detect_ext(data)
            if not ext or len(data) < MIN_BYTES:
                continue
            region_dir = os.path.join(OUT_ROOT, cat, region)
            os.makedirs(region_dir, exist_ok=True)
            dst = os.path.join(region_dir, base + ext)
            with open(dst, "wb") as f:
                f.write(data)
            rec = {
                "spot": spot,
                "raw_title": picked["title"],
                "file": os.path.relpath(dst, OUT_ROOT).replace("\\", "/"),
                "source_url": item["url"],
                "hit_title": item["title"][:80],
                "is_cover": False,
                "confidence": "high" if conf else "low",
                "interests": [target],
            }
            entry["images"].append(rec)
            have_now.append(rec)
            log("      [OK] %-24s %.0fKB  %s" % (spot, len(data) / 1024, item["title"][:34]))
            return True
        except Exception as exc:
            log("      [重试] %s : %s" % (kw, str(exc)[:50]))
        time.sleep(random.uniform(0.6, 1.1))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="只输出缺口计划，不抓图")
    ap.add_argument("--regions", default="", help="只处理这些目的地（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="最多抓多少张（调试用）")
    args = ap.parse_args()

    only = {s.strip() for s in args.regions.split(",") if s.strip()}
    regions = collect_regions()
    if only:
        regions = [r for r in regions if r["region"] in only]

    index = load_index()
    idx_map = {e["region"]: e for e in index.get("destinations", [])}
    fetched = failed = planned = 0

    for r in regions:
        region, cat = r["region"], r["category"]
        spots = tag_spots(region, r["guide"], r["route"])
        entry = idx_map.get(region) or {"region": region, "category": cat, "images": []}
        entry.setdefault("images", [])
        have = [im for im in entry["images"] if im.get("file")
                and os.path.exists(os.path.join(OUT_ROOT, im["file"]))]

        covered = {}
        for im in have:
            # 覆盖判断必须同时看**已存标签**（含上一轮「复用」补的），
            # 否则每轮都会把已覆盖的兴趣当成缺口，反复抓出跑题的图
            tags = spot_interests(im["spot"], spots) | set(im.get("interests") or [])
            for it in tags:
                covered.setdefault(it, []).append(im)
        missing = [it for it in INTERESTS if it not in covered]
        if not missing:
            log("  [齐了] %s（%d 张图，6 类兴趣全覆盖）" % (region, len(have)))
            continue

        log("\n=== %s（%s）缺 %d 类：%s ===" % (region, cat, len(missing), "、".join(missing)))
        next_no = len(have) + 1
        for it in missing:
            if it in covered:
                # 同一轮里前一个兴趣抓到的图已顺带覆盖了它（一图多标签），无需再抓
                log("  [已覆盖] %s：由前一张图顺带覆盖" % it)
                continue
            # 强信号候选：路线库写到的景点（或特色美食），分数 ≥2。
            # 美食类优先取真正的菜品（搜出来就是食物特写），其余优先取景点。
            want_kind = "food" if it == "美食探店" else "spot"
            cands = [sp for sp in spots
                     if it in sp["interests"] and sp.get("scores", {}).get(it, 0) >= 2]
            # 排掉明显不搭的搭配（景点名命中该兴趣关键词的除外）；
            # 若排完一个都不剩，就退回未过滤候选，宁可搭配略勉强也不硬凑无关图
            blocked = MISMATCH.get(it, [])
            if blocked:
                strict = [sp for sp in cands
                          if it in sp.get("title_hit", set())
                          or not any(b in clean_name(sp["title"]) for b in blocked)]
                cands = strict or cands
            cands.sort(key=lambda sp: (-sp["scores"][it], 0 if sp.get("kind") == want_kind else 1))
            picked = pick_target(cands, have)
            if not picked:
                log("  [复用] %s：无强相关新景点，稍后用同区图片追加标签" % it)
                continue
            planned += 1
            if args.plan:
                log("  [计划] %-6s → %s（%d 分）"
                    % (it, clean_name(picked["title"]), picked["scores"][it]))
                continue
            if args.limit and fetched + failed >= args.limit:
                log("  [截断] 达到 --limit 上限")
                break
            log("  → %s：%s" % (it, clean_name(picked["title"])))
            if fetch_one(region, cat, picked, entry, have, next_no, it):
                fetched += 1
                next_no += 1
                covered.setdefault(it, []).append(have[-1])
            else:
                failed += 1
            time.sleep(random.uniform(0.5, 1.0))

        entry["images"] = [im for im in entry["images"] if im.get("file")]
        idx_map[region] = entry

    if args.plan:
        log("\n合计需要补抓 %d 张（复用不计）" % planned)
        return

    # ---- 重建索引：补 interests 字段 + 兜底复用 ----
    log("\n--- 重建索引与兴趣覆盖 ---")
    rebuilt = []
    for r in collect_regions():
        region = r["region"]
        entry = idx_map.get(region)
        if not entry:
            continue
        spots = tag_spots(region, r["guide"], r["route"])
        imgs = [im for im in entry["images"] if im.get("file")]
        for i, im in enumerate(imgs):
            # 标签顺序 = 抓图用途优先 → 知识库推导补充，最多 3 类。
            # 注意不能用 sorted() 后截断：中文按 Unicode 排序会把「购物娱乐」排到最后而被误删。
            kept = [t for t in (im.get("interests") or []) if t in INTERESTS]
            derived = [t for t in INTERESTS if t in spot_interests(im["spot"], spots)]
            ordered = kept + [t for t in derived if t not in kept]
            im["interests"] = ordered[:3] or ["历史文化"]
            im["is_cover"] = (i == 0)

        cov = {}
        for im in imgs:
            for t in im["interests"]:
                cov.setdefault(t, []).append(im)
        reused = []
        for it in INTERESTS:
            if it in cov or not imgs:
                continue
            donor = min(imgs, key=lambda im: len(im["interests"]))
            donor["interests"] = sorted(set(donor["interests"]) | {it})
            cov.setdefault(it, []).append(donor)
            reused.append("%s←%s" % (it, donor["spot"]))
        rebuilt.append({"region": region, "category": entry["category"], "images": imgs})
        gaps = [it for it in INTERESTS if it not in cov]
        log("  %-6s 图 %2d 张 ｜ 覆盖 %d/6%s%s"
            % (region, len(imgs), 6 - len(gaps),
               ("  ｜ 缺 " + "、".join(gaps)) if gaps else "",
               ("  ｜ 复用 " + "；".join(reused)) if reused else ""))

    out = {"source": index.get("source", ""), "interests": INTERESTS,
           "destinations": sorted(rebuilt, key=lambda e: (e["category"], e["region"]))}
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    log("\n" + "=" * 56)
    log("新抓 %d ｜ 失败 %d ｜ 目的地 %d 个" % (fetched, failed, len(rebuilt)))
    log("索引：%s" % INDEX_PATH)
    log("=" * 56)


if __name__ == "__main__":
    main()
