@echo off
REM Locate uvx — tries common Windows install locations first, then PATH

for %%P in (
    "%USERPROFILE%\.local\bin\uvx.exe"
    "%USERPROFILE%\.cargo\bin\uvx.exe"
    "%LOCALAPPDATA%\uv\bin\uvx.exe"
) do (
    if exist %%P (
        %%P nextcloud-mcp-server run --transport stdio
        exit /b %errorlevel%
    )
)

REM Fall back to uvx on PATH if found
where uvx >nul 2>&1
if %errorlevel% equ 0 (
    uvx nextcloud-mcp-server run --transport stdio
    exit /b %errorlevel%
)

REM uvx not found — print actionable error
echo Error: 'uvx' was not found in any expected location. >&2
echo Install uv (which provides uvx) from: https://docs.astral.sh/uv/getting-started/installation/ >&2
echo   Windows: winget install astral-sh.uv >&2
echo   or:      powershell -c "irm https://astral.sh/uv/install.ps1 | iex" >&2
exit /b 1
