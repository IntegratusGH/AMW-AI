# Build pipeline: produces dist\AMW-AI-Setup-<VERSION>.msi
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 [-Push] [-Force] [-SeedDevEnv] [-Version 1.0.1]
# -Version X.Y[.Z] (optional): build this exact version instead of the
#   git-derived one (also skips the git-dependent version compute).
# -SeedDevEnv (DEV ONLY): after building, copy the workspace .env into
#   %PROGRAMDATA%\AMW-AI\.env so the installed app uses your dev DB credentials.
param([switch]$Push, [switch]$Force, [switch]$SeedDevEnv, [string]$Version = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root   = Split-Path -Parent $PSScriptRoot
$build  = Join-Path $root "build"
$dist   = Join-Path $root "dist"
$tools  = Join-Path $root "tools"
$cache  = Join-Path $tools "cache"
$py     = Join-Path $root "venv\Scripts\python.exe"

# (i) ensure git + remote origin (only needed when pushing a release)
if ($Push) {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
      winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
  }
  if (-not (Test-Path (Join-Path $root ".git"))) { git -C $root init | Out-Null }
  if (-not (git -C $root remote)) {
      git -C $root remote add origin https://github.com/IntegratusGH/AMW-AI-1.git
  }
}

# (ii) determine version
# NOTE: PowerShell variable names are case-insensitive; do NOT name the build
# number $BUILD or it collides with the $build directory path above.
if ($Version) {
  # explicit override: build this exact version, skip git-derived compute
  $vp = $Version.Split(".")
  $MAJOR=$vp[0]; $MINOR=$vp[1]
  $BuildNum = if ($vp.Count -ge 3) { $vp[2] } else { "0" }
  $APPVER=$Version; $MSIVER="$MAJOR.$MINOR.$BuildNum"
} else {
  $parts  = (& $py (Join-Path $root "scripts\compute_version.py")).Split(" ")
  $MAJOR=$parts[0]; $MINOR=$parts[1]; $BuildNum=$parts[2]
  $APPVER="$MAJOR.$MINOR"; $MSIVER="$MAJOR.$MINOR.$BuildNum"
}

# (iii) write version SSOT
Set-Content (Join-Path $root "app\version.py") "__version__ = `"$APPVER`"`n__build__ = $BuildNum`n"
Set-Content (Join-Path $root "VERSION") $APPVER

# (iv) build self-contained runtime (embeddable python + site-packages)
$rt = Join-Path $build "runtime"
Remove-Item $build -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $rt,$cache,$dist | Out-Null
$embZip = Join-Path $cache "python-3.14.6-embed-amd64.zip"
if (-not (Test-Path $embZip)) {
  Invoke-WebRequest -UseBasicParsing -OutFile $embZip `
    -Uri "https://www.python.org/ftp/python/3.14.6/python-3.14.6-embed-amd64.zip"
}
Expand-Archive $embZip -DestinationPath $rt -Force
# enable site-packages in the embeddable interpreter (cp314 => python314._pth)
# Read-modify-write in one shot (avoids reopening the freshly-extracted file,
# which can transiently be locked by AV/indexer and break Add-Content).
$pth = Join-Path $rt "python314._pth"
$pthLines = @(Get-Content $pth) -replace '^#\s*import site','import site'
$pthLines += "Lib\site-packages"
Set-Content -Path $pth -Value $pthLines -Encoding ASCII
# install deps with the BUILD python using --target (pip can't run in embeddable)
& $py -m pip install --upgrade pip
& $py -m pip install --target (Join-Path $rt "Lib\site-packages") -r (Join-Path $root "requirements.lock")
# pywin32 gotcha: copy DLLs so the service can load them without PATH edits
$sp = Join-Path $rt "Lib\site-packages"
Copy-Item (Join-Path $sp "pywin32_system32\*.dll") $rt -Force -ErrorAction SilentlyContinue
& $py (Join-Path $sp "win32\scripts\pywin32_postinstall.py") -install -destination $sp 2>$null

