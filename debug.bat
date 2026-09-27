@echo off
chcp 65001 >nul
title 曜核 - AI 资产与协作工作台
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)

echo ============================================
echo   曜核 · AI 资产与协作工作台
echo   数据目录: %~dp0data
echo ============================================
%PY% server.py serve --open
pause
