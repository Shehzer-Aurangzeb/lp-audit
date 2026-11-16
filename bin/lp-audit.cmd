@echo off
rem Lets you type "lp-audit ..." in PowerShell or Command Prompt.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lp-audit.ps1" %*
