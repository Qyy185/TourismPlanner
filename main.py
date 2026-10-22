# -*- coding: utf-8 -*-
"""
云端发布入口（薄封装）

为什么需要这个文件：
    发布沙箱默认会去找 `main.py` 作为启动入口。本项目的真正入口是
    `app_gradio.py`，直接发布时会报
    `can't open file '/workspace/main.py': No such file or directory`。
    这里只做一件事——把启动转发给 `app_gradio.py`，不改任何业务逻辑。

端口与监听地址：
    `app_gradio.py` 的 __main__ 里已按优先级取端口
    TRAVEL_PORT > PORT > 7860，并在检测到 PORT 时自动绑 0.0.0.0
    （云端反向代理要求）。所以这里无需再传参。

本机使用：直接跑 app_gradio.py 即可，这个文件不影响本地开发。
"""
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

os.chdir(_HERE)

# 以 __main__ 身份执行 app_gradio.py，触发其中的 if __name__ == "__main__" 分支
runpy.run_path(os.path.join(_HERE, "app_gradio.py"), run_name="__main__")
