@echo off
chcp 65001 >nul
title 抖音带货直播剪辑任务填表工具
cd /d "D:\Projects\dy-live-ecommerce-editor"
echo 正在启动剪辑任务填表工具 (浏览器将自动打开交互表单)...
"C:\Users\ePanda\.workbuddy\binaries\python\envs\liveclip\Scripts\python.exe" "src\tools\spec_creator.py"
pause