# (v) stage payload
Copy-Item (Join-Path $root "app")     (Join-Path $build "app")     -Recurse
Copy-Item (Join-Path $root "service") (Join-Path $build "service") -Recurse
Copy-Item (Join-Path $root "scripts") (Join-Path $build "scripts") -Recurse
Copy-Item (Join-Path $root "migrations") (Join-Path $build "migrations") -Recurse
Copy-Item (Join-Path $root "manage.py"),(Join-Path $root "wsgi.py"),`
          (Join-Path $root "VERSION"),(Join-Path $root ".env.example") $build
# purge caches/secrets from payload
Get-ChildItem $build -Recurse -Include __pycache__,*.pyc | Remove-Item -Recurse -Force
Remove-Item (Join-Path $build ".env") -ErrorAction SilentlyContinue

# (vi) ensure WiX + .NET 3.5
$wix = Join-Path $tools "wix"
if (-not (Test-Path (Join-Path $wix "candle.exe"))) {
  $wz = Join-Path $cache "wix314-binaries.zip"
  Invoke-WebRequest -UseBasicParsing -OutFile $wz `
    -Uri "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip"
  Expand-Archive $wz -DestinationPath $wix -Force
}
# WiX v3 tools target .NET 3.5.  Prefer enabling NetFx3 when we can elevate;
# otherwise fall back to config shims so the tools run on the installed .NET 4.x.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$netfx3 = $false
try { $netfx3 = ((Get-WindowsOptionalFeature -Online -FeatureName NetFx3 -ErrorAction Stop).State -eq "Enabled") } catch { }
if (-not $netfx3 -and $isAdmin) {
  Write-Warning "Enabling .NET 3.5 (NetFx3) - may need media/Internet"
  DISM /online /Enable-Feature /FeatureName:NetFx3 /All /NoRestart
  try { $netfx3 = ((Get-WindowsOptionalFeature -Online -FeatureName NetFx3 -ErrorAction Stop).State -eq "Enabled") } catch { }
}
if (-not $netfx3) {
  Write-Warning ".NET 3.5 unavailable; running WiX tools on .NET 4 via config shims."
  $shim = @'
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <startup useLegacyV2RuntimeActivationPolicy="true">
    <supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.0" />
    <supportedRuntime version="v2.0.50727" />
  </startup>
</configuration>
'@
  foreach ($exe in "candle.exe","light.exe","heat.exe") {
    Set-Content -Path (Join-Path $wix "$exe.config") -Value $shim -Encoding UTF8
  }
}

