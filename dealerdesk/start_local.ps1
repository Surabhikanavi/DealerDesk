$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path (Split-Path -Parent $root) ".venv\Scripts\python.exe"
$code = @'
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dealerdesk.settings")
import django
django.setup()
from django.core.wsgi import get_wsgi_application
from wsgiref.simple_server import make_server
app = get_wsgi_application()
httpd = make_server("127.0.0.1", 8000, app)
print("DealerDesk WSGI ready on http://127.0.0.1:8000", flush=True)
httpd.serve_forever()
'@

Start-Process -FilePath $py -ArgumentList "-u", "-c", $code -WorkingDirectory $root -WindowStyle Hidden | Out-Null
Start-Sleep -Seconds 3

try {
    $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/health/" -TimeoutSec 10
    Write-Host "Health check: $($r.StatusCode)"
    Write-Host "Open http://127.0.0.1:8000/"
} catch {
    Write-Host "The app did not respond yet. Wait a few seconds and try http://127.0.0.1:8000/"
}
