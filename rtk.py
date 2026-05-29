"""RTK integration — run dev commands through RTK for compressed output.

RTK (rtk-ai/rtk) compresses verbose CLI output by 60-90%.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

ALLOWED_COMMANDS = frozenset({
    "git", "ls", "cat", "grep", "find", "cargo", "npm",
    "pytest", "docker", "gh", "pip", "python", "python3",
    "make", "curl", "wget", "echo", "head", "tail", "sort",
    "uniq", "wc", "diff", "rsync", "tree",
})


class RTK:
    """Wrapper around rtk binary for output compression."""

    def __init__(self, binary_path: str = "~/.local/bin/rtk") -> None:
        self._binary = Path(binary_path).expanduser()
        self._available: bool | None = None
        self._total_raw_chars: int = 0
        self._total_compressed_chars: int = 0

    def check_available(self) -> bool:
        """Check if rtk binary is installed."""
        if self._available is not None:
            return self._available
        try:
            result = subprocess.run(
                [str(self._binary), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self._available = result.returncode == 0
            if self._available:
                logger.info("rtk_available", version=result.stdout.strip())
            else:
                logger.warning("rtk_not_available")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            self._available = False
            logger.warning("rtk_not_found", path=str(self._binary))
        return self._available

    def exec(self, command: str, cwd: str | None = None) -> dict[str, Any]:
        """Execute a command through RTK and return compressed output.

        Args:
            command: Shell command string (first token must be an allowed command)
            cwd: Working directory for the command (default: current dir)

        Returns:
            dict with stdout (compressed), stderr, returncode, gain stats
        """
        if not self.check_available():
            return {
                "success": False,
                "error": "rtk binary not available",
                "stdout": "",
                "stderr": "",
                "returncode": -1,
            }

        tokens = command.strip().split()
        if not tokens:
            return {
                "success": False,
                "error": "empty command",
                "stdout": "",
                "stderr": "",
                "returncode": -1,
            }

        base_cmd = tokens[0]
        if base_cmd not in ALLOWED_COMMANDS:
            return {
                "success": False,
                "error": f"command not allowed: {base_cmd}",
                "stdout": "",
                "stderr": "",
                "returncode": -1,
            }

        # Block shell injection
        dangerous = {"|", "&&", "||", ";", "`", "$("}
        if any(ch in command for ch in dangerous):
            return {
                "success": False,
                "error": "shell metacharacters not allowed",
                "stdout": "",
                "stderr": "",
                "returncode": -1,
            }

        work_dir = cwd or os.getcwd()

        try:
            # First get raw output (no rtk)
            raw_result = subprocess.run(
                tokens,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=work_dir,
            )
            raw_chars = len(raw_result.stdout)

            # Then run through rtk for compression
            rtk_result = subprocess.run(
                [str(self._binary), *tokens],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=work_dir,
            )

            compressed_output = rtk_result.stdout or raw_result.stdout
            compressed_chars = len(compressed_output)

            gain = 0.0
            if raw_chars > 0:
                gain = round((1 - compressed_chars / raw_chars) * 100, 1)

            self._total_raw_chars += raw_chars
            self._total_compressed_chars += compressed_chars

            return_code = (
                raw_result.returncode
                if rtk_result.returncode == 0 or not rtk_result.stdout
                else rtk_result.returncode
            )

            return {
                "success": raw_result.returncode == 0 or rtk_result.returncode == 0,
                "stdout": compressed_output,
                "stderr": rtk_result.stderr or raw_result.stderr,
                "returncode": return_code,
                "gain_pct": gain,
                "raw_chars": raw_chars,
                "compressed_chars": compressed_chars,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "command timed out",
                "stdout": "",
                "stderr": "",
                "returncode": -1,
            }
        except OSError as exc:
            return {
                "success": False,
                "error": str(exc),
                "stdout": "",
                "stderr": "",
                "returncode": -1,
            }

    @property
    def total_gain_pct(self) -> float:
        if self._total_raw_chars == 0:
            return 0.0
        return round(
            (1 - self._total_compressed_chars / self._total_raw_chars) * 100, 1
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "available": self._available or False,
            "total_raw_chars": self._total_raw_chars,
            "total_compressed_chars": self._total_compressed_chars,
            "total_gain_pct": self.total_gain_pct,
        }
