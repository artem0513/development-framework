$aseSourceRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = $aseSourceRoot + [IO.Path]::PathSeparator + $env:PYTHONPATH
python -m ase @args
exit $LASTEXITCODE
