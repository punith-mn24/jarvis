"""Create local configuration without replacing an existing .env or printing secrets."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / ".env"
if path.exists():
    print("Existing .env preserved.")
else:
    contents = (
        (root / ".env.example")
        .read_text()
        .replace("replace-this-with-a-random-token", secrets.token_urlsafe(32))
    )
    # Restrictive permissions from creation, including before the token is written.
    with path.open("x") as file:
        path.chmod(0o600)
        file.write(contents)
    print("Created .env with a random workspace token. Keep this file private.")
print("Use JARVIS_ACCESS_TOKEN from .env to unlock the browser workspace.")
