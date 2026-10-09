@echo off
cd /d "%~dp0"
echo 正在启动 Shopify Theme 管理控制台...
echo 浏览器访问: http://127.0.0.1:9290
python -m uvicorn server:app --host 127.0.0.1 --port 9290
pause
