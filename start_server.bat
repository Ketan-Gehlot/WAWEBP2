@echo off
title Maxis Gold Jewellery — WhatsApp Automation Server
cd /d "%~dp0"
echo Starting Maxis Gold Jewellery WhatsApp Automation Platform at http://127.0.0.1:8000 ...
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
