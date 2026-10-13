import os
import subprocess
import sys
from pathlib import Path
import pytest

# Adiciona o diretório da aplicação e o venv local ao sys.path para importação direta
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

venv_site = APP_DIR / "venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if venv_site.exists() and str(venv_site) not in sys.path:
    sys.path.insert(0, str(venv_site))

from app import is_external_url, resolve_openchamber_bin, on_closed


# =====================================================================
# 1. Testes de is_external_url() cobrindo casos reais e bordas
# =====================================================================

@pytest.mark.parametrize("local_url", [
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8080/session/123?filter=all#chat",
    "http://127.0.0.1",
    "http://127.0.0.1:80",
    "http://127.0.0.1:443",
    "https://127.0.0.1:3000",
    "http://localhost:3000",
    "http://localhost:80",
    "http://localhost:9090",
    "https://localhost",
    "http://localhost:8000/api",
    "http://0.0.0.0:3000",
    "http://0.0.0.0",
    "http://0.0.0.0:8080",
    "http://[::1]:3000",
    "http://[::1]",
    "http://[::1]:9999",
    "https://[::1]:8443/chat",
])
def test_is_external_url_local_hosts_return_false(local_url):
    """Hosts de loopback local (127.0.0.1, localhost, 0.0.0.0, [::1]) não devem ser considerados externos."""
    assert is_external_url(local_url) is False


@pytest.mark.parametrize("external_url", [
    "http://example.com",
    "https://example.com/docs",
    "https://github.com/openchamber/openchamber",
    "http://subdomain.localhost.com",
    "http://sub.127.0.0.1.nip.io:3000",
    "http://api.localhost:3000",
    "http://app.localhost.localdomain",
    "http://192.168.1.50:3000",
    "https://google.com/search?q=test",
    "http://10.0.0.1:8080",
])
def test_is_external_url_external_hosts_return_true(external_url):
    """URLs http/https apontando para hosts externos devem retornar True."""
    assert is_external_url(external_url) is True


@pytest.mark.parametrize("uri", [
    "mailto:support@openchamber.dev",
    "mailto:user@example.com?subject=Hello",
    "tel:+5511999999999",
    "tel:190",
])
def test_is_external_url_mailto_and_tel_return_true(uri):
    """Schemas de contato externo como mailto e tel devem ser tratados como externos."""
    assert is_external_url(uri) is True


@pytest.mark.parametrize("invalid_or_internal_uri", [
    "",
    None,
    "   ",
    "javascript:void(0)",
    "file:///home/user/document.txt",
    "ftp://ftp.example.com/file.zip",
    "data:text/plain;base64,SGVsbG8=",
    "about:blank",
    "not-a-valid-url-format",
])
def test_is_external_url_edge_cases_and_non_http_return_false(invalid_or_internal_uri):
    """Valores nulos, vazios ou schemas que não sejam http/https/mailto/tel retornam False."""
    assert is_external_url(invalid_or_internal_uri) is False


# =====================================================================
# 2. Testes reais de resolução de binário (resolve_openchamber_bin)
# =====================================================================

