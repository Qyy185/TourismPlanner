# -*- coding: utf-8 -*-
"""
图册总览页生成脚本
==================
把 assets/images/index.json 渲染成一张静态总览页（兴趣图册总览.html），
方便逐区核对图片与兴趣标签，也便于答辩时展示图库规模。

用法：
    python tools/build_gallery_overview.py
"""
import html
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "assets", "images", "index.json")
OUT = os.path.join(ROOT, "兴趣图册总览.html")

STYLE = """
:root{--bg:#f6f7fb;--card:#fff;--line:#e3e6ef;--ink:#1f2430;--sub:#6b7280;--acc:#4b5bd6}
*{box-sizing:border-box}
body{margin:0;padding:28px 22px 60px;background:var(--bg);color:var(--ink);
     font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif}
h1{margin:0 0 6px;font-size:24px}
p.lead{margin:0 0 18px;color:var(--sub)}
.sum{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 24px}
.sum span{background:#eef0fb;color:var(--acc);border:1px solid #dfe3f8;
          border-radius:999px;padding:3px 12px;font-size:13px}
nav{margin:0 0 20px;color:var(--sub);font-size:13px;line-height:2}
nav a{color:var(--acc);text-decoration:none;margin-right:10px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:14px 16px 6px;margin:0 0 16px}
h3{margin:0 0 10px;font-size:16px}
h3 em{color:var(--sub);font-style:normal;font-size:12px;font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
figure{margin:0 0 12px}
img{width:100%;height:130px;object-fit:cover;border-radius:8px;background:#eef1f6;display:block}
figcaption{font-size:12px;padding-top:5px;display:flex;flex-direction:column}
figcaption b{font-weight:600}
figcaption span{color:var(--acc);font-size:11px}
"""


def main():
    data = json.load(open(INDEX, encoding="utf-8"))
    interests = data.get("interests") or []
    dests = data["destinations"]
    total = sum(len(e["images"]) for e in dests)

    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        "<title>兴趣图册总览 · 智慧旅游规划助手</title>",
        "<style>" + STYLE + "</style></head><body>",
        "<h1>兴趣图册总览</h1>",
        '<p class="lead">34 个省级行政区 × 6 类兴趣偏好，共 ' + str(total) +
        " 张实景图。图片来自网络，版权归原作者所有，仅作学习与演示用途。</p>",
    ]

    # 各兴趣图片数
    chips = []
    for it in interests:
        n = sum(1 for e in dests for im in e["images"] if it in (im.get("interests") or []))
        chips.append("<span>%s %d 张</span>" % (it, n))
    parts.append('<div class="sum">' + "".join(chips) + "</div>")

    # 目录导航
    parts.append("<nav>" + "".join(
        '<a href="#r-%s">%s</a>' % (e["region"], e["region"]) for e in dests) + "</nav>")

    for e in dests:
        cards = []
        for im in e["images"]:
            tags = "、".join(im.get("interests") or [])
            src = "assets/images/" + im["file"]
            cards.append(
                '<figure><img src="%s" loading="lazy" alt="%s">'
                "<figcaption><b>%s</b><span>%s</span></figcaption></figure>"
                % (html.escape(src), html.escape(im["spot"]),
                   html.escape(im["spot"]), html.escape(tags)))
        parts.append('<section id="r-%s"><h3>%s <em>%d 张</em></h3><div class="grid">%s</div></section>'
                     % (html.escape(e["region"]), html.escape(e["region"]),
                        len(e["images"]), "".join(cards)))

    parts.append("</body></html>")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    print("已生成：%s（%d 张图）" % (OUT, total))


if __name__ == "__main__":
    main()
