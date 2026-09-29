@echo off
setlocal
cd /d "%~dp0"
echo ===== Image Converter: build =====

python --version >nul 2>&1
if errorlevel 1 goto :nopython

python -m pip install --upgrade pip
python -m pip install pillow pillow-heif tkinterdnd2 pyinstaller
if errorlevel 1 goto :fail

python -m PyInstaller --noconfirm --clean --onefile --windowed --name ImageConverter --icon icon.ico --add-data "icon.ico;." --add-data "icon.png;." --collect-all tkinterdnd2 --collect-all pillow_heif image_converter.py
if errorlevel 1 goto :fail
echo.
echo Built the app: dist\ImageConverter.exe

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" goto :noinno

"%ISCC%" installer.iss
if errorlevel 1 goto :fail
echo.
echo DONE! Send this file to people: installer_output\ImageConverter-Setup.exe
goto :end

:noinno
echo.
echo Inno Setup is not installed, so the Setup file was not made.
echo Install it free from https://jrsoftware.org/isdl.php then run build.bat again.
goto :end

:nopython
echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during install, then run this again.
goto :end

:fail
echo.
echo Something went wrong - see the messages above.

:end
pause