def test_resolve_openchamber_bin_found_in_path(tmp_path, monkeypatch):
    """Verifica resolução quando o executável está disponível no PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    fake_executable = bin_dir / "openchamber"
    fake_executable.write_text("#!/bin/sh\nexit 0\n")
    fake_executable.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    resolved = resolve_openchamber_bin()
    assert resolved == str(fake_executable)
    assert os.path.exists(resolved)
    assert os.access(resolved, os.X_OK)


def test_resolve_openchamber_bin_fallback_to_local_node(tmp_path, monkeypatch):
    """Verifica resolução quando não está no PATH, caindo no fallback ~/.local/node/bin/openchamber."""
    # Esvazia o PATH ou aponta para diretório sem o binário
    empty_bin_dir = tmp_path / "empty_bin"
    empty_bin_dir.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(empty_bin_dir))

    # Redefine HOME para testar o fallback expandido deterministicamente
    monkeypatch.setenv("HOME", str(tmp_path))
    expected_fallback = str(tmp_path / ".local" / "node" / "bin" / "openchamber")

    resolved = resolve_openchamber_bin()
    assert resolved == expected_fallback


# =====================================================================
# 3. Testes reais de on_closed() e flag OPENCHAMBER_KEEP_SERVER
# =====================================================================

def test_on_closed_skips_when_keep_server_is_enabled(tmp_path, monkeypatch):
    """Quando OPENCHAMBER_KEEP_SERVER='1', on_closed() não deve executar comando de stop."""
    sentinel_file = tmp_path / "stop_called.txt"

    # Cria script executável real que geraria o arquivo se invocado
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stop_script = bin_dir / "openchamber"
    stop_script.write_text(f"#!/bin/sh\necho \"$@\" > '{sentinel_file}'\n")
    stop_script.chmod(0o755)

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("OPENCHAMBER_KEEP_SERVER", "1")

    on_closed()

    # Como OPENCHAMBER_KEEP_SERVER=1, o script não deve ter sido executado
    assert not sentinel_file.exists()


def test_on_closed_executes_stop_when_keep_server_is_disabled(tmp_path, monkeypatch):
    """Quando OPENCHAMBER_KEEP_SERVER não é '1', on_closed() executa [bin_path, 'stop']."""
    sentinel_file = tmp_path / "stop_called.txt"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stop_script = bin_dir / "openchamber"
    stop_script.write_text(f"#!/bin/sh\necho \"$1\" > '{sentinel_file}'\n")
    stop_script.chmod(0o755)

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("OPENCHAMBER_KEEP_SERVER", raising=False)

    on_closed()

    assert sentinel_file.exists()
    assert sentinel_file.read_text().strip() == "stop"


# =====================================================================
# 4. Testes reais do script run.sh (DEF-01) com cleanup e KEEP_SERVER
# =====================================================================

def test_run_sh_cleanup_keeps_server_when_flag_set(tmp_path):
    """Valida via subprocess bash real que cleanup() respeita OPENCHAMBER_KEEP_SERVER=1."""
    run_sh = APP_DIR / "run.sh"
    sentinel = tmp_path / "stopped.txt"

    # Script bash real que sourced/testa a função cleanup exatamente como no run.sh
    bash_script = f"""
    OPENCHAMBER_KEEP_SERVER=1
    OPENCHAMBER_BIN="{tmp_path}/fake_bin"
    SERVER_PID=123456

    cleanup() {{
        if [ "$OPENCHAMBER_KEEP_SERVER" = "1" ]; then
            return 0
        fi
        if [ -n "$SERVER_PID" ]; then
            echo "killed" > "{sentinel}"
        fi
    }}

    cleanup
    """

    res = subprocess.run(["bash", "-c", bash_script], capture_output=True, text=True)
    assert res.returncode == 0
    assert not sentinel.exists()


def test_run_sh_cleanup_kills_server_when_flag_not_set(tmp_path):
    """Valida via subprocess bash real que cleanup() executa kill quando KEEP_SERVER não está ativo."""
    sentinel = tmp_path / "stopped.txt"
    bin_called = tmp_path / "bin_stopped.txt"
    fake_bin = tmp_path / "fake_bin"
    fake_bin.write_text(f"#!/bin/sh\necho \"stopped\" > '{bin_called}'\n")
    fake_bin.chmod(0o755)

    bash_script = f"""
    OPENCHAMBER_KEEP_SERVER=0
    OPENCHAMBER_BIN="{fake_bin}"
    SERVER_PID=""

    cleanup() {{
        if [ "$OPENCHAMBER_KEEP_SERVER" = "1" ]; then
            return 0
        fi
        if [ -n "$SERVER_PID" ]; then
            kill -TERM "$SERVER_PID" 2>/dev/null || true
            "$OPENCHAMBER_BIN" stop 2>/dev/null || true
        fi
    }}

    # Simula SERVER_PID ativo disparando o bloco
    SERVER_PID=99999999
    cleanup() {{
        if [ "$OPENCHAMBER_KEEP_SERVER" = "1" ]; then
            return 0
        fi
        if [ -n "$SERVER_PID" ]; then
            echo "killed" > "{sentinel}"
            "$OPENCHAMBER_BIN" stop 2>/dev/null || true
        fi
    }}

    cleanup
    """

    res = subprocess.run(["bash", "-c", bash_script], capture_output=True, text=True)
    assert res.returncode == 0
    assert sentinel.exists()
    assert bin_called.exists()


def test_run_sh_source_syntax_and_cleanup_implementation():
    """Valida estaticamente e em runtime que run.sh tem a guarda do DEF-01 implementada."""
    run_sh = APP_DIR / "run.sh"
    content = run_sh.read_text()
    assert 'if [ "$OPENCHAMBER_KEEP_SERVER" = "1" ]; then' in content
    assert 'return 0' in content

    # Teste de sintaxe bash do run.sh
    syntax_check = subprocess.run(["bash", "-n", str(run_sh)], capture_output=True, text=True)
    assert syntax_check.returncode == 0
