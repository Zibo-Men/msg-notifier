# Windows built-in OCR (Windows.Media.Ocr) for one image file.
# Must run under Windows PowerShell 5.1 (powershell.exe), not PowerShell 7.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -ListLanguages
#   powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -Path C:\x.png [-Lang zh-Hans-CN]
# Without -Lang, every installed OCR language is tried; each block starts with "## <lang>".
param(
    [string]$Path = "",
    [string]$Lang = "",
    [switch]$ListLanguages
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, [Type]$type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$langs = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
if ($ListLanguages) {
    $langs | ForEach-Object { $_.LanguageTag }
    exit 0
}
if (-not $Path) { throw "missing -Path" }

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $Path).Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$tags = if ($Lang) { @($Lang) } else { $langs | ForEach-Object { $_.LanguageTag } }
foreach ($tag in $tags) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($tag))
    if ($null -eq $engine -and $Lang) {
        # requested language not installed: fall back to the user profile languages
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        $tag = "$tag (not installed, using profile language)"
    }
    if ($null -eq $engine) { Write-Output "## $tag (not installed)"; continue }
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    Write-Output "## $tag"
    $result.Lines | ForEach-Object { $_.Text }
}
$stream.Dispose()
