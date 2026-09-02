#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$SummaryPath = "",
    [switch]$AskBeforePrint
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$ResultsRoot = Join-Path $Root "results"
$SettingsPath = Join-Path $Root "config\test-settings.json"
$LocalSettingsPath = Join-Path $Root "config\test-settings.local.json"
$GeneratorPath = Join-Path $Root "tools\label_generator.py"

Set-Location $Root

# Manual label printing remains independent from label.mode. label.mode only
# controls whether the normal board-test workflow starts this script.
$DefaultAskBeforePrint = $false

function ConvertTo-Hashtable {
    param($Value)

    if ($null -eq $Value) {
        return $null
    }

    if ($Value -is [System.Collections.IDictionary]) {
        $Result = @{}
        foreach ($Key in $Value.Keys) {
            $Result[[string]$Key] = ConvertTo-Hashtable -Value $Value[$Key]
        }
        return $Result
    }

    if ($Value -is [PSCustomObject]) {
        $Result = @{}
        foreach ($Property in $Value.PSObject.Properties) {
            $Result[$Property.Name] = ConvertTo-Hashtable -Value $Property.Value
        }
        return $Result
    }

    if (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
        $Items = @()
        foreach ($Item in $Value) {
            $Items += ,(ConvertTo-Hashtable -Value $Item)
        }
        return $Items
    }

    return $Value
}

