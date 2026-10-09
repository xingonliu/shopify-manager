$targetVbs = "D:\code\shopify-manager\start_silent.vbs"
$startupDir = [System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")
$shortcutPath = Join-Path $startupDir "ShopifyThemeManager.lnk"

$wshShell = New-Object -ComObject WScript.Shell
$shortcut = $wshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "wscript.exe"
$shortcut.Arguments = "`"$targetVbs`""
$shortcut.WorkingDirectory = "D:\code\shopify-manager"
$shortcut.Description = "Shopify Theme Local Manager"
$shortcut.WindowStyle = 7
$shortcut.Save()

Write-Host "Startup shortcut created successfully: $shortcutPath" -ForegroundColor Green
