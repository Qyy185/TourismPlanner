# -*- coding: utf-8 -*-
"""
一键配图海报：把行程摘要渲染成一张可分享的长图（PNG）

为什么要自己画而不是截图：
    浏览器截图需要 headless 浏览器（云端沙箱不一定有），而 Pillow 是纯 Python
    图像库、随依赖装进沙箱即可用，产出图片尺寸/像素可控，也不依赖前端渲染完成。

字体策略（跨平台）：
    Windows 常用 微软雅黑 / 黑体；Linux（云端沙箱）通常只有 Noto/DejaVu。
    这里按候选列表逐个尝试，全找不到时退回 Pillow 内置位图字体
    —— 内置字体不支持中文，会画成方块，所以能加载到 TTF 才是关键，
    找不到就明确报错让上层降级，而不是产出一张乱码图。
"""

import os
import re
import time

from core.render import parse_days, _extract_title, _extract_budget, _strip_md

# ---------------------------------------------------------------------------
# 画布与配色（与页面的青绿主色保持一致）
# ---------------------------------------------------------------------------
W = 900
PAD = 56
BG = (247, 250, 249)
INK = (31, 41, 55)
MUTED = (100, 116, 139)
BRAND = (13, 148, 136)
BRAND_D = (15, 118, 110)
CARD = (255, 255, 255)
LINE = (226, 232, 240)
TAG_BUF_BG = (254, 249, 195)
TAG_BUF_FG = (133, 100, 4)
TAG_BAK_BG = (219, 234, 254)
TAG_BAK_FG = (30, 64, 175)

_FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    # Linux（云端沙箱常见路径）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
]
_font_cache = {}
_font_path_resolved = None


def _find_font_path():
    global _font_path_resolved
    if _font_path_resolved is None:
        for p in _FONT_CANDIDATES:
            if os.path.exists(p):
                _font_path_resolved = p
                break
        else:
            _font_path_resolved = ""
    return _font_path_resolved


# 中文字体（微软雅黑/Noto CJK）大多没有彩色 emoji 字形，直接画会出「方块豆腐」。
# 海报是静态图，emoji 的信息量靠前面的文字就能表达，统一剥离最稳妥。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0000FE0F\U0000200D\U00002700-\U000027BF]"
)


def _no_emoji(s):
    s = _EMOJI_RE.sub("", str(s or ""))
    return re.sub(r"\s{2,}", " ", s).strip()


def _font(size):
    from PIL import ImageFont
    if size in _font_cache:
        return _font_cache[size]
    path = _find_font_path()
    f = None
    if path:
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            f = None
    if f is None:
        f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def poster_supported():
    """有可用中文字体才算支持（否则会画成方块）"""
    return bool(_find_font_path())


def _wrap(draw, text, font, max_w):
    """按像素宽度折行（中文无空格，逐字累加；英文按空格断）"""
    text = _no_emoji(_strip_md(text))
    if not text:
        return []
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        test = cur + ch
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill,
                           outline=outline, width=width)


def _qr_image(url, box):
    """把链接生成二维码位图（segno 纯 Python、零依赖，沙箱里最好装）

    失败一律返回 None，由调用方降级成「只印链接文字」，绝不因为二维码
    而让整张海报生成失败。
    """
    try:
        import io

        import segno
        from PIL import Image

        qr = segno.make(str(url), error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=12, border=1)
        buf.seek(0)
        return Image.open(buf).convert("RGB").resize((box, box), Image.NEAREST)
    except Exception:
        return None


