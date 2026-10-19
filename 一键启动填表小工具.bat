@echo off
chcp 65001 >nul
title 抖音带货直播剪辑任务填表小工具
cd /d "%~dp0"
echo 正在启动剪辑任务填表小工具...
"C:\Users\ePanda\.workbuddy\binaries\python\envs\liveclip\Scripts\python.exe" "src\tools\spec_creator.py"
pause
