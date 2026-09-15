"""Команда для удалённого openclaw по ssh собирается без подстановок оболочки.

Аудит 2026-09-15: _raw_api_agent_message для агентов из REMOTE_SSH_CONFIG
(sdev, hoster) склеивал строку `openclaw agent --message "<текст>"` и отдавал её
удалённой оболочке через ssh. Экранировались только кавычки: `$(id)`, обратные
кавычки и `\\"` исполнялись на узле от имени ssh-пользователя. Любой агент
доверенной сети мог выполнить команду на hoster через POST /api/agent/shopin/message.
"""
from __future__ import annotations

import shlex
import subprocess

from proxy_server import REMOTE_SSH_CONFIG, build_remote_agent_command

PAYLOADS = [
    'hello $(echo INJECTED)',
    'hello `echo INJECTED`',
    'x\\" ; echo INJECTED ; echo \\"',
    "it's a 'quoted' message; echo INJECTED",
    'multi\nline $HOME',
]


def _run_as_remote_shell(cmd: str) -> str:
    """Удалённая оболочка получает строку целиком; здесь вместо openclaw — echo."""
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=5).stdout


def test_текст_доходит_до_оболочки_дословно_без_подстановок():
    for msg in PAYLOADS:
        cmd = build_remote_agent_command("echo ", "shopin", msg)
        out = _run_as_remote_shell(cmd)
        # echo печатает аргументы через пробел: подстановки не сработали, если
        # вывод буквально повторяет текст сообщения.
        assert out == f"openclaw agent --agent shopin --message {msg} --json\n", msg


def test_аргументы_разбираются_в_ожидаемый_argv():
    for msg in PAYLOADS:
        cmd = build_remote_agent_command("", "shopin", msg)
        assert shlex.split(cmd) == [
            "openclaw", "agent", "--agent", "shopin", "--message", msg, "--json"], msg


def test_идентификатор_агента_тоже_экранируется():
    cmd = build_remote_agent_command("echo ", "main; echo INJECTED", "hi")
    out = _run_as_remote_shell(cmd)
    assert out.splitlines() == ["openclaw agent --agent main; echo INJECTED --message hi --json"]


def test_префикс_команды_из_конфига_остаётся_сырым():
    """cmd_prefix — доверенное значение из REMOTE_SSH_CONFIG (env, cd). Его не трогаем."""
    cmd = build_remote_agent_command("export PATH=/opt/bin:$PATH; ", "main", "hi")
    assert cmd.startswith("export PATH=/opt/bin:$PATH; openclaw agent")


def test_cmd_exe_кавычка_и_процент_не_вырываются_из_строки():
    """vibe (Windows): вырваться из "..." можно только кавычкой и %VAR%."""
    msg = 'a" & calc & "b %PATH% \n end'
    cmd = build_remote_agent_command('set "X=1" && ', "vboris2", msg, shell="cmd")
    tail = cmd[len('set "X=1" && '):]
    assert tail == ('openclaw agent --agent "vboris2" '
                    '--message "a\' & calc & \'b %%PATH%%   end" --json')


def test_vibe_объявлен_как_cmd_оболочка():
    assert REMOTE_SSH_CONFIG["vibe"].get("shell") == "cmd"
    for node in ("sdev", "hoster"):
        assert REMOTE_SSH_CONFIG[node].get("shell", "sh") == "sh"
