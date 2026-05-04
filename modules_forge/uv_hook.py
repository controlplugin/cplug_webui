import shlex
import shutil
import subprocess
import sys
from copy import copy
from functools import wraps


def _ensure_uv() -> bool:
    if shutil.which("uv"):
        return True
    print("[uv-hook] 'uv' not on PATH — bootstrapping via pip into the active venv...", flush=True)
    result = subprocess.run([sys.executable, "-m", "pip", "install", "uv"])
    if result.returncode != 0 or not shutil.which("uv"):
        print("[uv-hook] failed to install uv; falling back to plain pip.", flush=True)
        return False
    return True


def patch(symlink: bool):
    if hasattr(subprocess, "__original_run"):
        return

    # --uv was requested but the uv binary isn't installed yet. Bootstrap it
    # so the launcher's default `--uv` flag doesn't break on fresh venvs.
    if not _ensure_uv():
        return

    subprocess.__original_run = subprocess.run
    BAD_FLAGS = ("--prefer-binary", "--ignore-installed", "-I")

    @wraps(subprocess.__original_run)
    def patched_run(*args, **kwargs):
        _original_args = copy(args)
        _original_kwargs = copy(kwargs)

        if args:
            command, *_args = args
        else:
            command, _args = kwargs.pop("args", ""), ()

        if isinstance(command, str):
            command = shlex.split(command)
        else:
            command = [arg.strip() for arg in command]

        assert isinstance(command, list)

        if "pip" not in command:
            return subprocess.__original_run(*_original_args, **_original_kwargs)

        cmd = command[command.index("pip") + 1 :]

        cmd = [arg for arg in cmd if arg not in BAD_FLAGS]

        modified_command: list[str] = ["uv", "pip", *cmd]

        if symlink:
            modified_command.extend(["--link-mode", "symlink"])

        command = [*modified_command, *_args]
        if kwargs.get("shell", False):
            command = shlex.join(command).replace("'", '"')

        return subprocess.__original_run(command, **kwargs)

    subprocess.run = patched_run
