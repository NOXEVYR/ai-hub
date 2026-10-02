@echo off
chcp 65001 >nul
title 曜核 - AI 资产与协作工作台
cd /d "%~dp0"

echo ============================================
echo   曜核 · AI 资产与协作工作台
echo   数据目录: %~dp0data
echo ============================================
set "PYTHON_EXE="
for /f "delims=" %%P in ('"%SystemRoot%\System32\cscript.exe" //nologo "%~dp0start.vbs" --check') do set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE (
  echo 未找到可运行的 Python 3.9 或更新版本。
  echo PATH 中的候选位置仅供诊断：
  where python.exe 2>nul
  where python3.exe 2>nul
  where py.exe 2>nul
  pause
  exit /b 1
)
echo Python: "%PYTHON_EXE%"
"%PYTHON_EXE%" server.py serve --open
if errorlevel 1 echo 后台启动命令退出，代码 %errorlevel%。
pause
