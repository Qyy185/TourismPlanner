"""
云端托管入口（用于发布成常驻的公网链接）

和 app_gradio.py 的本机 / 局域网 / 隧道启动方式的区别：
- 端口从平台注入的 PORT 环境变量读取，并监听 0.0.0.0；
- 不对内开启 Gradio 自带 share（对外暴露由托管平台负责）；
- 保留账号密码，避免公开链接被随意刷、白耗模型额度；
- 用 PUBLIC_URL 显式覆盖 Gradio 的 root_path（关键，见下）。

【为什么必须显式指定 root_path】
托管平台的网关转发时会改写 Host 头，Gradio 据此推断出的公开地址（config 里的 "root"）
会变成沙箱内网域名，例如 https://7860-xxxx.e2b.bj7.sandbox.cloudstudio.club。
后果：登录页前端把 POST /login 发到那个内网域名，登录 cookie 也就落在那个域下，
用户在真实公网域名上刷新后仍是登录页 —— 表现正是「输完账号密码点了没反应、跳不进去」。
生成方案时的 predict 请求同样会打错地址。
解法：launch(root_path=完整公网 URL)，Gradio 会原样使用它作为 root。

本地也可以直接跑（默认 7860，和 app_gradio.py 一致）：
    python server.py
"""
import os
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
# 向量库、知识库都按相对路径访问，必须先把工作目录钉在项目根
os.chdir(ROOT)

import gradio as gr

import app_gradio as app

PORT = int(os.environ.get("PORT") or os.environ.get("TRAVEL_PORT") or 7860)
USERNAME = os.environ.get("TRAVEL_USER") or "travel"
PASSWORD = os.environ.get("TRAVEL_PWD") or "travel2026"
# 对外公开地址：显式告诉 Gradio 前端该往哪里发请求（见文件头说明）。
# 换域名/换端口时只需改这个环境变量，或直接改下面的默认值。
PUBLIC_URL = (os.environ.get("TRAVEL_PUBLIC_URL")
              or "https://smart-travel-planner.app.workbuddy.host").rstrip("/")


def main():
    print("=" * 62)
    print("  智慧旅游规划助手  云端托管模式")
    print("=" * 62)
    print("  端口      : %d" % PORT)
    print("  公开地址  : %s" % PUBLIC_URL)
    print("  访问账号  : %s" % USERNAME)
    print("  API Key   : %s" % ("已读取" if os.environ.get("DASHSCOPE_API_KEY") else "未读取（检查 .env）"))
    print("=" * 62)

    # 后台预热向量库，避免第一位访客多等
    threading.Thread(target=app.warmup, daemon=True).start()

    app.demo.queue().launch(
        server_name="0.0.0.0",
        server_port=PORT,
        auth=(USERNAME, PASSWORD),
        theme=gr.themes.Soft(),
        css=app.MOBILE_CSS,
        allowed_paths=[app._ASSETS_DIR],
        root_path=PUBLIC_URL,
    )


if __name__ == "__main__":
    main()