# (vii) harvest + compile + link
# heat.exe's MEF harvester discovery fails when the tools run on .NET 4 (no
# .NET 3.5 present / no elevation to add it), so generate the payload fragment
# ourselves.  Output mirrors `heat dir -cg PayloadGroup -dr INSTALLFOLDER -srd
# -var var.PayloadDir`: one file per component with auto GUIDs.
function Get-WixId([string]$prefix, [string]$rel) {
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($rel.ToLowerInvariant())
  $hex = -join ([System.Security.Cryptography.MD5]::Create().ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") })
  "$prefix$($hex.Substring(0,24))"
}
function ConvertTo-XmlAttr([string]$s) {
  $s.Replace('&','&amp;').Replace('<','&lt;').Replace('>','&gt;').Replace('"','&quot;').Replace("'",'&apos;')
}
$compIds = New-Object System.Collections.Generic.List[string]
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('<?xml version="1.0" encoding="utf-8"?>')
[void]$sb.AppendLine('<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">')
[void]$sb.AppendLine('  <Fragment>')
[void]$sb.AppendLine('    <DirectoryRef Id="INSTALLFOLDER">')
function Add-DirContents([string]$fsPath, [string]$rel, [int]$indent) {
  $pad = ' ' * $indent
  foreach ($f in (Get-ChildItem -LiteralPath $fsPath -File | Sort-Object Name)) {
    $frel = if ($rel) { "$rel\$($f.Name)" } else { $f.Name }
    $cid = Get-WixId "cmp" $frel; $fid = Get-WixId "fil" $frel
    $compIds.Add($cid)
    $src = ConvertTo-XmlAttr ("`$(var.PayloadDir)\" + $frel)
    $nm  = ConvertTo-XmlAttr $f.Name
    [void]$sb.AppendLine("$pad<Component Id=`"$cid`" Guid=`"*`">")
    [void]$sb.AppendLine("$pad  <File Id=`"$fid`" Name=`"$nm`" KeyPath=`"yes`" Source=`"$src`" />")
    [void]$sb.AppendLine("$pad</Component>")
  }
  foreach ($d in (Get-ChildItem -LiteralPath $fsPath -Directory | Sort-Object Name)) {
    $drel = if ($rel) { "$rel\$($d.Name)" } else { $d.Name }
    $did = Get-WixId "dir" $drel; $nm = ConvertTo-XmlAttr $d.Name
    [void]$sb.AppendLine("$pad<Directory Id=`"$did`" Name=`"$nm`">")
    Add-DirContents $d.FullName $drel ($indent+2)
    [void]$sb.AppendLine("$pad</Directory>")
  }
}
Add-DirContents $build "" 6
[void]$sb.AppendLine('    </DirectoryRef>')
[void]$sb.AppendLine('  </Fragment>')
[void]$sb.AppendLine('  <Fragment>')
[void]$sb.AppendLine('    <ComponentGroup Id="PayloadGroup">')
foreach ($c in $compIds) { [void]$sb.AppendLine("      <ComponentRef Id=`"$c`" />") }
[void]$sb.AppendLine('    </ComponentGroup>')
[void]$sb.AppendLine('  </Fragment>')
[void]$sb.AppendLine('</Wix>')
Set-Content -Path (Join-Path $root "installer\Files.wxs") -Value $sb.ToString() -Encoding UTF8

# NOTE: no -ext flags - WiX extensions can't load on the .NET 4 shim, so the
# authoring avoids them (ProgramData ACL is applied by installer_bootstrap.py).
& (Join-Path $wix "candle.exe") -arch x64 -dPayloadDir="$build" `
  -dProductVersion="$MSIVER" -dAppVersion="$APPVER" `
  (Join-Path $root "installer\Product.wxs") (Join-Path $root "installer\Files.wxs") `
  -o "$build\"
if ($LASTEXITCODE -ne 0) { throw "candle.exe failed (exit $LASTEXITCODE)" }
& (Join-Path $wix "light.exe") -sval `
  "$build\Product.wixobj" "$build\Files.wixobj" `
  -o (Join-Path $dist "AMW-AI-Setup-$APPVER.msi")
if ($LASTEXITCODE -ne 0) { throw "light.exe failed (exit $LASTEXITCODE)" }

# (viii) optional: commit version bump + tag + push
if ($Push) {
  git -C $root add app\version.py VERSION
  git -C $root commit -m "build: release v$APPVER (msi $MSIVER)"
  git -C $root tag -a "v$APPVER" -m "AMW-AI v$APPVER"
  git -C $root push origin HEAD --tags
}

# (ix) optional (DEV ONLY): seed ProgramData .env from the workspace .env so the
# installed app uses your dev DB credentials.  Off by default; never use in prod
# (the MSI itself never ships .env).  ProgramData .env persists across
# installs/upgrades, so this is a one-time sync you can re-run after edits.
if ($SeedDevEnv) {
  $devEnv = Join-Path $root ".env"
  if (Test-Path $devEnv) {
    $pdDir = Join-Path $env:ProgramData "AMW-AI"
    New-Item -ItemType Directory -Force -Path $pdDir | Out-Null
    Copy-Item -LiteralPath $devEnv -Destination (Join-Path $pdDir ".env") -Force
    Write-Host "Seeded $pdDir\.env from workspace .env (dev credentials)."
  } else {
    Write-Warning "-SeedDevEnv set but no .env found at $devEnv; skipped."
  }
}
Write-Host "Built dist\AMW-AI-Setup-$APPVER.msi (ProductVersion $MSIVER)"
