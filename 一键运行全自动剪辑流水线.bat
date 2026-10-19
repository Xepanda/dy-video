@echo off
chcp 65001 >nul
title 抖音带货直播自动化剪辑流水线
cd /d "D:\Projects\dy-live-ecommerce-editor"
echo =======================================================
echo 正在运行抖音带货直播自动化剪辑端到端流水线...
echo 读取任务配置: job_spec.json
echo =======================================================
"C:\Users\ePanda\.workbuddy\binaries\python\envs\liveclip\Scripts\python.exe" "src\main_pipeline.py" --spec job_spec.json
pause
