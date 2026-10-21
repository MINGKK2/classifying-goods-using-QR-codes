@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_LAUNCHER=py"
) else (
    set "PY_LAUNCHER=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo [QR Scanner] Đang tạo môi trường Python...
    %PY_LAUNCHER% -m venv .venv
    if errorlevel 1 goto :error
)

echo [QR Scanner] Đang kiểm tra thư viện...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error

echo.
echo [QR Scanner] Nội dung QR sẽ xuất ở terminal này, mỗi mã một lần.
echo [QR Scanner] Đang mở giao diện...
echo.
".venv\Scripts\python.exe" qr_scanner.py
if errorlevel 1 goto :error

endlocal
exit /b 0

:error
echo.
echo Không thể khởi động QR Scanner. Kiểm tra Python và thông báo lỗi phía trên.
pause
endlocal
exit /b 1
