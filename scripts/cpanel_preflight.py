"""Pre-deployment self-check for cPanel / shared hosting.

Run it over SSH or in cPanel's Terminal from the project directory, with the
same Python that will run the bot::

    python scripts/cpanel_preflight.py

Every line is PASS, WARN or FAIL. The exit code is 1 when anything FAILs.
Secret values are never printed; only whether they are set.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_PYTHON = (3, 11)
MIN_FREE_BYTES = 3 * 1024**3
#: One address per Telegram data center is enough to prove outbound MTProto works.
TELEGRAM_PROBES = (("149.154.167.51", 443), ("149.154.175.53", 443))
REQUIRED_PACKAGES = (
    "Telethon", "aiohttp", "aiosqlite", "python-dotenv", "python-pptx",
    "lxml", "av", "ppt2pptx", "python-docx",
)


@dataclass(frozen=True)
class Result:
    level: str  # PASS | WARN | FAIL
    name: str
    detail: str = ""


def check_python() -> Result:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] < MIN_PYTHON:
        return Result(
            "FAIL", "Python version",
            f"{version} is too old; 3.{MIN_PYTHON[1]}+ is required. In cPanel "
            "\"Setup Python App\" choose 3.11 or newer.",
        )
    return Result("PASS", "Python version", version)


def check_platform() -> Result:
    if os.name != "posix":
        return Result("WARN", "Operating system", platform.platform())
    libc = platform.libc_ver()
    detail = f"{platform.system()} {platform.machine()} {libc[0]} {libc[1]}".strip()
    try:
        major, minor = (int(part) for part in libc[1].split(".")[:2])
    except ValueError:
        return Result("PASS", "Operating system", detail)
    if libc[0] == "glibc" and (major, minor) < (2, 28):
        return Result(
            "FAIL", "Operating system",
            f"{detail}: glibc < 2.28 (CentOS/CloudLinux 7) cannot install the PyAV "
            "and lxml wheels. Ask the host for CloudLinux/AlmaLinux 8 or newer.",
        )
    return Result("PASS", "Operating system", detail)


def check_packages() -> list[Result]:
    results = []
    for package in REQUIRED_PACKAGES:
        try:
            results.append(Result("PASS", f"package {package}", importlib.metadata.version(package)))
        except importlib.metadata.PackageNotFoundError:
            results.append(
                Result("FAIL", f"package {package}", "not installed: pip install -r requirements.txt")
            )
    try:
        importlib.import_module("av")
    except Exception as exc:  # noqa: BLE001 - report any import failure
        results.append(Result("FAIL", "PyAV import", f"{type(exc).__name__}: {exc}"))
    try:
        importlib.import_module("python_socks")
    except ImportError:
        results.append(Result("WARN", "package python-socks", "needed only when TELEGRAM_PROXY is set"))
    return results


def check_location(root: Path = ROOT) -> Result:
    parts = {part.lower() for part in root.parts}
    if "public_html" in parts or "www" in parts:
        return Result(
            "FAIL", "Project location",
            f"{root} is inside the web root: .env, the database and the Telegram "
            "session could be downloaded by anyone. Move the project outside public_html.",
        )
    return Result("PASS", "Project location", str(root))


def check_lock_support(directory: Path) -> Result:
    from gamas_bot.instance_lock import AlreadyRunningError, InstanceLock

    path = directory / ".preflight.lock"
    try:
        with InstanceLock(path):
            try:
                InstanceLock(path).acquire()
            except AlreadyRunningError:
                return Result("PASS", "Single-instance lock", "file locking works")
            return Result(
                "FAIL", "Single-instance lock",
                "file locking is not enforced on this filesystem; two bot copies could run.",
            )
    except OSError as exc:
        return Result("FAIL", "Single-instance lock", str(exc))
    finally:
        path.unlink(missing_ok=True)


def check_directory(name: str, directory: Path) -> Result:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".preflight-write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return Result("FAIL", name, f"{directory} is not writable: {exc}")
    free = shutil.disk_usage(directory).free
    if free < MIN_FREE_BYTES:
        return Result(
            "WARN", name,
            f"{directory}: only {free / 1024**3:.1f} GB free; large lectures need several GB.",
        )
    return Result("PASS", name, f"{directory} ({free / 1024**3:.1f} GB free)")


def check_settings() -> tuple[list[Result], object | None]:
    from gamas_bot.config import Settings

    env_file = ROOT / ".env"
    results: list[Result] = []
    if not env_file.is_file():
        results.append(Result("WARN", ".env", f"{env_file} not found; using process environment only"))
    else:
        mode = env_file.stat().st_mode & 0o077
        if os.name == "posix" and mode:
            results.append(Result("WARN", ".env permissions", "run: chmod 600 .env"))
    try:
        settings = Settings.from_env()
        settings.validate_runtime()
    except ValueError as exc:
        results.append(Result("FAIL", "Configuration", str(exc)))
        return results, None
    results.append(Result("PASS", "Configuration", "required settings are present and valid"))
    if not settings.admin_ids:
        results.append(Result("WARN", "ADMIN_IDS", "no administrator is configured"))
    if settings.max_concurrent_jobs > 1:
        results.append(
            Result(
                "WARN", "MAX_CONCURRENT_JOBS",
                f"{settings.max_concurrent_jobs}: shared hosting usually limits memory "
                "to 1-2 GB; start with 1.",
            )
        )
    results.extend(check_stt_queue(settings))
    return results, settings


def check_stt_queue(settings) -> list[Result]:
    """Warn about a queue or a custom dictionary that will not fit one job."""
    results: list[Result] = []
    if settings.max_pending_jobs > 4 * max(1, settings.max_concurrent_jobs):
        results.append(
            Result(
                "WARN",
                "MAX_PENDING_JOBS",
                f"{settings.max_pending_jobs} waiting jobs for {settings.max_concurrent_jobs} "
                "worker(s): uploads beyond that are rejected with back-pressure.",
            )
        )
    terms = len(settings.speechmatics_additional_vocab)
    if terms and terms > settings.speechmatics_vocab_max_items:
        results.append(
            Result(
                "WARN",
                "SPEECHMATICS_ADDITIONAL_VOCAB",
                f"{terms} terms configured but only the first "
                f"{settings.speechmatics_vocab_max_items} are sent per job; raise "
                "SPEECHMATICS_VOCAB_MAX_ITEMS (1000 recommended, 20000 hard cap) or "
                "reorder the terms so the most valuable ones come first.",
            )
        )
    return results


def tcp_reachable(host: str, port: int, timeout: float = 6.0) -> str | None:
    """None when a TCP connection succeeds, otherwise the error text."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def check_network(settings) -> list[Result]:
    results = []
    if settings is not None and settings.telegram_proxy:
        _kind, host, port, *_rest = settings.telegram_proxy
        error = tcp_reachable(host, port)
        results.append(
            Result("PASS", "Telegram proxy", f"{host}:{port} reachable")
            if error is None
            else Result("FAIL", "Telegram proxy", f"{host}:{port} unreachable ({error})")
        )
    else:
        probes = [(host, port, tcp_reachable(host, port)) for host, port in TELEGRAM_PROBES]
        failures = [f"{host}:{port} ({error})" for host, port, error in probes if error is not None]
        if len(failures) == len(TELEGRAM_PROBES):
            results.append(
                Result(
                    "FAIL", "Telegram connectivity",
                    "cannot open outbound TCP 443 to Telegram: " + "; ".join(failures)
                    + ". Ask the host to allow it, or set TELEGRAM_PROXY.",
                )
            )
        else:
            results.append(Result("PASS", "Telegram connectivity", "MTProto servers reachable"))
    hosts: list[tuple[str, str]] = []
    if settings is not None:
        if settings.speechmatics_api_key:
            hosts.append(("Speechmatics", settings.speechmatics_base_url))
        if settings.deepgram_api_key:
            hosts.append(("Deepgram", "https://api.deepgram.com"))
        if settings.stt_openai_base_url:
            hosts.append(("OpenAI-compatible STT", settings.stt_openai_base_url))
        if settings.note_api_provider != "disabled":
            base = settings.note_api_base_url or {
                "gemini": "https://generativelanguage.googleapis.com",
                "anthropic": "https://api.anthropic.com",
            }.get(settings.note_api_provider, "https://api.openai.com")
            hosts.append((f"Note API ({settings.note_api_provider})", base))
    for label, url in hosts:
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        error = tcp_reachable(parsed.hostname or "", port)
        results.append(
            Result("PASS", f"{label} reachable", f"{parsed.hostname}:{port}")
            if error is None
            else Result("FAIL", f"{label} reachable", f"{parsed.hostname}:{port} ({error})")
        )
    return results


