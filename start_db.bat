@echo off
REM Starts the private WheelSync PostgreSQL server (port 5434). Run once after each reboot.
"C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe" -D "C:\Users\USER\wheelsync-pgdata" -o "-p 5434" -l "C:\Users\USER\wheelsync-pgdata\server.log" -w start
