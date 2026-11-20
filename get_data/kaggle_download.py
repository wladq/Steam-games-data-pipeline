"""Download one file of a Kaggle dataset into the project tmp/ folder.

Kaggle credentials are read by the kaggle package itself, in this order: the
KAGGLE_API_TOKEN environment variable, ~/.kaggle/access_token, the legacy
~/.kaggle/kaggle.json (or KAGGLE_USERNAME + KAGGLE_KEY), then cached OAuth
credentials from ``kaggle auth login``.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# owner/dataset, optionally followed by /versions/N or other URL tail
_REF_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)")


class DownloadError(RuntimeError):
    """Kaggle could not authenticate, list the dataset's files or download one."""


def parse_dataset_ref(text: str) -> str:
    """Accept 'owner/name' or a kaggle.com dataset URL and return 'owner/name'."""
    ref = text.strip().strip('"').strip("'")
    ref = re.sub(r"^https?://(www\.)?kaggle\.com/", "", ref)
    ref = re.sub(r"^datasets/", "", ref)
    match = _REF_RE.match(ref)
    if not match:
        raise ValueError(f"Not a Kaggle dataset reference: {text!r}. "
                         "Expected 'owner/dataset-name'.")
    return f"{match['owner']}/{match['name']}"


def plain_file_name(name: str) -> str:
    """`name` itself when it is a bare file name.  Anything that could point outside the
    download folder ('../x', 'data/x.csv', an absolute path) raises a ValueError."""
    if name in ("", ".", "..") or "/" in name or "\\" in name or Path(name).name != name:
        raise ValueError(f"Refusing the file name {name!r}: not a plain file name")
    return name


def _make_api():
    # Import the class, not the package: `import kaggle` authenticates at import
    # time and prints a help wall when no credentials exist.  Imported here so the
    # other steps never load it, and a missing package becomes the clear message below.
    try:
        # pylint: disable-next=import-outside-toplevel
        from kaggle.api.kaggle_api_extended import KaggleApi
    except ImportError:
        raise DownloadError(
            "The 'kaggle' package is not installed. Run: pip install kaggle"
        ) from None

    api = KaggleApi()
    try:
        api.authenticate()
    except SystemExit:  # kaggle calls exit(1) after printing its own help text
        raise DownloadError(
            "Kaggle authentication failed. Run `kaggle auth login`, or create a token at "
            "https://www.kaggle.com/settings/api and set KAGGLE_API_TOKEN."
        ) from None
    return api


def list_dataset_files(dataset: str, api=None) -> list[tuple[str, int]]:
    """Return [(file_name, size_bytes), ...] for every file in the dataset."""
    api = api or _make_api()
    files: list[tuple[str, int]] = []
    token = None
    try:
        while True:
            result = api.dataset_list_files(dataset, page_token=token, page_size=200)
            if getattr(result, "error_message", None):
                raise DownloadError(f"Kaggle: {result.error_message}")
            files += [(f.name, int(getattr(f, "total_bytes", 0) or 0))
                      for f in (result.files or [])]
            token = getattr(result, "next_page_token", None)
            if not token:
                break
    except SystemExit:
        raise DownloadError(f"Kaggle refused to list the files of {dataset}.") from None
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"Could not list the files of {dataset}: {exc}") from exc
    if not files:
        raise DownloadError(f"Kaggle dataset {dataset} has no files (or does not exist).")
    return files


def download_dataset_file(dataset: str, file_name: str, dest_dir: Path, api=None) -> Path:
    """Download `file_name` from `dataset` into `dest_dir` and return the local path."""
    plain_file_name(file_name)  # before anything is created or asked
    dest_dir.mkdir(parents=True, exist_ok=True)
    api = api or _make_api()
    log.info("Downloading %s from kaggle.com/datasets/%s into %s ...", file_name, dataset, dest_dir)
    try:
        api.dataset_download_file(dataset, file_name, path=str(dest_dir), force=True,
                                  quiet=False, unzip=True)
    except SystemExit:
        raise DownloadError(f"Kaggle refused the download of {file_name} from {dataset}.") from None
    except Exception as exc:  # HTTP 403/404 etc. surface as library-specific exceptions
        raise DownloadError(f"Could not download {file_name} from {dataset}: {exc}") from exc

    target = dest_dir / file_name
    if not target.is_file():
        raise DownloadError(f"Download finished but {target} was not found.")
    log.info("Downloaded %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
    return target


def clear_tmp(tmp_dir: Path, project_root: Path) -> None:
    """Delete the project tmp/ folder.  Refuses any other path."""
    tmp_dir = tmp_dir.resolve()
    if tmp_dir != (project_root / "tmp").resolve():
        raise ValueError(f"Refusing to delete {tmp_dir}: not the project tmp folder")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
        log.info("Cleared %s", tmp_dir)
