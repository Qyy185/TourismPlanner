# -*- coding: utf-8 -*-
"""
景点图片抓取脚本
================
按知识库每个「XX-主攻略.md」的「热门景点」章节，为每个省级行政区抓取
代表景点图片，供 Gradio 界面的「目的地实景图册」使用。

设计原则
--------
1. 图片**不进知识库**：只落在 assets/images/ 下，knowledge_base/ 一个字不改，
   避免 markdown 图片语法被切片器当成正文、污染向量检索。
2. 幂等：已存在的图片默认跳过，可重复运行。
3. 可溯源：每张图记录来源 URL 与命中标题，便于核对。

图片来源与用途
--------------
图片来源为百度图片搜索结果（搜索引擎聚合），**图片版权归原作者所有**。
仅限学习、教学与内部演示使用，不得用于公开传播或商业用途。

用法
----
    python tools/fetch_spots_images.py                 # 全部 34 个目的地
    python tools/fetch_spots_images.py --regions 重庆   # 只跑指定目的地（逗号分隔）
    python tools/fetch_spots_images.py --force          # 覆盖已存在的图片
"""
import argparse
import io
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DEST = os.path.join(ROOT, "knowledge_base", "destinations")
OUT_ROOT = os.path.join(ROOT, "assets", "images")
INDEX_PATH = os.path.join(OUT_ROOT, "index.json")

SPOTS_PER_REGION = 3          # 每个目的地抓 3 张（第 1 张兼作封面）
MIN_BYTES = 12 * 1024         # 小于 12KB 视为坏图
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# 去掉通用后缀后剩下的才是检索核心词
SUFFIXES = [
    "繁育研究基地", "国家森林公园", "国家级自然保护区", "风景名胜区",
    "大峡谷", "风景区", "旅游区", "景区", "古镇", "古城", "公园", "博物馆",
    "纪念馆", "陈列馆", "森林公园", "自然保护区", "索道", "观景台",
    "遗址", "大佛", "石刻", "梯田", "草原", "滑雪场", "温泉", "步行街",
    "步行街", "老街", " cathedral",
]


def log(msg):
    print(msg, flush=True)


def http_get(url, ref=None, timeout=25):
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if ref:
        headers["Referer"] = ref
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=CTX).read()


def clean_spot(name):
    """把「解放碑与八一路好吃街」这类组合景点名收敛成一个检索主体"""
    s = re.split(r"[与、和]|（|\(|\s+-\s+", name)[0].strip()
    s = re.sub(r"[（(].*?[）)]", "", s).strip()
    return s or name.strip()


def core_key(spot):
    """检索核心词：去掉通用后缀。同时返回用于校验标题的关键词列表"""
    core = spot
    for suf in SUFFIXES:
        if core.endswith(suf) and len(core) > len(suf):
            core = core[: -len(suf)]
            break
    # 校验关键词：核心词本身，或核心词中长度>=2 的片段
    keys = [core] if len(core) >= 2 else []
    if len(core) >= 4:
        keys.append(core[:2])
        keys.append(core[-2:])
    return core, keys


def search_baidu(keyword, n=16):
    url = ("https://image.baidu.com/search/acjson?tn=resultjson_com&ipn=rj"
           "&ct=201326592&is=&fp=result&word=%s&pn=0&rn=%d&gsm=&ie=utf-8"
           % (urllib.parse.quote(keyword), n))
    raw = http_get(url, ref="https://image.baidu.com/")
    data = json.loads(raw.decode("utf-8", "replace"))
    items = []
    for it in (data.get("data") or []):
        if not it:
            continue
        u = it.get("middleURL") or it.get("thumbURL")
        if not u:
            continue
        items.append({
            "url": u,
            "title": it.get("fromPageTitleEnc") or "",
            "w": int(it.get("width") or 0),
            "h": int(it.get("height") or 0),
        })
    return items


def enlarge(url):
    """把百度缩略图参数放大，拿到更清晰的版本"""
    return re.sub(r"w=\d+", "w=1000", re.sub(r"&h=\d+", "", url))


def pick_item(items, keys):
    """优先选标题命中核心词的；命中不了则退回第一条（低置信）"""
    for it in items:
        title = it["title"]
        if any(k and k in title for k in keys):
            return it, True
    for it in items:
        # 放宽：只要分辨率够、不是极端竖长图
        if it["w"] >= 500 and it["h"] >= 300 and it["w"] / max(it["h"], 1) >= 0.6:
            return it, False
    return (items[0], False) if items else (None, False)


def download(url, dst):
    data = http_get(url, ref="https://image.baidu.com/")
    if len(data) < MIN_BYTES:
        return False
    if data[:2] != b"\xff\xd8" and data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    with open(dst, "wb") as f:
        f.write(data)
    return True