function Read-JsonHashtable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Settings file is missing: $Path"
    }

    try {
        $Object = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Invalid JSON in $Path`: $($_.Exception.Message)"
    }

    $Table = ConvertTo-Hashtable -Value $Object
    if (-not ($Table -is [System.Collections.IDictionary])) {
        throw "Settings file must contain a JSON object: $Path"
    }

    return $Table
}

function Merge-Hashtables {
    param(
        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$Base,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$Override
    )

    $Result = @{}

    foreach ($Key in $Base.Keys) {
        $Result[[string]$Key] = $Base[$Key]
    }

    foreach ($Key in $Override.Keys) {
        $Name = [string]$Key
        $OverrideValue = $Override[$Key]

        if (
            $Result.ContainsKey($Name) -and
            ($Result[$Name] -is [System.Collections.IDictionary]) -and
            ($OverrideValue -is [System.Collections.IDictionary])
        ) {
            $Result[$Name] = Merge-Hashtables -Base $Result[$Name] -Override $OverrideValue
        }
        else {
            $Result[$Name] = $OverrideValue
        }
    }

    return $Result
}

function Get-EffectiveSettings {
    $Settings = Read-JsonHashtable -Path $SettingsPath

    if (Test-Path -LiteralPath $LocalSettingsPath) {
        $Local = Read-JsonHashtable -Path $LocalSettingsPath
        $Settings = Merge-Hashtables -Base $Settings -Override $Local
    }

    return $Settings
}

function Resolve-ConfiguredPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ([IO.Path]::IsPathRooted($Value)) {
        return $Value
    }

    return Join-Path $Root $Value
}

function Convert-BoardId {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $Text = $Value.Trim().ToUpperInvariant()

    if ($Text -match '^(\d{3})-(E32|C3|S3)$') {
        $NumberText = $Matches[1]
        $Type = $Matches[2]
    }
    elseif ($Text -match '^(E32|C3|S3)-(\d{3})$') {
        $Type = $Matches[1]
        $NumberText = $Matches[2]
    }
    else {
        return $null
    }

    $Number = [int]$NumberText
    if ($Number -lt 1 -or $Number -gt 999) {
        return $null
    }

    return ("{0:D3}-{1}" -f $Number, $Type)
}

function Read-Summary {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    try {
        $Summary = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        $BoardId = Convert-BoardId -Value ([string]$Summary.board_id)
        $Result = ([string]$Summary.result).Trim().ToUpperInvariant()
        $TimestampRaw = ([string]$Summary.timestamp).Trim()

        if ([string]::IsNullOrWhiteSpace($BoardId)) {
            return $null
        }

        if ($Result -notin @("PASS", "FAIL", "UNRATED")) {
            return $null
        }

        $Timestamp = [DateTime]::Parse($TimestampRaw)

        return [PSCustomObject]@{
            SummaryPath  = $Path
            Directory    = Split-Path -Parent $Path
            BoardId      = $BoardId
            Result       = $Result
            Timestamp    = $Timestamp
            TimestampRaw = $TimestampRaw
        }
    }
    catch {
        return $null
    }
}

function Get-Summaries {
    $Entries = @()

    if (-not (Test-Path -LiteralPath $ResultsRoot)) {
        return $Entries
    }

    Get-ChildItem -LiteralPath $ResultsRoot -Filter "summary.json" -File -Recurse | ForEach-Object {
        $Entry = Read-Summary -Path $_.FullName

        if ($null -ne $Entry) {
            $Entries += $Entry
        }
    }

    return @($Entries | Sort-Object Timestamp -Descending)
}

function Test-PythonVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [string[]]$Arguments = @()
    )

    try {
        & $Command @Arguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-Python {
    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

    if ((Test-Path -LiteralPath $VenvPython) -and (Test-PythonVersion -Command $VenvPython)) {
        return @{
            Command = $VenvPython
            Args = @()
        }
    }

    foreach ($Candidate in @(
        @{ Command = "py"; Args = @("-3") },
        @{ Command = "python"; Args = @() },
        @{ Command = "python3"; Args = @() }
    )) {
        if (Test-PythonVersion -Command $Candidate.Command -Arguments @($Candidate.Args)) {
            return $Candidate
        }
    }

    throw "Python 3.10 or newer was not found."
}

function Get-LabelConfiguration {
    $Settings = Get-EffectiveSettings
    $Label = @{}

    if (
        $Settings.ContainsKey("label") -and
        ($Settings["label"] -is [System.Collections.IDictionary])
    ) {
        $Label = $Settings["label"]
    }

    $Backend = if ($Label.ContainsKey("backend")) {
        ([string]$Label["backend"]).Trim().ToLowerInvariant()
    }
    else {
        # Backward compatibility for existing installations that predate the
        # generic Windows printer backend.
        "brother-bpac"
    }

    if ($Backend -notin @("windows", "brother-bpac")) {
        throw "Unknown label backend '$Backend'. Allowed values: windows, brother-bpac."
    }

    $PrinterName = if ($Label.ContainsKey("printer_name")) {
        ([string]$Label["printer_name"]).Trim()
    }
    else {
        ""
    }

    $WidthMm = if ($Label.ContainsKey("width_mm")) { [double]$Label["width_mm"] } else { 62.0 }
    $HeightMm = if ($Label.ContainsKey("height_mm")) { [double]$Label["height_mm"] } else { 0.0 }
    $Dpi = if ($Label.ContainsKey("dpi")) { [int]$Label["dpi"] } else { 300 }
    $MarginMm = if ($Label.ContainsKey("margin_mm")) { [double]$Label["margin_mm"] } else { 2.0 }
    $BrotherTemplate = if ($Label.ContainsKey("brother_template")) {
        ([string]$Label["brother_template"]).Trim()
    }
    else {
        "templates\label_brother.lbx"
    }

    if ($WidthMm -le 0) {
        throw "label.width_mm must be greater than 0."
    }
    if ($HeightMm -lt 0) {
        throw "label.height_mm must be 0 (automatic) or greater than 0."
    }
    if ($Dpi -lt 72 -or $Dpi -gt 1200) {
        throw "label.dpi must be between 72 and 1200."
    }
    if ($MarginMm -lt 0 -or ($MarginMm * 2) -ge $WidthMm) {
        throw "label.margin_mm is invalid for the configured label width."
    }

    return [PSCustomObject]@{
        Backend         = $Backend
        PrinterName     = $PrinterName
        WidthMm         = $WidthMm
        HeightMm        = $HeightMm
        Dpi             = $Dpi
        MarginMm        = $MarginMm
        BrotherTemplate = $BrotherTemplate
    }
}

function New-LabelFromSummary {
    param(
        [Parameter(Mandatory = $true)]
        $Entry,

        [Parameter(Mandatory = $true)]
        [string]$LabelPath,

        [Parameter(Mandatory = $true)]
        $Configuration
    )

    if (-not (Test-Path -LiteralPath $GeneratorPath)) {
        throw "Label generator is missing: $GeneratorPath"
    }

    $Python = Get-Python
    $GeneratorArgs = @(
        $GeneratorPath,
        $Entry.SummaryPath,
        "--output",
        $LabelPath
    )

    if ($Configuration.Backend -eq "windows") {
        # Pillow is installed through requirements.txt by scripts/start-test.ps1.
        $GeneratorArgs += @(
            "--format", "png",
            "--width-mm", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0}", $Configuration.WidthMm)),
            "--height-mm", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0}", $Configuration.HeightMm)),
            "--dpi", [string]$Configuration.Dpi,
            "--margin-mm", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0}", $Configuration.MarginMm))
        )
    }
    else {
        $TemplatePath = Resolve-ConfiguredPath -Value $Configuration.BrotherTemplate
        if (-not (Test-Path -LiteralPath $TemplatePath)) {
            throw "Brother label template is missing: $TemplatePath"
        }

        $GeneratorArgs += @(
            "--format", "brother-lbx",
            "--template", $TemplatePath
        )
    }

    $CommandArgs = @($Python.Args) + $GeneratorArgs
    & $Python.Command @CommandArgs | Out-Host

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $LabelPath)) {
        throw "Label could not be created."
    }
}

function Get-PrintCount {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Directory
    )

    $Path = Join-Path $Directory "label-print.json"

    if (-not (Test-Path -LiteralPath $Path)) {
        return 0
    }

    try {
        $State = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        return [int]$State.print_count
    }
    catch {
        return 0
    }
}

function Save-PrintState {
    param(
        [Parameter(Mandatory = $true)]
        $Entry,

        [Parameter(Mandatory = $true)]
        [string]$LabelPath,

        [Parameter(Mandatory = $true)]
        [int]$PreviousCount,

        [Parameter(Mandatory = $true)]
        [string]$Backend,

        [string]$PrinterName = ""
    )

    $Path = Join-Path $Entry.Directory "label-print.json"

    $State = [ordered]@{
        board_id        = $Entry.BoardId
        result          = $Entry.Result
        test_timestamp  = $Entry.TimestampRaw
        print_count     = $PreviousCount + 1
        last_printed_at = (Get-Date).ToString("s")
        backend         = $Backend
        printer_name    = $PrinterName
        label_file      = Split-Path -Leaf $LabelPath
    }

    $State |
        ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $Path -Encoding UTF8
}

function Initialize-WindowsLabelPrinter {
    if ("Esp32BoardTestLabel.WindowsPrinter" -as [type]) {
        return
    }

    $Source = @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Printing;

namespace Esp32BoardTestLabel
{
    public static class WindowsPrinter
    {
        public static string Print(string imagePath, string printerName)
        {
            using (Image image = Image.FromFile(imagePath))
            using (PrintDocument document = new PrintDocument())
            {
                if (!String.IsNullOrWhiteSpace(printerName))
                {
                    document.PrinterSettings.PrinterName = printerName;
                }

                if (!document.PrinterSettings.IsValid)
                {
                    throw new InvalidOperationException(
                        "The configured Windows printer is not installed or is not available: " +
                        (String.IsNullOrWhiteSpace(printerName) ? "<default printer>" : printerName)
                    );
                }

                float dpiX = image.HorizontalResolution > 1.0f ? image.HorizontalResolution : 300.0f;
                float dpiY = image.VerticalResolution > 1.0f ? image.VerticalResolution : 300.0f;
                float widthInches = image.Width / dpiX;
                float heightInches = image.Height / dpiY;
                int widthHundredths = Math.Max(1, (int)Math.Ceiling(widthInches * 100.0f));
                int heightHundredths = Math.Max(1, (int)Math.Ceiling(heightInches * 100.0f));

                document.DocumentName = "ESP32 Board Test Label";
                document.PrintController = new StandardPrintController();
                document.OriginAtMargins = false;
                document.DefaultPageSettings.Landscape = false;
                document.DefaultPageSettings.Margins = new Margins(0, 0, 0, 0);
                document.DefaultPageSettings.PaperSize = new PaperSize(
                    "ESP32 Board Label",
                    widthHundredths,
                    heightHundredths
                );

                document.PrintPage += delegate(object sender, PrintPageEventArgs args)
                {
                    args.Graphics.PageUnit = GraphicsUnit.Inch;
                    args.Graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    args.Graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;

                    // Move the image origin to the physical page origin. This
                    // avoids an additional driver hard-margin offset.
                    args.Graphics.TranslateTransform(
                        -args.PageSettings.HardMarginX / 100.0f,
                        -args.PageSettings.HardMarginY / 100.0f
                    );

                    args.Graphics.DrawImage(
                        image,
                        new RectangleF(0.0f, 0.0f, widthInches, heightInches)
                    );
                    args.HasMorePages = false;
                };

                string actualPrinter = document.PrinterSettings.PrinterName;
                document.Print();
                return actualPrinter;
            }
        }
    }
}
'@

    Add-Type -TypeDefinition $Source -ReferencedAssemblies "System.Drawing"
}

function Invoke-WindowsLabelPrint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LabelPath,

        [string]$PrinterName = ""
    )

    if (-not (Test-Path -LiteralPath $LabelPath)) {
        throw "PNG label is missing: $LabelPath"
    }

    Initialize-WindowsLabelPrinter

    try {
        $ActualPrinter = [Esp32BoardTestLabel.WindowsPrinter]::Print($LabelPath, $PrinterName)
    }
    catch {
        throw "Windows label printing failed: $($_.Exception.Message)"
    }

    Write-Host ("Printer: {0}" -f $ActualPrinter)
    return [PSCustomObject]@{
        Printed     = $true
        PrinterName = [string]$ActualPrinter
    }
}

function Invoke-BrotherLabelPrint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LabelPath
    )

    $Document = $null
    $Printer = $null
    $Opened = $false
    $AutoCut = 1

    try {
        try {
            $Document = New-Object -ComObject "bpac.Document"
        }
        catch {
            throw "Brother b-PAC Document component is not installed or is not registered correctly."
        }

        try {
            $Printer = New-Object -ComObject "bpac.Printer"
        }
        catch {
            throw "Brother b-PAC Printer component is not installed or is not registered correctly."
        }

        $Opened = [bool]$Document.Open($LabelPath)

        if (-not $Opened) {
            throw "b-PAC could not open the LBX file: $LabelPath"
        }

        $PrinterName = [string]$Document.GetPrinterName()

        if ([string]::IsNullOrWhiteSpace($PrinterName)) {
            throw "The printer name could not be determined from the LBX file."
        }

        while ($true) {
            try {
                $PrinterOnline = [bool]$Printer.IsPrinterOnline($PrinterName)
            }
            catch {
                throw "Printer status for '$PrinterName' could not be queried: $($_.Exception.Message)"
            }

            if ($PrinterOnline) {
                break
            }

            Write-Host ""
            Write-Host ("Printer is offline: {0}" -f $PrinterName)
            Write-Host "Turn on the printer or check the USB connection."
            $OfflineAnswer = (Read-Host "Enter = check again, A = abort printing").Trim().ToLowerInvariant()

            if ($OfflineAnswer -in @("a", "abort", "q", "quit")) {
                Write-Host "Printing aborted. The label remains saved."
                return [PSCustomObject]@{
                    Printed     = $false
                    PrinterName = $PrinterName
                }
            }
        }

        Write-Host ("Printer online: {0}" -f $PrinterName)

        $StartResult = $Document.StartPrint("", $AutoCut)
        if (($StartResult -is [bool]) -and (-not $StartResult)) {
            throw "b-PAC could not start the print job."
        }

        $PrintResult = $Document.PrintOut(1, $AutoCut)
        if (($PrintResult -is [bool]) -and (-not $PrintResult)) {
            throw "b-PAC reported an error while printing."
        }

        $EndResult = $Document.EndPrint()
        if (($EndResult -is [bool]) -and (-not $EndResult)) {
            throw "b-PAC could not finish the print job cleanly."
        }

        return [PSCustomObject]@{
            Printed     = $true
            PrinterName = $PrinterName
        }
    }
    finally {
        if ($null -ne $Document) {
            if ($Opened) {
                try {
                    [void]$Document.Close()
                }
                catch {
                }
            }

            try {
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Document)
            }
            catch {
            }
        }

        if ($null -ne $Printer) {
            try {
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Printer)
            }
            catch {
            }
        }
    }
}

$Configuration = Get-LabelConfiguration
$Entry = $null

if (-not [string]::IsNullOrWhiteSpace($SummaryPath)) {
    if (-not (Test-Path -LiteralPath $SummaryPath)) {
        throw "summary.json not found: $SummaryPath"
    }

    $ResolvedSummary = (Resolve-Path -LiteralPath $SummaryPath).Path
    $Entry = Read-Summary -Path $ResolvedSummary

    if ($null -eq $Entry) {
        throw "Invalid summary.json: $ResolvedSummary"
    }
}
else {
    $Summaries = @(Get-Summaries)

    if ($Summaries.Count -eq 0) {
        throw "No test results found under results."
    }

    $Requested = (Read-Host "Which label should be created/printed? Board ID/number [Enter = latest]").Trim().ToUpperInvariant()

    if ([string]::IsNullOrWhiteSpace($Requested)) {
        $Entry = $Summaries[0]
    }
    elseif ($Requested -match '^\d{1,3}$') {
        $Number = [int]$Requested

        if ($Number -lt 1 -or $Number -gt 999) {
            throw "Invalid board number: $Requested"
        }

        $NumberText = "{0:D3}" -f $Number
        $MatchingEntries = @(
            $Summaries |
                Where-Object { $_.BoardId -match "^$NumberText-(E32|C3|S3)$" }
        )

        if ($MatchingEntries.Count -eq 0) {
            throw "No test result found for board number $NumberText."
        }

        $MatchingIds = @(
            $MatchingEntries |
                ForEach-Object { $_.BoardId } |
                Sort-Object -Unique
        )

        if ($MatchingIds.Count -gt 1) {
            throw "Board number $NumberText exists more than once in legacy data: $($MatchingIds -join ', '). Enter the full board ID."
        }

        $Entry = $MatchingEntries[0]
    }
    else {
        $CanonicalRequested = Convert-BoardId -Value $Requested

        if ([string]::IsNullOrWhiteSpace($CanonicalRequested)) {
            throw "Invalid board ID: $Requested"
        }

        $MatchingEntries = @(
            $Summaries |
                Where-Object { $_.BoardId -eq $CanonicalRequested }
        )

        if ($MatchingEntries.Count -eq 0) {
            throw "No test result found for $CanonicalRequested."
        }

        $Entry = $MatchingEntries[0]
    }
}

if ($Configuration.Backend -eq "windows") {
    $LabelPath = Join-Path $Entry.Directory ("label_{0}.png" -f $Entry.BoardId)
}
else {
    $LabelPath = Join-Path $Entry.Directory ("label_{0}_brother.lbx" -f $Entry.BoardId)
}

$WasExisting = Test-Path -LiteralPath $LabelPath
# Regenerate every time so changed label dimensions/template settings are
# reflected when a label is reprinted.
New-LabelFromSummary -Entry $Entry -LabelPath $LabelPath -Configuration $Configuration

$PrintCount = Get-PrintCount -Directory $Entry.Directory
$MonthYear = $Entry.Timestamp.ToString("MM/yy")
$Ask = $DefaultAskBeforePrint -or $AskBeforePrint.IsPresent

Write-Host ""
Write-Host "=========================================="
Write-Host (" BOARD:   {0}" -f $Entry.BoardId)
Write-Host (" RESULT:  {0}" -f $Entry.Result)
Write-Host (" DATE:    {0}" -f $MonthYear)
Write-Host (" BACKEND: {0}" -f $Configuration.Backend)

if ($Configuration.Backend -eq "windows") {
    $ConfiguredPrinter = if ([string]::IsNullOrWhiteSpace($Configuration.PrinterName)) {
        "Windows default printer"
    }
    else {
        $Configuration.PrinterName
    }
    Write-Host (" PRINTER: {0}" -f $ConfiguredPrinter)
}
else {
    Write-Host " PRINTER: defined by Brother LBX template"
}

if ($WasExisting) {
    Write-Host " LABEL:   regenerated / reprint"
}
else {
    Write-Host " LABEL:   newly created"
}

Write-Host (" FILE:    {0}" -f (Split-Path -Leaf $LabelPath))
Write-Host (" PRINTED: {0}x" -f $PrintCount)
Write-Host "=========================================="
Write-Host ""

if ($Ask) {
    $Answer = (Read-Host "Print label now? [Y/N]").Trim().ToLowerInvariant()

    if ($Answer -notin @("y", "yes")) {
        Write-Host "Not printed. The label remains saved."
        exit 0
    }
}
else {
    Write-Host "Label is being printed automatically ..."
}

if ($Configuration.Backend -eq "windows") {
    $PrintResult = Invoke-WindowsLabelPrint -LabelPath $LabelPath -PrinterName $Configuration.PrinterName
}
else {
    $PrintResult = Invoke-BrotherLabelPrint -LabelPath $LabelPath
}

if (-not $PrintResult.Printed) {
    exit 0
}

$PrintStateArgs = @{
    Entry         = $Entry
    LabelPath     = $LabelPath
    PreviousCount = $PrintCount
    Backend       = $Configuration.Backend
    PrinterName   = $PrintResult.PrinterName
}
Save-PrintState @PrintStateArgs

Write-Host ""
Write-Host ("Label printed: {0}" -f $Entry.BoardId)
Write-Host ("Print count: {0}x" -f ($PrintCount + 1))
exit 0
