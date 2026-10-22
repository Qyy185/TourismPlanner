@echo off
title 智慧旅游规划助手
cd /d "%~dp0"

echo ============================================
echo   智慧旅游规划助手 - 启动中
echo ============================================
echo.

if not exist ".env" (
    echo [!] 没找到 .env 文件
    echo     请先在项目根目录建立 .env，内容一行：
    echo     DASHSCOPE_API_KEY=你的密钥
    echo.
    pause
    exit /b 1
)

echo [1/2] 正在启动服务，请稍候...
echo.

start "" /min cmd /c "timeout /t 12 >nul & start http://127.0.0.1:7860"

python app_gradio.py

echo.
echo 服务已停止。
pause
