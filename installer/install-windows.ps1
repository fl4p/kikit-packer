$ErrorActionPreference = "Stop"
python "$PSScriptRoot\install.py" @args
exit $LASTEXITCODE
