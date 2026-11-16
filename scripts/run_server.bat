@echo off
rem TravelPlan sunucusunu bu pencerede calistirir (Ctrl+C ile durdurulur).
rem Script nereden calistirilirsa calistirilsin repo kokune gecer.
setlocal
cd /d "%~dp0.."

if not exist server_v2.py (
  echo server_v2.py bulunamadi: %CD%
  pause
  exit /b 1
)

echo TravelPlan sunucusu baslatiliyor (varsayilan adres: http://127.0.0.1:8085)
python server_v2.py
if errorlevel 1 (
  echo Sunucu hata ile kapandi.
  pause
)
