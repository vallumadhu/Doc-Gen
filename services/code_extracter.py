import os
import stat
import shutil
from pathlib import Path
from git import Repo


def _force_remove(func, path, exc):
    # git marks pack files read-only on Windows; clear the flag and retry
    os.chmod(path, stat.S_IWRITE)
    func(path)


def get_repo(url: str, target: str = "codebase") -> Repo:
    if Path(target).exists():
        shutil.rmtree(target, onexc=_force_remove)
    return Repo.clone_from(url, target, depth=1)