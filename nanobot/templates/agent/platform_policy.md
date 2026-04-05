{% if system == 'Windows' %}
## Platform Policy (Windows)
- You are running on Windows with a Git Bash (POSIX-compatible) shell.
- Use standard Unix/POSIX commands (`grep`, `sed`, `awk`, `ls`, `cat`, etc.), NOT Windows CMD commands (`dir`, `type`, `findstr`, etc.).
- Use forward slashes `/` in paths, not backslashes `\`.
- If you need a Windows-native command (e.g. registry, WMI, service management), run it via `pwsh -c "..."` or `powershell -c "..."`.
- If terminal output is garbled, retry with UTF-8 output enabled.
{% else %}
## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
{% endif %}
