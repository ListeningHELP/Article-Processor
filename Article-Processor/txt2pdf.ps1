# txt2pdf.ps1 - 用 Word COM 把 UTF-8(BOM) 的 txt 转成 PDF
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File txt2pdf.ps1 <input.txt> <output.pdf>
param(
    [string]$txtPath,
    [string]$pdfPath
)

$word = $null
$doc = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    # 打开 txt（UTF-8 BOM 会被 Word 自动识别），只读
    $doc = $word.Documents.Open($txtPath, $false, $true)
    # 17 = wdFormatPDF
    $doc.SaveAs([ref]$pdfPath, [ref]17)
    $doc.Close($false)
    $doc = $null
} catch {
    Write-Error $_.Exception.Message
    exit 1
} finally {
    if ($doc -ne $null) { try { $doc.Close($false) } catch {} }
    if ($word -ne $null) {
        try { $word.Quit() } catch {}
        try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null } catch {}
    }
}
exit 0
