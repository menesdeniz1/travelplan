<#
.SYNOPSIS
  TravelPlan sunucusunu Windows oturumu açıldığında otomatik başlatır.

.DESCRIPTION
  -Install    Zamanlanmış görev "TravelPlanServer" kaydeder (oturum açılınca, mevcut kullanıcı, yönetici gerekmez).
  -Uninstall  Görevi siler. Çalışan sunucuyu kapatmaz.
  -Status     Görevin varlığını, son çalışma zamanını ve 8085 portunun durumunu gösterir.
  -WhatIf     -Install veya -Uninstall ile birlikte kullanılırsa yalnızca ne yapılacağını yazar; hiçbir şey değiştirmez.

  Sunucu pythonw.exe ile (pencere açmadan) çalışır; kendi günlüğünü logs\server.log dosyasına yazar
  (5 MB, 3 yedek; sunucu kendisi döndürür). Çıktı yönlendirmesi yapılmaz.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autostart_windows.ps1 -Status
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autostart_windows.ps1 -Install -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [string]$TaskName = "TravelPlanServer"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$serverScript = Join-Path $repoRoot "server_v2.py"
$logDir = Join-Path $repoRoot "logs"
$logFile = Join-Path $logDir "server.log"
$userId = "$env:USERDOMAIN\$env:USERNAME"
$serverPort = 8085
if ($env:TRAVELPLAN_PORT -match '^\d+$') { $serverPort = [int]$env:TRAVELPLAN_PORT }

function Find-Interpreter {
    # pythonw.exe runs without a console window; python.exe is only a fallback
    $pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonw) { return [pscustomobject]@{ Path = $pythonw.Source } }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) {
        Write-Host "UYARI: pythonw.exe bulunamadı, python.exe kullanılacak (görev çalışırken konsol penceresi açılabilir)."
        return [pscustomobject]@{ Path = $python.Source }
    }
    return $null
}

function Get-TravelPlanTask {
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Show-Status {
    $task = Get-TravelPlanTask
    if (-not $task) {
        Write-Host "Zamanlanmış görev '$TaskName' kayıtlı değil. Kurmak için: -Install"
    } else {
        $info = $task | Get-ScheduledTaskInfo
        Write-Host "Görev '$TaskName' kayıtlı. Durum: $($task.State)"
        Write-Host "Son çalışma: $($info.LastRunTime)   Son sonuç kodu: $($info.LastTaskResult)"
    }
    Write-Host "Log dosyası: $logFile"
    $listening = Get-NetTCPConnection -LocalPort $serverPort -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Host "Sunucu $serverPort portunda çalışıyor."
    } else {
        Write-Host "$serverPort portunda dinleyen bir sunucu yok."
    }
}

function Install-TravelPlanTask {
    if (-not (Test-Path -LiteralPath $serverScript)) {
        Write-Host "server_v2.py bulunamadı: $serverScript"
        exit 1
    }
    $interpreter = Find-Interpreter
    if (-not $interpreter) {
        Write-Host "Python bulunamadı. Python 3.12'nin kurulu ve PATH'te olduğundan emin olun."
        exit 1
    }
    if ($interpreter.Path -like "*\WindowsApps\*") {
        Write-Host "Python yolu Microsoft Store takma adı ($($interpreter.Path)). Gerçek Python kurulumunu PATH'e ekleyin."
        exit 1
    }

    # Run the interpreter directly, with no cmd.exe wrapper and no stdout/stderr redirect.
    # The server rotates logs\server.log itself (RotatingFileHandler). A redirect would keep one
    # ever-growing file open, and Windows will not rotate a file that another process holds open.
    $arguments = '"' + $serverScript + '"'
    $action = New-ScheduledTaskAction -Execute $interpreter.Path -Argument $arguments -WorkingDirectory $repoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew

    if ($WhatIfPreference) {
        Write-Host "WhatIf: aşağıdaki görev kaydedilecekti (hiçbir şey değiştirilmedi):"
        Write-Host "  Görev adı    : $TaskName"
        Write-Host "  Tetikleyici  : $userId oturum açınca"
        Write-Host "  Program      : $($interpreter.Path)"
        Write-Host "  Argümanlar   : $arguments"
        Write-Host "  Çalışma dizini: $repoRoot"
        Write-Host "  Log          : $logFile"
        return
    }

    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "TravelPlan sunucusu (server_v2.py) oturum açılınca başlar" -Force | Out-Null
    Write-Host "Görev '$TaskName' kaydedildi. Oturum açıldığında sunucu otomatik başlayacak."
    Write-Host "Şimdi başlatmak için: Start-ScheduledTask -TaskName $TaskName"
}

function Uninstall-TravelPlanTask {
    $task = Get-TravelPlanTask
    if (-not $task) {
        Write-Host "Görev '$TaskName' zaten kayıtlı değil."
        return
    }
    if ($WhatIfPreference) {
        Write-Host "WhatIf: '$TaskName' görevi silinecekti (hiçbir şey değiştirilmedi)."
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Görev '$TaskName' silindi. Çalışan bir sunucu varsa kapatılmadı; kapatmak için pencereyi kapatın."
}

$selected = @($Install, $Uninstall, $Status) | Where-Object { $_ }
if ($selected.Count -ne 1) {
    Write-Host "Kullanım: autostart_windows.ps1 -Install | -Uninstall | -Status  (-WhatIf ile yalnızca gösterir)"
    exit 1
}

if ($Status) { Show-Status }
elseif ($Install) { Install-TravelPlanTask }
elseif ($Uninstall) { Uninstall-TravelPlanTask }
