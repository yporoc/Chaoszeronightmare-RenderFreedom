@echo off
setlocal enabledelayedexpansion
rem Build script for the skin render tool: produces dist\skinrender.exe (single file, no console).
rem Steps are all hard gates: any failure stops before a shipping artifact exists.
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [x] python not found in PATH
  exit /b 2
)
where node >nul 2>&1
if errorlevel 1 (
  echo [x] node not found in PATH. The sandbox assertion is a shipping gate, not optional.
  exit /b 2
)

echo [1/6] dependencies
python -m pip install --disable-pip-version-check -q -r requirements.txt
python -m pip install --disable-pip-version-check -q "pyinstaller>=6.0"
if errorlevel 1 (
  echo [x] dependency install failed
  exit /b 1
)

echo [2/6] cleanliness scan over every shipped byte
python scan.py --out ..\skinrender-scan.txt
if errorlevel 1 (
  echo [x] scan hit private or hard-coded content, see ..\skinrender-scan.txt
  exit /b 1
)

echo [3/6] sandbox assertions
node dryrun.js
if errorlevel 1 (
  echo [x] sandbox assertions failed, nothing will be produced
  exit /b 1
)

echo [4/6] pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name skinrender ^
  --paths . --specpath build --collect-all customtkinter --hidden-import pefile ^
  --add-data "%~dp0payload.js;." --add-data "%~dp0table.json;." --add-data "%~dp0dryrun.js;." ^
  --exclude-module test --exclude-module unittest --exclude-module setuptools ^
  --exclude-module pydoc_data --log-level WARN app.py
if errorlevel 1 (
  echo [x] build failed
  exit /b 1
)
if not exist dist\skinrender.exe (
  echo [x] dist\skinrender.exe is missing
  exit /b 1
)

echo [5/6] frozen self check
dist\skinrender.exe --selfcheck
if errorlevel 1 (
  echo [x] selfcheck failed
  exit /b 1
)
dist\skinrender.exe --gate
if errorlevel 1 (
  echo [x] the shipping gates did not hold inside the frozen build
  exit /b 1
)

echo [6/6] artifact
for %%F in (dist\skinrender.exe) do echo file=%%~nxF size=%%~zF
certutil -hashfile dist\skinrender.exe SHA256 | findstr /v /i "hash SHA256"
echo usage: dist\skinrender.exe
echo        dist\skinrender.exe --analyze ^<file.exe^> [--manifest ^<resource-list^>]
echo        dist\skinrender.exe --build ^<file.exe^> [output-file]
echo        dist\skinrender.exe --gate
echo done: dist\skinrender.exe