def check_media_worker() -> Result:
    import asyncio

    from gamas_bot.media import MediaToolError, check_media_worker as run_check

    try:
        summary = asyncio.run(run_check())
    except MediaToolError as exc:
        return Result("FAIL", "Media worker", str(exc))
    return Result("PASS", "Media worker", summary)


def run_all(*, network: bool = True) -> list[Result]:
    results = [check_python(), check_platform(), check_location()]
    results += check_packages()
    setting_results, settings = check_settings()
    results += setting_results
    if settings is not None:
        results.append(check_directory("Data directory", settings.session_path.parent))
        results.append(check_directory("Temp directory", settings.temp_dir))
        results.append(check_lock_support(settings.session_path.parent))
    if network:
        results += check_network(settings)
    if not any(r.level == "FAIL" and r.name.startswith("package") for r in results):
        results.append(check_media_worker())
    return results


def main(argv: list[str] | None = None) -> int:
    network = "--offline" not in (argv if argv is not None else sys.argv[1:])
    results = run_all(network=network)
    for result in results:
        print(f"[{result.level:<4}] {result.name}" + (f": {result.detail}" if result.detail else ""))
    failed = sum(r.level == "FAIL" for r in results)
    warned = sum(r.level == "WARN" for r in results)
    print(f"\n{len(results)} checks, {failed} failed, {warned} warnings")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
