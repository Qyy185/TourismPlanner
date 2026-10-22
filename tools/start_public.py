"""
公网访问启动器

一键做两件事：
1. 用 cloudflared 建立一条临时公网隧道（Cloudflare 免费提供，无需注册）；
2. 启动「智慧旅游规划助手」，手机流量、异地电脑都能打开。

用法：
    python tools/start_public.py

可选环境变量：
    TRAVEL_PORT   本地端口，默认 7860
    TRAVEL_USER   公网访问账号，默认 travel
    TRAVEL_PWD    公网访问密码，默认 travel2026

注意事项：
- 公网链接由 Cloudflare 随机生成，形如 https://xxx.trycloudflare.com，
  每次启动都不一样，关掉本窗口立即失效。
- 该链接是公开的，任何人拿到都能使用（会消耗你的模型 API 额度），
  因此这里强制开启账号密码；想换密码就设 TRAVEL_PWD 再启动。
- 掉线或长时间不用后链接可能被回收，重启本脚本即可拿到新链接。
"""
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 控制台多为 GBK 代码页：固定按 gbk 输出并容错，避免个别字符导致崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="gbk", errors="replace")
    except (AttributeError, ValueError):
        pass

PORT = int(os.environ.get("TRAVEL_PORT") or 7860)
USERNAME = os.environ.get("TRAVEL_USER") or "travel"
PASSWORD = os.environ.get("TRAVEL_PWD") or "travel2026"
CLOUDFLARED = os.path.join(ROOT, "tools", "cloudflared.exe")
URL_FILE = os.path.join(ROOT, "公网地址.txt")
LOCAL_URL = "http://127.0.0.1:%d" % PORT


def open_tunnel():
    """启动隧道并抓取公网地址，返回 (进程, 地址或 None, 日志行列表)"""
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", LOCAL_URL, "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    found = []
    lines = []
    ready = threading.Event()

    def pump():
        for line in proc.stdout:
            lines.append(line.rstrip())
            match = re.search(r"https://[-\w.]+\.trycloudflare\.com", line)
            if match and not found:
                found.append(match.group(0))
                try:
                    with open(URL_FILE, "w", encoding="utf-8") as fh:
                        fh.write(match.group(0) + "\n")
                except OSError:
                    pass
                ready.set()
        ready.set()  # 进程意外退出时也要放行，避免主线程死等

    threading.Thread(target=pump, daemon=True).start()
    ready.wait(60)
    return proc, (found[0] if found else None), lines


def print_banner(url):
    line = "=" * 68
    print("")
    print(line)
    print("  公网地址已就绪，手机 / 异地电脑用浏览器打开：")
    print("")
    print("     %s" % url)
    print("")
    print("  访问账号：%s      密码：%s" % (USERNAME, PASSWORD))
    print("  该链接在本次启动期间有效，关掉本窗口即失效。")
    print(line)
    print("")


def main():
    if not os.path.exists(CLOUDFLARED):
        print("找不到 tools/cloudflared.exe，无法建立公网隧道。")
        print("请到 Cloudflare 官方发布页下载 cloudflared-windows-amd64.exe，")
        print("重命名为 cloudflared.exe 后放进项目的 tools 目录。")
        return 1

    print("正在建立公网隧道（首次约 5~20 秒，请稍候）……")
    proc, url, lines = open_tunnel()

    if url:
        print_banner(url)
    else:
        print("")
        print("公网隧道建立失败，最后几行日志如下：")
        for item in lines[-12:]:
            print("   " + item)
        print("")
        print("本机访问不受影响，可继续使用 %s" % LOCAL_URL)
        print("")

    import gradio as gr

    import app_gradio as app

    # 后台预热向量库，避免第一位访客（常常就是手机端）多等 1~2 秒
    threading.Thread(target=app.warmup, daemon=True).start()

    app.demo.queue().launch(
        server_name="0.0.0.0",
        server_port=PORT,
        auth=(USERNAME, PASSWORD),
        theme=gr.themes.Soft(),
        css=app.MOBILE_CSS,
        allowed_paths=[app._ASSETS_DIR],
        prevent_thread_lock=True,
    )

    print("")
    print("本机访问：%s" % LOCAL_URL)
    print("按 Ctrl+C 停止服务（隧道会同时关闭）。")
    print("")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭……")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