def build_poster(md, destination="", days=0, people=0, budget="", origin="",
                 out_dir=None, share_url="", qr_payload=""):
    """生成海报 PNG，返回文件路径；失败抛异常由上层捕获

    qr_payload：要编进二维码的链接（一般是分享短码的绝对地址）；
                给了就在页脚上方画一块「扫码看行程」卡片。
    """
    from PIL import Image, ImageDraw

    if not poster_supported():
        raise RuntimeError("未找到可用的中文字体，无法生成海报")

    md = str(md or "")
    title = _extract_title(md) or ("%s 旅游方案" % destination if destination else "旅游方案")
    budget_line = _extract_budget(md)
    day_list = parse_days(md)

    # ---- 先量一遍高度，再画（Pillow 没有自动布局） ----
    probe = Image.new("RGB", (W, 10))
    pd = ImageDraw.Draw(probe)
    f_title = _font(40)
    f_sub = _font(22)
    f_day = _font(26)
    f_stop = _font(21)
    f_tag = _font(18)
    f_foot = _font(17)

    inner_w = W - PAD * 2
    y = PAD
    y += 92                                  # 品牌头
    y += 20

    title_lines = _wrap(pd, title, f_title, inner_w)
    y += len(title_lines) * 52 + 16

    meta_parts = []
    if destination:
        meta_parts.append("目的地 %s" % destination)
    if days:
        meta_parts.append("行程 %s 天" % int(days))
    if people:
        meta_parts.append("%s 人同行" % int(people))
    if origin:
        meta_parts.append("出发地 %s" % origin)
    meta_lines = _wrap(pd, "　".join(meta_parts), f_sub, inner_w) if meta_parts else []
    y += len(meta_lines) * 32 + 12

    if budget_line:
        blines = _wrap(pd, budget_line, f_sub, inner_w - 32)
        y += len(blines) * 32 + 36
    else:
        y += 12

    day_blocks = []
    for d in day_list:
        stops = [s for s in d["stops"] if s]
        stop_lines = []
        for s in stops:
            stop_lines.extend(_wrap(pd, s, f_stop, inner_w - 96))
        tags = []
        if d["buffer"]:
            tags.append(("buf", "机动 %s" % d["buffer"]))
        if d["backup"]:
            tags.append(("bak", "备选 %s" % d["backup"]))
        tag_lines = [(k, _wrap(pd, t, f_tag, inner_w - 96)) for k, t in tags]
        n_lines = max(len(stop_lines), 1) + sum(len(l) for _, l in tag_lines)
        h = 56 + n_lines * 34 + 20
        day_blocks.append((d, stop_lines, tag_lines, h))
        y += h + 16

    qr_box = 168
    if qr_payload:
        y += qr_box + 44
    y += 46                                   # 页脚
    H = y + PAD

    # ---- 正式绘制 ----
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)

    dr.rectangle([0, 0, W, 8], fill=BRAND)

    cy = PAD
    dr.text((PAD, cy + 6), "智慧旅游规划助手 · AI 旅行规划", font=_font(30), fill=BRAND_D)
    cy += 46
    dr.text((PAD, cy + 6), "AI 检索本地知识库生成的专属行程", font=_font(18), fill=MUTED)
    cy += 46

    for ln in title_lines:
        dr.text((PAD, cy), ln, font=f_title, fill=INK)
        cy += 52
    cy += 10

    for ln in meta_lines:
        dr.text((PAD, cy), ln, font=f_sub, fill=MUTED)
        cy += 32
    cy += 10

    if budget_line:
        blines = _wrap(pd, budget_line, f_sub, inner_w - 32)
        bh = len(blines) * 32 + 24
        _rounded(dr, [PAD, cy, W - PAD, cy + bh], 12, (236, 253, 245), BRAND, 1)
        ty = cy + 12
        for ln in blines:
            dr.text((PAD + 16, ty), ln, font=f_sub, fill=BRAND_D)
            ty += 32
        cy += bh + 16

    for d, stop_lines, tag_lines, h in day_blocks:
        _rounded(dr, [PAD, cy, W - PAD, cy + h], 14, CARD, LINE, 1)
        # 左侧品牌竖条
        dr.rounded_rectangle([PAD, cy + 16, PAD + 6, cy + h - 16], radius=3, fill=BRAND)
        dr.text((PAD + 24, cy + 14), d["label"], font=f_day, fill=BRAND_D)

        ty = cy + 54
        for ln in stop_lines:
            dr.text((PAD + 56, ty), ln, font=f_stop, fill=INK)
            ty += 34
        for kind, lines in tag_lines:
            if not lines:
                continue
            bg, fg = (TAG_BUF_BG, TAG_BUF_FG) if kind == "buf" else (TAG_BAK_BG, TAG_BAK_FG)
            for ln in lines:
                w = pd.textlength(ln, font=f_tag) + 22
                _rounded(dr, [PAD + 56, ty - 2, PAD + 56 + w, ty + 28], 8, bg)
                dr.text((PAD + 67, ty + 2), ln, font=f_tag, fill=fg)
                ty += 34
        cy += h + 16

    if not day_blocks:
        dr.text((PAD, cy), "（本方案未包含按天行程）", font=f_stop, fill=MUTED)
        cy += 40

    # 扫码卡片：左侧引导文字 + 右侧二维码（手机上扫码直接打开只读分享页）
    if qr_payload:
        card_h = qr_box + 44
        _rounded(dr, [PAD, cy + 4, W - PAD, cy + 4 + card_h], 14, CARD, LINE, 1)
        qim = _qr_image(qr_payload, qr_box)
        if qim is not None:
            img.paste(qim, (W - PAD - 22 - qr_box, cy + 26))
        tx = PAD + 26
        dr.text((tx, cy + 30), "扫码查看完整行程", font=_font(25), fill=BRAND_D)
        dr.text((tx, cy + 70), "手机相机对准二维码即可打开（只读页）",
                font=_font(18), fill=MUTED)
        dr.text((tx, cy + 102), "也可以手输链接：", font=_font(17), fill=MUTED)
        url_font = _font(16)
        for i, ln in enumerate(_wrap(pd, qr_payload, url_font,
                                     W - PAD * 2 - qr_box - 76)[:2]):
            dr.text((tx, cy + 128 + i * 24), ln, font=url_font, fill=BRAND)
        cy += card_h + 20

    dr.line([PAD, cy + 6, W - PAD, cy + 6], fill=LINE, width=1)
    foot = "生成时间 %s" % time.strftime("%Y-%m-%d %H:%M")
    if share_url:
        foot += "　|　%s" % share_url
    dr.text((PAD, cy + 20), foot, font=f_foot, fill=MUTED)

    out_dir = out_dir or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "posters")
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "", str(destination or "方案"))[:12] or "方案"
    path = os.path.join(out_dir, "海报-%s-%s.png" % (
        safe, time.strftime("%Y%m%d-%H%M%S")))
    img.save(path, "PNG", optimize=True)
    return path
