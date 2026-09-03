"""Small shared helper for reading/writing single keys in .env, used by dashboard_server.py,
fetch_login_cookie.py, and ensure_connection.py so the same logic isn't copy-pasted three times."""
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def write_env_var(key: str, value: str, env_path: Path = ENV_PATH):
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.MULTILINE):
        text = re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.MULTILINE)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    env_path.write_text(text, encoding="utf-8")
