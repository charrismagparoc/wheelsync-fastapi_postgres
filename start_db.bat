@echo off
REM Starts the WheelSync PostgreSQL server. Run once after each reboot, before the backend.
REM The database runs in its own minimized window ("WheelSync database"), so pressing Ctrl+C in this window
REM (e.g. to restart the backend) does not stop it. Closing that minimized window stops the database.
REM This PC: portable PostgreSQL in %USERPROFILE%\pgsql on port 5432.
REM Otherwise: PostgreSQL 17 from Program Files on port 5434 (the other development PC).
if exist "%USERPROFILE%\pgsql\bin\pg_ctl.exe" (
  set "PG_BIN=%USERPROFILE%\pgsql\bin"
  set "PG_PORT=5432"
) else (
  set "PG_BIN=C:\Program Files\PostgreSQL\17\bin"
  set "PG_PORT=5434"
)
set "PG_DATA=%USERPROFILE%\wheelsync-pgdata"

"%PG_BIN%\pg_isready.exe" -h localhost -p %PG_PORT% >nul 2>&1 && (echo PostgreSQL is already running on port %PG_PORT%. & goto :eof)

echo Starting PostgreSQL on port %PG_PORT% ...
start "WheelSync database" /min "%PG_BIN%\pg_ctl.exe" start -D "%PG_DATA%" -o "-p %PG_PORT%" -l "%PG_DATA%\server.log"

for /l %%i in (1,1,60) do (
  "%PG_BIN%\pg_isready.exe" -h localhost -p %PG_PORT% >nul 2>&1 && (echo PostgreSQL is running. & goto :eof)
  ping -n 2 127.0.0.1 >nul
)
echo PostgreSQL did not start within 60 seconds. See %PG_DATA%\server.log
exit /b 1
