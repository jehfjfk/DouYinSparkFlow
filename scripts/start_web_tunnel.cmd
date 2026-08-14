@echo off
powershell.exe -NoProfile -WindowStyle Hidden -Command "$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if (-not $listener) { Start-Process -FilePath 'C:\Users\yanghao\AppData\Local\Programs\Python\Python311\python.exe' -ArgumentList 'web_app.py','--host','127.0.0.1','--port','8765' -WorkingDirectory 'E:\E盘新建文件夹\DouYinSparkFlow' -WindowStyle Hidden; Start-Sleep -Seconds 2 }"
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --no-autoupdate --logfile "E:\E盘新建文件夹\DouYinSparkFlow\logs\cloudflared.log" --url http://127.0.0.1:8765