def detect_ext(data):
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return None


def collect_targets():
    """扫描知识库主攻略，返回 [{region, category, spots:[...]}]"""
    targets = []
    for cat in sorted(os.listdir(KB_DEST)):
        cat_dir = os.path.join(KB_DEST, cat)
        if not os.path.isdir(cat_dir):
            continue
        for fn in sorted(os.listdir(cat_dir)):
            if not fn.endswith("-主攻略.md"):
                continue
            path = os.path.join(cat_dir, fn)
            text = open(path, encoding="utf-8").read()
            m = re.search(r"^#\s*(.+?)\s*旅游攻略", text, re.M)
            region = m.group(1).strip() if m else fn.split("-")[0]
            sec = re.search(r"^##\s*热门景点\s*\n(.*?)(?=\n##\s|\Z)", text, re.S | re.M)
            spots = re.findall(r"^###\s*(.+?)\s*$", sec.group(1), re.M) if sec else []
            if not spots:
                log("  [警告] %s 未解析到景点" % fn)
                continue
            targets.append({"region": region, "category": cat, "spots": spots})
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="", help="只处理这些目的地（逗号分隔）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的图片")
    args = ap.parse_args()

    only = {s.strip() for s in args.regions.split(",") if s.strip()}
    targets = collect_targets()
    if only:
        targets = [t for t in targets if t["region"] in only]

    log("待处理目的地：%d 个" % len(targets))
    os.makedirs(OUT_ROOT, exist_ok=True)

    index = {"source": "百度图片搜索（版权归原作者，仅限学习与内部演示）",
             "destinations": []}
    ok_count = fail_count = skip_count = 0

    for t in targets:
        region, cat = t["region"], t["category"]
        region_dir = os.path.join(OUT_ROOT, cat, region)
        os.makedirs(region_dir, exist_ok=True)
        entry = {"region": region, "category": cat, "images": []}
        log("\n=== %s（%s）===" % (region, cat))

        for i, raw_spot in enumerate(t["spots"][:SPOTS_PER_REGION], 1):
            spot = clean_spot(raw_spot)
            core, keys = core_key(spot)
            base = "%02d-%s" % (i, re.sub(r'[\\/:*?"<>|]', "_", spot))

            # 已存在则跳过
            existing = None
            for ext in (".jpg", ".png"):
                p = os.path.join(region_dir, base + ext)
                if os.path.exists(p):
                    existing = p
                    break
            if existing and not args.force:
                log("  [跳过] %s（已存在）" % base)
                entry["images"].append({
                    "spot": spot, "file": os.path.relpath(existing, OUT_ROOT).replace("\\", "/"),
                    "is_cover": i == 1, "confidence": "cached",
                })
                skip_count += 1
                continue

            got = False
            # 第一轮：景点名 + 行政区名；第二轮：只搜景点名
            for kw in ("%s %s" % (spot, region), spot):
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
                    dst = os.path.join(region_dir, base + ext)
                    with open(dst, "wb") as f:
                        f.write(data)
                    entry["images"].append({
                        "spot": spot,
                        "raw_title": raw_spot,
                        "file": os.path.relpath(dst, OUT_ROOT).replace("\\", "/"),
                        "source_url": item["url"],
                        "hit_title": item["title"][:80],
                        "is_cover": i == 1,
                        "confidence": "high" if conf else "low",
                    })
                    log("  [OK] %-28s %.0fKB  %s  %s"
                        % (spot, len(data) / 1024, "高" if conf else "低",
                           item["title"][:38]))
                    ok_count += 1
                    got = True
                    break
                except Exception as exc:
                    log("  [重试] %s : %s" % (kw, str(exc)[:60]))
                time.sleep(random.uniform(0.6, 1.2))

            if not got:
                log("  [失败] %s" % spot)
                entry["images"].append({"spot": spot, "file": None, "is_cover": i == 1})
                fail_count += 1
            time.sleep(random.uniform(0.8, 1.5))

        index["destinations"].append(entry)

    # 保留未处理目的地的既有记录
    if only and os.path.exists(INDEX_PATH):
        old = json.load(open(INDEX_PATH, encoding="utf-8"))
        done = {e["region"] for e in index["destinations"]}
        for e in old.get("destinations", []):
            if e["region"] not in done:
                index["destinations"].append(e)
    index["destinations"].sort(key=lambda e: (e["category"], e["region"]))

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    log("\n" + "=" * 56)
    log("成功 %d ｜ 失败 %d ｜ 跳过 %d" % (ok_count, fail_count, skip_count))
    log("索引文件：%s" % INDEX_PATH)
    log("=" * 56)


if __name__ == "__main__":
    main()
