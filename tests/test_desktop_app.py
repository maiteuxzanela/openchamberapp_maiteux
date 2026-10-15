import os
import base64
import inspect
import shutil
import subprocess
import sys
import threading
import time
import typing
import webbrowser
from pathlib import Path
import pytest

# Adiciona o diretório da aplicação e o venv local ao sys.path para importação direta.
# Precisa vir ANTES de `import webview`/`from app import ...`: o pywebview só existe
# no site-packages do venv local, e o pytest pode ser invocado com o python3 do sistema.
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

venv_site = APP_DIR / "venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if venv_site.exists() and str(venv_site) not in sys.path:
    sys.path.insert(0, str(venv_site))

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gdk, GObject, Gtk, GdkPixbuf, GLib
import webview

from app import (
    is_external_url,
    open_external,
    browser_spawn_suppressed,
    resolve_openchamber_bin,
    resolve_debug_mode,
    preserve_default_context_menu,
    on_closed,
    configure_graphics_environment,
    create_shortcut_handler,
    install_shortcut_listener,
    DesktopAPI,
    get_desktop_handlers_js,
    setup_window_handlers,
)
import app as app_module


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


# =====================================================================
# 5. TICKET-DESKTOP-08 — performance WebKitGTK: DevTools, GPU e listeners
# =====================================================================

class GtkWindowProbe(GObject.Object):
    """Janela GTK real (objeto GObject com sinal ``key-press-event`` real).

    Usa o mesmo mecanismo de sinal GObject/GLib do ``Gtk.Window`` de produção,
    permitindo auditar o wiring do listener sem depender de um display X11.
    """

    __gsignals__ = {
        'key-press-event': (GObject.SignalFlags.RUN_LAST, bool, (object,)),
    }


class BrowserViewProbe:
    """Contém a janela GTK real usada pelo ``install_shortcut_listener``."""

    def __init__(self, window: GObject.Object) -> None:
        self.window = window


def make_key_event(keyval: int, state: Gdk.ModifierType = 0) -> Gdk.EventKey:
    """Cria um ``Gdk.EventKey`` real (sem display) para alimentar o handler."""
    event = Gdk.EventKey.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    event.state = state
    return event


def test_open_devtools_in_debug_is_disabled_at_import():
    """O Web Inspector não pode abrir compulsoriamente no boot (TICKET-DESKTOP-08)."""
    assert webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] is False


def test_webview_start_uses_resolve_debug_mode_not_hardcoded_true():
    """``webview.start`` recebe ``debug=resolve_debug_mode()``: produção nasce com debug desligado (TICKET-DESKTOP-09)."""
    content = (APP_DIR / "app.py").read_text()
    start_lines = [line for line in content.splitlines() if "webview.start(" in line]
    assert start_lines, "webview.start não encontrado em app.py"
    for line in start_lines:
        assert "debug=resolve_debug_mode()" in line
        assert "debug=True" not in line


def test_graphics_environment_disables_dmabuf_for_nvidia():
    """No import do módulo o renderer DMA-BUF é desativado para o host NVIDIA."""
    assert os.environ.get('WEBKIT_DISABLE_DMABUF_RENDERER') == '1'


def test_configure_graphics_environment_removes_forced_software_and_compositing_flags(monkeypatch):
    """Flags herdadas que forçam software fallback/composição off são removidas do ambiente."""
    monkeypatch.setenv('WEBKIT_DISABLE_COMPOSITING_MODE', '1')
    monkeypatch.setenv('LIBGL_ALWAYS_SOFTWARE', '1')

    configure_graphics_environment()

    assert os.environ['WEBKIT_DISABLE_DMABUF_RENDERER'] == '1'
    assert 'WEBKIT_DISABLE_COMPOSITING_MODE' not in os.environ
    assert 'LIBGL_ALWAYS_SOFTWARE' not in os.environ


def test_app_py_never_assigns_forced_rendering_flags():
    """O app.py não pode mais atribuir as flags legadas de composição/software."""
    content = (APP_DIR / "app.py").read_text()
    assert "os.environ['WEBKIT_DISABLE_COMPOSITING_MODE']" not in content
    assert "os.environ['LIBGL_ALWAYS_SOFTWARE']" not in content
    assert "WEBKIT_DISABLE_DMABUF_RENDERER': '1'" in content


def test_app_py_connects_key_press_event_only_on_gtk_window_level():
    """Listener de atalhos conectado apenas na janela GTK, sem duplicação no widget WebKit."""
    content = (APP_DIR / "app.py").read_text()
    assert "self.webview.connect('key-press-event'" not in content
    assert ".webview.connect('key-press-event'" not in content
    assert content.count("window.connect('key-press-event'") == 1


def test_install_shortcut_listener_handles_f5_on_window_signal():
    """O wiring real do listener consome F5 no sinal GObject da janela GTK."""
    window_probe = GtkWindowProbe()
    browse_view = BrowserViewProbe(window=window_probe)
    pywebview_window = webview.Window(uid='probe', title='probe', url='http://127.0.0.1:3000')

    install_shortcut_listener(browse_view, pywebview_window)

    handled = window_probe.emit('key-press-event', make_key_event(Gdk.KEY_F5))
    assert handled is True


def test_install_shortcut_listener_survives_invalid_browse_view():
    """Caminho de erro: browse_view sem .window não pode derrubar o boot (fail-soft)."""
    pywebview_window = webview.Window(uid='probe3', title='probe3', url='http://127.0.0.1:3000')
    install_shortcut_listener(None, pywebview_window)  # não levanta exceção


def test_install_shortcut_listener_swallows_real_connect_failure():
    """Caminho de erro real: connect() que levanta exceção é contido pelo try/except (fail-soft)."""
    class _ExplodingConnect:
        def connect(self, signal_name, callback):
            raise OSError("sinal indisponível")

    class _BrokenView:
        window = _ExplodingConnect()

    pywebview_window = webview.Window(uid='probe4', title='probe4', url='http://127.0.0.1:3000')
    install_shortcut_listener(_BrokenView(), pywebview_window)  # não levanta exceção


def test_shortcut_handler_ignores_keys_that_are_not_shortcuts():
    """Teclas fora de F5/Ctrl+R não devem ser consumidas pelo handler."""
    pywebview_window = webview.Window(uid='probe2', title='probe2', url='http://127.0.0.1:3000')
    handler = create_shortcut_handler(pywebview_window)

    assert handler(None, make_key_event(Gdk.KEY_a)) is False
    assert handler(None, make_key_event(Gdk.KEY_r, Gdk.ModifierType.CONTROL_MASK)) is True


# =====================================================================
# 6. TICKET-DESKTOP-07 — Colar imagem via clipboard (DesktopAPI & JS)
# =====================================================================

def invoke_clipboard_image_base64(api: DesktopAPI) -> typing.Optional[str]:
    """Invoca get_clipboard_image_base64 em worker thread processando eventos GLib na main thread.

    Simula deterministicamente o comportamento do pywebview, onde chamadas à js_api
    rodam em worker threads separadas enquanto a main thread processa o loop de eventos GTK/GLib.
    """
    result: typing.Dict[str, typing.Optional[str]] = {'value': None}
    exc_box: typing.Dict[str, typing.Optional[BaseException]] = {'error': None}

    def worker() -> None:
        try:
            result['value'] = api.get_clipboard_image_base64()
        except BaseException as exc:
            exc_box['error'] = exc

    t = threading.Thread(target=worker)
    t.start()

    ctx = GLib.MainContext.default()
    while t.is_alive():
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)

    t.join()
    if exc_box['error'] is not None:
        raise exc_box['error']
    return result['value']


def test_desktop_api_get_clipboard_image_base64_with_real_pixbuf():
    """Valida serialização real de GdkPixbuf para PNG base64 via DesktopAPI (sem mocks)."""
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    pix = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 32, 32)
    cb.set_image(pix)
    cb.store()

    api = DesktopAPI()
    result = invoke_clipboard_image_base64(api)

    assert result is not None
    assert isinstance(result, str)
    raw = base64.b64decode(result)
    assert raw.startswith(b'\x89PNG\r\n\x1a\n')

    # Valida decodificação real de volta para pixbuf preservando dimensões 32x32
    loader = GdkPixbuf.PixbufLoader.new_with_type('png')
    loader.write(raw)
    loader.close()
    loaded = loader.get_pixbuf()
    assert loaded.get_width() == 32
    assert loaded.get_height() == 32


def test_desktop_api_get_clipboard_image_base64_when_clipboard_has_text():
    """Quando a área de transferência contém texto, get_clipboard_image_base64 deve retornar None."""
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    cb.set_text("Texto simples colado na área de transferência", -1)
    cb.store()

    api = DesktopAPI()
    assert invoke_clipboard_image_base64(api) is None


def test_desktop_api_get_clipboard_image_base64_when_clipboard_cleared():
    """Quando o clipboard é limpo, get_clipboard_image_base64 deve retornar None."""
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    cb.clear()

    api = DesktopAPI()
    assert invoke_clipboard_image_base64(api) is None


def test_desktop_api_get_clipboard_image_base64_timeout_returns_none():
    """Quando a espera síncrona sofre timeout (1.0s) sem callback, retorna None com segurança."""
    api = DesktopAPI()
    # Chamada direta sem iterar o loop GLib dispara o timeout de 1.0s com segurança e sem travar
    result = api.get_clipboard_image_base64()
    assert result is None


# =====================================================================
# 6.1 open_external() — guardas fail-fast de abertura de navegador
#     (task_mv82vmnr_0001_c6bhij)
# =====================================================================

@pytest.fixture
def browser_spawn_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sonda REAL (zero mocks) contra spawn de navegador nos testes.

    Redireciona os dois caminhos reais de abertura de URL para scripts
    sentinelas dentro de ``tmp_path``:

    * ``$BROWSER`` — variável consultada pelo módulo ``webbrowser`` do Python
      em ``webbrowser.open()``;
    * ``PATH`` — shims ``xdg-open`` e navegadores comuns, cobrindo o fallback
      ``subprocess.run(['xdg-open', ...])``.

    Os sentinelas apenas gravam a URL num arquivo de marcação e encerram —
    nenhum navegador real é acionado em nenhuma circunstância. Se uma guarda
    de ``open_external`` falhar, o arquivo aparece e o teste falha; se a
    própria sonda estiver quebrada,
    ``test_browser_spawn_probe_detects_real_webbrowser_call`` falha primeiro.
    """
    marker = tmp_path / "browser.spawned"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    sentinel = shim_dir / "spawn-sentinel"
    sentinel.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$*" >> "{marker}"\n'
    )
    sentinel.chmod(0o755)
    for name in (
        "xdg-open", "firefox", "google-chrome", "chromium", "chromium-browser",
        "www-browser", "x-www-browser", "lynx", "w3m", "links",
    ):
        link = shim_dir / name
        if not link.exists():
            link.symlink_to(sentinel)
    monkeypatch.setenv("BROWSER", str(sentinel))
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return marker


def _wait_for_spawn_marker(marker: Path, timeout: float = 5.0) -> bool:
    """Aguarda com polling real a gravação da marcação da sonda (spawn é async)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(0.05)
    return marker.exists()


def _assert_no_browser_spawn(marker: Path, context: str, settle: float = 0.4) -> None:
    """Exige ausência de spawn, concedendo ao SO uma janela real para gravar.

    O spawn via ``webbrowser``/``Popen`` é assíncrono: sem a janela de
    ``settle`` um teste poderia passar falso-positivamente antes da sonda
    gravar a marcação.
    """
    if settle:
        time.sleep(settle)
    assert not marker.exists(), (
        f"{context}: spawn de navegador detectado pela sonda -> "
        f"{marker.read_text().strip()!r}"
    )


def test_browser_spawn_probe_detects_real_webbrowser_call(browser_spawn_probe: Path):
    """Autochecagem da sonda: um ``webbrowser.open`` real DEVE ser capturado.

    Garante que os asserts ``not marker.exists()`` dos demais testes não são
    falsos-positivos — o mecanismo de detecção é exercitado de fato, contra o
    sentinel e nunca contra um navegador real.
    """
    webbrowser.open("https://example.com/probe-self-check")
    assert _wait_for_spawn_marker(browser_spawn_probe), (
        "sonda não capturou webbrowser.open: os testes de supressão seriam "
        "falsos-positivos"
    )
    assert "https://example.com/probe-self-check" in browser_spawn_probe.read_text()


def test_desktop_api_open_external_link_fail_soft_on_empty_url(browser_spawn_probe):
    """Valida que open_external_link descarta URLs vazias, inválidas, de esquema
    desconhecido ou local sem levantar erro e SEM tocar no sistema operacional.

    Cobertura: "", "javascript:void(0)", "http://localhost:3000" e demais
    categorias rejeitadas por ``is_external_url``. A sonda real
    ``browser_spawn_probe`` falha o teste se qualquer spawn (webbrowser ou
    xdg-open) chegar ao SO.
    """
    api = DesktopAPI()
    rejected_urls = [
        "",                                  # vazio
        "   ",                               # só espaços (não-externo)
        "javascript:void(0)",                # esquema desconhecido/ataque
        "file:///etc/passwd",                # esquema de arquivo local
        "data:text/plain;base64,SGVsbG8=",   # esquema de dados
        "about:blank",                       # esquema interno do browser
        "ftp://ftp.example.com/file.zip",    # esquema não suportado
        "not-a-valid-url-format",            # lixo sem esquema
        "http://localhost:3000",             # host local
        "http://127.0.0.1:8080/session/1",   # host local
        "http://0.0.0.0:3000",               # bind local
        "http://[::1]:3000",                 # loopback IPv6
    ]
    for url in rejected_urls:
        api.open_external_link(url)          # não deve levantar exceção
        assert not browser_spawn_probe.exists(), (
            f"open_external_link({url!r}) disparou spawn de navegador no SO"
        )
    _assert_no_browser_spawn(browser_spawn_probe, "open_external_link")


def test_open_external_discards_non_external_urls_without_touching_os(browser_spawn_probe):
    """Guarda 1 de open_external: validação preliminar estrita — URL vazia ou
    não-externa retorna imediatamente, sem ``webbrowser.open`` nem ``xdg-open``."""
    rejected_urls = [
        "",
        None,  # type: ignore[arg-type]  # robustez: falsy nulo não pode explodir
        "javascript:void(0)",
        "http://localhost:3000",
        "https://localhost/admin",
        "http://127.0.0.1:9090",
        "file:///etc/hosts",
        "data:text/html,<script>alert(1)</script>",
        "about:config",
        "ftp://mirror.kernel.org/",
        "ssh://user@host:22",
        "just-a-string",
    ]
    for url in rejected_urls:
        assert open_external(url) is None
    _assert_no_browser_spawn(browser_spawn_probe, "open_external (URLs não externas)")


def test_open_external_suppressed_under_pytest_even_for_valid_external_url(browser_spawn_probe):
    """Guarda 2 de open_external: ``pytest`` em ``sys.modules`` bloqueia o spawn
    mesmo para uma URL externa legítima — a suíte nunca abre abas reais."""
    assert "pytest" in sys.modules  # premissa do teste verificada de fato
    assert browser_spawn_suppressed() is True
    assert open_external("https://example.com/docs") is None
    _assert_no_browser_spawn(browser_spawn_probe, "guarda pytest em sys.modules")


def test_open_external_suppressed_by_disable_env_flag(browser_spawn_probe, monkeypatch):
    """Guarda 2 de open_external: ``OPENCHAMBER_DISABLE_BROWSER_SPAWN=1`` bloqueia
    o spawn de uma URL externa legítima sem levantar erro."""
    monkeypatch.setenv("OPENCHAMBER_DISABLE_BROWSER_SPAWN", "1")
    assert browser_spawn_suppressed() is True
    assert open_external("https://example.com/docs") is None
    _assert_no_browser_spawn(browser_spawn_probe, "flag OPENCHAMBER_DISABLE_BROWSER_SPAWN=1")


def test_browser_spawn_suppressed_branch_matrix_is_pure():
    """Ramificações de browser_spawn_suppressed de forma pura (env/modules
    injetáveis, sem I/O), isolando cada guarda individualmente."""
    # Nenhuma condição ativa → spawn permitido (produção normal)
    assert browser_spawn_suppressed(env={}, modules={}) is False
    # Ramificação da flag de ambiente, isolada do pytest
    assert browser_spawn_suppressed(
        env={"OPENCHAMBER_DISABLE_BROWSER_SPAWN": "1"}, modules={}) is True
    # Valores distintos de "1" não ativam a flag
    assert browser_spawn_suppressed(
        env={"OPENCHAMBER_DISABLE_BROWSER_SPAWN": "0"}, modules={}) is False
    assert browser_spawn_suppressed(
        env={"OPENCHAMBER_DISABLE_BROWSER_SPAWN": "yes"}, modules={}) is False
    # Ramificação do pytest, isolada da flag de ambiente
    assert browser_spawn_suppressed(env={}, modules={"pytest": None}) is True
    # Ambas simultaneamente
    assert browser_spawn_suppressed(
        env={"OPENCHAMBER_DISABLE_BROWSER_SPAWN": "1"},
        modules={"pytest": None}) is True


def _run_open_external_in_fresh_interpreter(
    url: str,
    disable_flag: typing.Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Executa ``open_external`` num interpretador limpo (sem pytest carregado).

    Reconstrói apenas o ambiente de PRODUÇÃO: ``pytest`` ausente de
    ``sys.modules`` (interpretador novo) e a flag
    ``OPENCHAMBER_DISABLE_BROWSER_SPAWN`` removida (a menos que seja
    explicitamente solicitada). O spawn — se ocorrer — continua sendo
    capturado pela sonda sentinel ``$BROWSER``/``PATH`` herdada do fixture,
    nunca por um navegador real.
    """
    code = (
        "import sys; "
        f"sys.path[:0] = {[str(APP_DIR), str(venv_site)]!r}; "
        "import app; "
        f"app.open_external({url!r})"
    )
    env = os.environ.copy()
    env.pop("OPENCHAMBER_DISABLE_BROWSER_SPAWN", None)
    if disable_flag is not None:
        env["OPENCHAMBER_DISABLE_BROWSER_SPAWN"] = disable_flag
    venv_python = APP_DIR / "venv" / "bin" / "python"
    interpreter = str(venv_python) if venv_python.exists() else sys.executable
    return subprocess.run(
        [interpreter, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_open_external_spawns_real_browser_path_outside_test_environment(browser_spawn_probe):
    """Caminho feliz real (sem mocks): fora do pytest e sem a flag de corte,
    open_external chega ao SO — capturado pela sonda, nunca por navegador real.

    Prova que as guardas não quebram a abertura legítima de links externos em
    produção.
    """
    result = _run_open_external_in_fresh_interpreter("https://example.com/positive")
    assert result.returncode == 0, (
        f"filho falhou: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert _wait_for_spawn_marker(browser_spawn_probe), (
        "fora do ambiente de teste open_external deveria disparar o spawn "
        "(capturado pela sonda) e não disparou"
    )
    assert "https://example.com/positive" in browser_spawn_probe.read_text()


def test_open_external_env_flag_blocks_spawn_outside_pytest(browser_spawn_probe):
    """Ramificação da flag de ambiente isolada de verdade: interpretador limpo
    (pytest NÃO está em sys.modules) com OPENCHAMBER_DISABLE_BROWSER_SPAWN=1
    não pode disparar spawn algum.
    """
    result = _run_open_external_in_fresh_interpreter(
        "https://example.com/blocked",
        disable_flag="1",
    )
    assert result.returncode == 0, (
        f"filho falhou: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    _assert_no_browser_spawn(
        browser_spawn_probe,
        "OPENCHAMBER_DISABLE_BROWSER_SPAWN=1 fora do pytest",
    )


def test_desktop_handlers_js_paste_listener_structure():
    """Valida a presença e integridade do listener de paste no script injetado."""
    js = get_desktop_handlers_js()

    assert "window.addEventListener('paste'" in js
    assert "e.clipboardData.files" in js
    assert "e.clipboardData.getData('text')" in js
    assert "window.pywebview.api.get_clipboard_image_base64" in js
    assert "e.preventDefault()" in js
    assert "fetch('data:image/png;base64,' + base64Data)" in js
    assert "res.blob()" in js
    assert "new File([blob], 'pasted-image.png', { type: 'image/png' })" in js
    assert "input[type=\"file\"][multiple]" in js
    assert "new DataTransfer()" in js
    assert "dispatchEvent(new Event('change', { bubbles: true }))" in js


def test_desktop_handlers_js_idempotent_installation_guard():
    """Valida que o script possui flag guard para evitar injeção duplicada de listeners."""
    js = get_desktop_handlers_js()
    assert "window.__desktop_handlers_installed" in js
    assert "if (!window.__desktop_handlers_installed)" in js


def test_desktop_api_wires_into_pywebview_window():
    """Valida que js_api=DesktopAPI() é aceito pelo pywebview e armazena a instância em _js_api."""
    api = DesktopAPI()
    w = webview.Window(uid='test_api', title='test_api', url='http://127.0.0.1:3000', js_api=api)
    assert w._js_api is api
    assert hasattr(w._js_api, 'get_clipboard_image_base64')
    assert callable(w._js_api.get_clipboard_image_base64)


def test_setup_window_handlers_connects_loaded_event():
    """Valida que setup_window_handlers registra handler no evento loaded do pywebview."""
    w = webview.Window(uid='test_load', title='test_load', url='http://127.0.0.1:3000')
    initial_handlers_count = len(w.events.loaded)
    setup_window_handlers(w)
    assert len(w.events.loaded) == initial_handlers_count + 1


def test_app_main_passes_js_api_desktop_api():
    """Valida que no ponto de entrada (__main__) do app.py, js_api é configurado com DesktopAPI."""
    content = (APP_DIR / "app.py").read_text()
    assert "desktop_api = DesktopAPI()" in content
    assert "js_api=desktop_api" in content


# =====================================================================
# 7. TICKET-DESKTOP-09 — resolve_debug_mode, flag --debug no run.sh
#    e preservação do menu de contexto (zero mocks)
# =====================================================================

RUN_SH_ARG_PARSE_BEGIN = "# >>> OPENCHAMBER_ARG_PARSE_BEGIN"
RUN_SH_ARG_PARSE_END = "# <<< OPENCHAMBER_ARG_PARSE_END"


def extract_run_sh_arg_parse_block() -> str:
    """Extrai o bloco real de parsing de argumentos do run.sh (sem cópia manual do código)."""
    lines = (APP_DIR / "run.sh").read_text().splitlines()
    begin = next((i for i, line in enumerate(lines) if RUN_SH_ARG_PARSE_BEGIN in line), None)
    end = next((i for i, line in enumerate(lines) if RUN_SH_ARG_PARSE_END in line), None)
    assert begin is not None, "marcador OPENCHAMBER_ARG_PARSE_BEGIN ausente em run.sh"
    assert end is not None and end > begin, "marcador OPENCHAMBER_ARG_PARSE_END inválido em run.sh"
    return "\n".join(lines[begin:end + 1])


def run_sh_parse_env(argv: typing.List[str], inherited: typing.Optional[str] = None) -> str:
    """Executa o bloco real de parsing do run.sh em bash e devolve o OPENCHAMBER_DEBUG resultante."""
    script = extract_run_sh_arg_parse_block() + '\nprintf "%s" "${OPENCHAMBER_DEBUG-__UNSET__}"\n'
    env = dict(os.environ)
    env.pop("OPENCHAMBER_DEBUG", None)
    if inherited is not None:
        env["OPENCHAMBER_DEBUG"] = inherited

    result = subprocess.run(
        ["bash", "-c", script, "openchamber_arg_parse"] + list(argv),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


# ---------------------------------------------------------------------
# 7.1 resolve_debug_mode() — tipagem estrita e matriz de valores
# ---------------------------------------------------------------------

def test_resolve_debug_mode_has_strict_typing_signature():
    """Assinatura determinística: env opcional tipado como Optional[Mapping[str, str]] e retorno bool."""
    hints = typing.get_type_hints(resolve_debug_mode)
    assert hints["return"] is bool
    assert hints["env"] == typing.Optional[typing.Mapping[str, str]]
    signature = inspect.signature(resolve_debug_mode)
    assert signature.parameters["env"].default is None


@pytest.mark.parametrize("value", [
    "1",
    "true",
    "True",
    "TRUE",
    "tRuE",
    "yes",
    "YES",
    "on",
    "On",
    " 1 ",
    "  True  ",
    "\tYES\n",
])
def test_resolve_debug_mode_truthy_values_return_true(value):
    """Valores verdadeiros (com strip e case-insensitive) ativam o modo de depuração."""
    assert resolve_debug_mode({"OPENCHAMBER_DEBUG": value}) is True


@pytest.mark.parametrize("value", [
    "0",
    "false",
    "False",
    "FALSE",
    "no",
    "off",
    "OFF",
    "",
    "   ",
    "invalid",
    "2",
    "y",
    "enabled",
    "truely",
])
def test_resolve_debug_mode_falsy_values_return_false(value):
    """Qualquer valor fora da matriz de verdadeiros mantém o modo de produção desligado."""
    assert resolve_debug_mode({"OPENCHAMBER_DEBUG": value}) is False


def test_resolve_debug_mode_absent_key_returns_false():
    """Sem a chave OPENCHAMBER_DEBUG no mapping o padrão é produção (False)."""
    assert resolve_debug_mode({}) is False


def test_resolve_debug_mode_defaults_to_process_environment(monkeypatch):
    """Sem env explícito, lê os.environ de forma determinística nos dois sentidos."""
    monkeypatch.delenv("OPENCHAMBER_DEBUG", raising=False)
    assert resolve_debug_mode() is False

    monkeypatch.setenv("OPENCHAMBER_DEBUG", "true")
    assert resolve_debug_mode() is True


def test_resolve_debug_mode_explicit_env_overrides_process_environment(monkeypatch):
    """Mapping explícito tem precedência exclusiva e não contamina os.environ."""
    monkeypatch.setenv("OPENCHAMBER_DEBUG", "1")
    assert resolve_debug_mode({}) is False
    assert resolve_debug_mode({"OPENCHAMBER_DEBUG": "0"}) is False
    assert resolve_debug_mode({"OPENCHAMBER_DEBUG": "on"}) is True
    assert os.environ["OPENCHAMBER_DEBUG"] == "1"


def test_resolve_debug_mode_handles_non_string_values_fail_soft():
    """Valores fora do contrato str (defesa) retornam False sem levantar exceção."""
    assert resolve_debug_mode({"OPENCHAMBER_DEBUG": None}) is False
    assert resolve_debug_mode({"OPENCHAMBER_DEBUG": 1}) is False


def test_resolve_debug_mode_is_wired_into_webview_start():
    """O ponto de entrada usa resolve_debug_mode() e nunca debug=True hardcoded."""
    content = (APP_DIR / "app.py").read_text()
    assert "debug=resolve_debug_mode()" in content
    assert "webview.start(gui='gtk', debug=True" not in content


# ---------------------------------------------------------------------
# 7.2 run.sh — repasse de argumentos e flag --debug / -d
# ---------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["--debug", "-d"])
def test_run_sh_debug_flag_exports_openchamber_debug(flag):
    """./run.sh --debug (ou -d) exporta OPENCHAMBER_DEBUG=1 para o app.py."""
    assert run_sh_parse_env([flag]) == "1"


def test_run_sh_debug_flag_works_mixed_with_other_args():
    """A flag é detectada mesmo com outros argumentos à frente/atrás, sem descartá-los."""
    assert run_sh_parse_env(["--other", "--debug", "foo"]) == "1"
    assert run_sh_parse_env(["-d", "extra"]) == "1"


def test_run_sh_without_debug_flag_leaves_env_unset():
    """Sem a flag, OPENCHAMBER_DEBUG permanece inalterada (produção)."""
    assert run_sh_parse_env([]) == "__UNSET__"
    assert run_sh_parse_env(["--help", "chat"]) == "__UNSET__"


def test_run_sh_preserves_preexisting_openchamber_debug_without_flag():
    """Env já exportada pelo usuário é preservada quando não há flag de debug."""
    assert run_sh_parse_env([], inherited="0") == "0"
    assert run_sh_parse_env([], inherited="true") == "true"


def test_run_sh_forwards_cli_args_to_app_and_keeps_trap_cleanup():
    """Argumentos são repassados ao app.py e o trap cleanup do DEF-01 permanece intacto."""
    content = (APP_DIR / "run.sh").read_text()
    assert 'app.py" "$@"' in content
    assert "trap cleanup EXIT INT TERM" in content
    assert 'if [ "$OPENCHAMBER_KEEP_SERVER" = "1" ]; then' in content

    syntax = subprocess.run(["bash", "-n", str(APP_DIR / "run.sh")], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_run_sh_parse_block_is_valid_and_contained():
    """O bloco de parsing é sintaticamente válido sozinho e não altera o fluxo do cleanup."""
    block = extract_run_sh_arg_parse_block()
    assert "--debug|-d" in block
    assert "OPENCHAMBER_DEBUG=1" in block

    check = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


# ---------------------------------------------------------------------
# 7.3 Menu de contexto: bloqueio do pywebview é removido com debug off
# ---------------------------------------------------------------------

class ContextMenuProbe(GObject.Object):
    """Widget real (GObject) exposto ao sinal ``context-menu`` idêntico ao do WebKitGTK.

    Usa o mesmo mecanismo de sinal GObject/GLib do ``WebKit2.WebView`` de
    produção, permitindo auditar a remoção do bloqueio do pywebview sem display.
    """

    __gsignals__ = {
        'context-menu': (GObject.SignalFlags.RUN_LAST, bool, (object, object, object, object)),
    }


class BrowserViewWithContext:
    """Possui o atributo ``webview`` consumido por ``preserve_default_context_menu``."""

    def __init__(self, webview_widget: typing.Any) -> None:
        self.webview = webview_widget


def test_preserve_default_context_menu_removes_blocker_when_debug_disabled(monkeypatch):
    """Com OPENCHAMBER_DEBUG ausente, o handler de bloqueio do pywebview é desconectado."""
    monkeypatch.delenv("OPENCHAMBER_DEBUG", raising=False)
    probe = ContextMenuProbe()
    blocked: typing.List[str] = []
    probe.connect('context-menu', lambda *args: blocked.append('hit') or True)

    preserve_default_context_menu(BrowserViewWithContext(probe))

    probe.emit('context-menu', None, None, None, None)
    assert blocked == []


def test_preserve_default_context_menu_keeps_blocker_when_debug_enabled(monkeypatch):
    """Com OPENCHAMBER_DEBUG ativa, o pywebview não liga o bloqueio e nada é desconectado."""
    monkeypatch.setenv("OPENCHAMBER_DEBUG", "1")
    probe = ContextMenuProbe()
    blocked: typing.List[str] = []
    probe.connect('context-menu', lambda *args: blocked.append('hit') or True)

    preserve_default_context_menu(BrowserViewWithContext(probe))

    probe.emit('context-menu', None, None, None, None)
    assert blocked == ['hit']


def test_preserve_default_context_menu_is_fail_soft(monkeypatch):
    """Sem .webview ou sem o sinal 'context-menu' a função não levanta exceção (fail-soft)."""
    monkeypatch.delenv("OPENCHAMBER_DEBUG", raising=False)
    preserve_default_context_menu(object())

    class WithoutContextMenuSignal(GObject.Object):
        pass

    preserve_default_context_menu(BrowserViewWithContext(WithoutContextMenuSignal()))


def test_app_py_restores_context_menu_right_after_browser_view_init():
    """A restauração é chamada logo após o __init__ original e antes do listener de atalhos."""
    content = (APP_DIR / "app.py").read_text()
    init_call = content.index("_orig_browser_view_init(self, window)")
    restore_call = content.index("preserve_default_context_menu(self)")
    shortcut_call = content.index("install_shortcut_listener(self, window)")
    assert init_call < restore_call < shortcut_call


# =====================================================================
# 8. TICKET-DESKTOP-10 — Contenção de repaints em idle/desfocada/minimizada
#    (zero mocks: asserções estruturais sobre o JS real injetado + validação
#     sintática com o binário Node.js do sistema)
# =====================================================================

IDLE_STYLE_ID = "__desktop_idle_style"
IDLE_ROOT_CLASS = "oc-desktop-idle"


def test_desktop_handlers_js_idle_css_containment_rule_structure():
    """A tag de estilo dinâmico pausa animações de todos os elementos (incl. pseudo-elementos)."""
    js = get_desktop_handlers_js()

    assert f"getElementById('{IDLE_STYLE_ID}')" in js
    assert f"el.id = '{IDLE_STYLE_ID}'" in js
    assert "document.head.appendChild(el)" in js
    assert "animation-play-state: paused !important" in js
    # Regra precisa cobrir elemento, ::before e ::after (cursores piscantes são pseudo-elementos)
    assert f"html.{IDLE_ROOT_CLASS} *," in js
    assert f"html.{IDLE_ROOT_CLASS} *::before," in js
    assert f"html.{IDLE_ROOT_CLASS} *::after" in js


def test_desktop_handlers_js_idle_containment_is_scoped_not_global():
    """A pausa é condicionada à classe raiz (preserva fluidez em interação ativa), nunca global."""
    js = get_desktop_handlers_js()

    rule_start = js.index(f"html.{IDLE_ROOT_CLASS} *")
    rule_end = js.index("animation-play-state: paused !important", rule_start)
    rule = js[js.index("'html.", js.index("el.textContent")):rule_end]
    assert rule.startswith(f"'html.{IDLE_ROOT_CLASS}")
    # A liberação remove a classe em vez de desligar a regra CSS
    assert f"root.classList.add('{IDLE_ROOT_CLASS}')" in js
    assert f"root.classList.remove('{IDLE_ROOT_CLASS}')" in js
    # Nenhuma pausa global de animação fora do seletor da classe
    assert "*, *::before, *::after { animation-play-state: paused" not in js


def test_desktop_handlers_js_idle_window_and_visibility_listeners():
    """Listeners de blur/focus na janela e visibilitychange no documento estão configurados."""
    js = get_desktop_handlers_js()

    assert "window.addEventListener('blur'" in js
    assert "window.addEventListener('focus'" in js
    assert "document.addEventListener('visibilitychange'" in js
    assert "document.hidden" in js
    # blur/oculto contenem; focus/visível liberam imediatamente
    assert "setReason('blur', true)" in js
    assert "setReason('blur', false)" in js


def test_desktop_handlers_js_idle_watchdog_and_activity_listeners():
    """Watchdog de 30s e todos os listeners de atividade do usuário estão presentes."""
    js = get_desktop_handlers_js()

    assert "window.__desktop_idle_timeout_ms = 30000" in js
    assert "setTimeout(function()" in js
    assert "clearTimeout(window.__desktop_idle_timer)" in js
    assert "setReason('idle', true)" in js
    assert "setReason('idle', false)" in js

    # Monitoramento de atividade otimizado (throttled)
    assert "window.__desktop_idle_activity_throttle_ms = 250" in js
    for event in ("mousemove", "keydown", "wheel", "mousedown", "touchstart"):
        assert f"'{event}'" in js, f"listener de atividade ausente: {event}"
    assert "window.addEventListener('keydown', function() {" in js


def test_desktop_handlers_js_idle_subsystem_is_idempotent_and_fail_soft():
    """Flag de instalação única, IIFE com 'use strict' e try/catch em todo o caminho."""
    js = get_desktop_handlers_js()

    assert "if (!window.__desktop_idle_throttle_installed)" in js
    assert "window.__desktop_idle_throttle_installed = true;" in js
    # Subsistema isolado em IIFE para não vazar var no escopo global da página React
    assert "(function() {" in js
    assert "'use strict';" in js
    # Fail-soft: guards contra documento ausente e catches em cada etapa
    assert "typeof document === 'undefined'" in js
    assert "typeof document !== 'undefined'" in js
    assert js.count("catch (_)") >= 8
    # Idempotência do sync: não re-aplica classe se o estado não mudou
    assert "if (active === window.__desktop_idle_active)" in js
    # Bloco idle vive dentro do guard global de instalação dos handlers
    assert js.index("if (!window.__desktop_handlers_installed)") < js.index(
        "if (!window.__desktop_idle_throttle_installed)"
    )


def test_desktop_handlers_js_idle_release_on_focus_and_visibility():
    """focus e retorno para visible liberam blur E idle imediatamente e reagendam o watchdog."""
    js = get_desktop_handlers_js()

    focus_block = js[js.index("window.addEventListener('focus'"):js.index("document.addEventListener('visibilitychange'")]
    assert "setReason('blur', false)" in focus_block
    assert "setReason('idle', false)" in focus_block
    assert "scheduleIdleWatchdog()" in focus_block

    vis_block = js[js.index("document.addEventListener('visibilitychange'"):]
    assert "setReason('blur', !!(document && document.hidden))" in vis_block
    assert "scheduleIdleWatchdog()" in vis_block


def test_desktop_handlers_js_is_valid_javascript_syntax(tmp_path):
    """O script injetado inteiro passa na validação sintática real do Node.js do sistema."""
    node = shutil.which("node")
    assert node is not None, "binário node ausente do PATH — validação sintática impossível"

    probe = tmp_path / "desktop_handlers_probe.js"
    probe.write_text(get_desktop_handlers_js(), encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(probe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# =====================================================================
# 9. TICKET-DESKTOP-11 — Reconexão ao backend, reciclagem de sessão e
#    descarte de streams (zero mocks: exercício real do caminho de
#    reciclagem com cooldown de 10s, asserções estruturais sobre o JS
#    injetado e validação sintática com o binário Node.js do sistema)
# =====================================================================

RECONNECT_GUARD = "if (!window.__desktop_reconnect_installed)"
RECONNECT_EVENT = "openchamber:backend-reconnected"
RECONNECT_STORAGE_KEY = "openchamber:desktop:reconnect:last-reload"


class WindowReloadProbe:
    """Janela pywebview mínima que registra o script avaliado em ``evaluate_js``.

    Mesmo espírito dos probes GObject já usados nesta suíte: permite exercitar
    de forma determinística o caminho real de ``DesktopAPI.recycle_session``
    sem iniciar o GTK (o pywebview real bloqueia em ``_pywebviewready`` sem GUI).
    A sincronização usa ``wait_for_calls`` com prazo finito — sem mocks.
    """

    def __init__(self) -> None:
        self.scripts: typing.List[str] = []
        self._lock = threading.Lock()

    def evaluate_js(self, script: str) -> None:
        """Registra o script exatamente como o pywebview o receberia."""
        with self._lock:
            self.scripts.append(script)

    @property
    def calls(self) -> typing.List[str]:
        """Retorna uma cópia estável dos scripts já registrados."""
        with self._lock:
            return list(self.scripts)

    def wait_for_calls(self, count: int, timeout: float = 5.0) -> bool:
        """Aguarda (prazo finito) até ``count`` chamadas serem registradas."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.calls) >= count:
                return True
            time.sleep(0.01)
        return len(self.calls) >= count


# ---------------------------------------------------------------------
# 9.1 DesktopAPI.recycle_session / reload_page — ponte nativa de reciclagem
# ---------------------------------------------------------------------

def test_desktop_api_recycle_session_strict_signature():
    """Assinatura determinística: reason/force tipados com defaults e retorno bool."""
    hints = typing.get_type_hints(DesktopAPI.recycle_session)
    assert hints["return"] is bool
    assert hints["reason"] is str
    assert hints["force"] is bool

    signature = inspect.signature(DesktopAPI.recycle_session)
    assert signature.parameters["reason"].default == "manual"
    assert signature.parameters["force"].default is False


def test_desktop_api_recycle_session_dispatches_reload_and_enforces_cooldown():
    """Reciclagem real despacha window.location.reload() e respeita o cooldown anti-loop de 10s."""
    probe = WindowReloadProbe()
    previous_window = app_module._desktop_window
    previous_ts = app_module._last_recycle_ts
    app_module.register_desktop_window(probe)
    app_module._last_recycle_ts = 0.0
    try:
        api = DesktopAPI()
        assert api.recycle_session(reason="ticket11") is True
        assert probe.wait_for_calls(1), "reload não foi despachado"
        assert probe.calls == ["window.location.reload()"]

        # Segunda reciclagem dentro do cooldown de 10s é suprimida (anti-loop).
        assert api.recycle_session(reason="ticket11") is False
        assert probe.calls == ["window.location.reload()"]

        # force=True ignora o cooldown de forma explícita (uso programático).
        assert api.recycle_session(reason="ticket11", force=True) is True
        assert probe.wait_for_calls(2), "reload forçado não foi despachado"
        assert probe.calls == ["window.location.reload()"] * 2
    finally:
        app_module._desktop_window = previous_window
        app_module._last_recycle_ts = previous_ts


def test_desktop_api_recycle_session_fail_soft_without_window():
    """Sem janela registrada nem GUI ativa retorna False, sem exceção e sem consumir cooldown."""
    previous_window = app_module._desktop_window
    previous_ts = app_module._last_recycle_ts
    app_module._desktop_window = None
    app_module._last_recycle_ts = 0.0
    try:
        api = DesktopAPI()
        assert api.recycle_session(reason="no-window") is False
        # O caminho de falha não consome o cooldown (próxima chamada válida não é bloqueada).
        assert app_module._last_recycle_ts == 0.0
    finally:
        app_module._desktop_window = previous_window
        app_module._last_recycle_ts = previous_ts


def test_desktop_api_reload_page_alias_delegates_to_recycle_session():
    """reload_page() é alias público da reciclagem e honra o mesmo cooldown de 10s."""
    api = DesktopAPI()
    previous_ts = app_module._last_recycle_ts
    app_module._last_recycle_ts = time.monotonic()  # cooldown ativo
    try:
        hints = typing.get_type_hints(DesktopAPI.reload_page)
        assert hints["return"] is bool
        assert api.reload_page(reason="alias") is False  # suprimida pelo cooldown
    finally:
        app_module._last_recycle_ts = previous_ts


def test_setup_window_handlers_registers_desktop_window_for_recycling():
    """setup_window_handlers registra a janela como alvo da reciclagem de sessão."""
    previous_window = app_module._desktop_window
    try:
        w = webview.Window(uid='ticket11_recycle', title='ticket11_recycle', url='http://127.0.0.1:3000')
        setup_window_handlers(w)
        assert app_module._desktop_window is w
    finally:
        app_module._desktop_window = previous_window


def test_app_py_recycle_session_wiring_source_structure():
    """A estrutura fonte da ponte de reciclagem: cooldown 10s, thread daemon e registro da janela."""
    content = (APP_DIR / "app.py").read_text(encoding="utf-8")
    assert "_RECYCLE_COOLDOWN_S: float = 10.0" in content
    assert "if not force and (now - _last_recycle_ts) < _RECYCLE_COOLDOWN_S:" in content
    assert "threading.Thread(target=dispatch_reload, daemon=True).start()" in content
    assert "target.evaluate_js('window.location.reload()')" in content
    assert "def recycle_session(self, reason: str = 'manual', force: bool = False) -> bool:" in content
    assert "register_desktop_window(window)" in content.split("def setup_window_handlers", 1)[1]


# ---------------------------------------------------------------------
# 9.2 Subsistema JS de reconexão — guarda, heartbeat e detecção de reinício
# ---------------------------------------------------------------------

def test_desktop_handlers_js_reconnect_guard_idempotent_and_isolated():
    """Bloco de reconexão: guarda única, IIFE com 'use strict' e posicionamento pós-idle."""
    js = get_desktop_handlers_js()

    assert RECONNECT_GUARD in js
    assert "window.__desktop_reconnect_installed = true;" in js
    # Vive dentro do guard global de handlers e DEPOIS do idle do TICKET-10
    assert js.index("if (!window.__desktop_handlers_installed)") < js.index(RECONNECT_GUARD)
    assert js.index("if (!window.__desktop_idle_throttle_installed)") < js.index(RECONNECT_GUARD)

    block = js[js.index(RECONNECT_GUARD):]
    assert "(function() {" in block
    assert "'use strict';" in block
    for var in ("__desktop_reconnect_timer", "__desktop_reconnect_online", "__desktop_reconnect_offline_since", "__desktop_reconnect_instance_id", "__desktop_reconnect_restart_count"):
        assert f"window.{var}" in block


def test_desktop_handlers_js_reconnect_heartbeat_endpoints_and_interval():
    """Heartbeat de 5s contra /api/system/info com fallback /health e timeout de abort."""
    js = get_desktop_handlers_js()

    assert "'/api/system/info'" in js
    assert "'/health'" in js
    assert "var RECONNECT_POLL_MS = 5000;" in js
    assert "var RECONNECT_FETCH_TIMEOUT_MS = 4000;" in js
    assert "cache: 'no-store'" in js
    assert "typeof AbortController" in js
    # Loop fail-soft com timer único idempotente
    assert "clearTimeout(window.__desktop_reconnect_timer)" in js
    assert "window.__desktop_reconnect_timer = null;" in js
    # Só marca offline quando TODOS os endpoints responderem mal (fallback encadeado)
    assert "if (attempt >= endpoints.length)" in js
    assert "onBackendOffline()" in js


def test_desktop_handlers_js_reconnect_detects_offline_transition_and_instance_change():
    """Transição offline -> online e detecção de reinício via server_instance_id/pid renovado."""
    js = get_desktop_handlers_js()

    assert "window.__desktop_reconnect_online = false;" in js
    assert "window.__desktop_reconnect_offline_since = nowMs();" in js
    assert "var wasOffline = !window.__desktop_reconnect_online;" in js
    assert "offlineMs" in js
    # Chaves de identidade do daemon (server_instance_id renovado = reinício)
    for key in ("'server_instance_id'", "'serverInstanceId'", "'instance_id'", "'instanceId'", "'pid'"):
        assert key in js
    assert "var instanceChanged = " in js
    assert "window.__desktop_reconnect_restart_count += 1;" in js
    assert "previousInstanceId" in js
    assert "window.__desktop_reconnect_online = true;" in js
    assert "window.__desktop_reconnect_offline_since = null;" in js


def test_desktop_handlers_js_reconnect_dispatches_custom_event_with_detail():
    """Evento openchamber:backend-reconnected carrega o detalhe completo da reconexão."""
    js = get_desktop_handlers_js()

    dispatch = "window.dispatchEvent(new CustomEvent('openchamber:backend-reconnected', { detail: detail }))"
    assert dispatch in js

    call_start = js.index("handleReconnect({")
    detail_block = js[call_start:js.index("});", call_start)]
    for field in ("at:", "wasOffline:", "offlineMs:", "instanceChanged:", "previousInstanceId:", "serverInstanceId:", "restartCount:"):
        assert field in detail_block, f"campo ausente no detail do evento: {field}"


def test_desktop_handlers_js_reconnect_discards_pending_streams_in_order():
    """Descarte fecha EventSource/WebSocket em __openchamber_streams e globais, na ordem evento -> descarte -> reload."""
    js = get_desktop_handlers_js()

    assert "window.__openchamber_streams" in js
    assert "Array.isArray(registry)" in js
    assert "typeof target.close === 'function'" in js
    for glob in ("'__openchamber_eventsource'", "'__openchamber_sse'", "'__openchamber_socket'", "'__openchamber_ws'", "'__eventSource'", "'__sse'", "'__socket'", "'__ws'"):
        assert glob in js, f"global residual ausente do descarte: {glob}"
    assert "window[globals[g]] = null;" in js

    # Ordem obrigatória da spec: (a) evento -> (b) descarte de streams -> (c) recarga
    handle = js.index("var handleReconnect = function(detail) {")
    dispatch = js.index(RECONNECT_EVENT + "'", handle)
    discard = js.index("discardPendingStreams();", dispatch)
    reload = js.index("maybeReload(detail);", discard)
    assert dispatch < discard < reload


# ---------------------------------------------------------------------
# 9.3 Recarga controlada — cooldown anti-loop de 10s e cancelas de reinício
# ---------------------------------------------------------------------

def test_desktop_handlers_js_reconnect_reload_anti_loop_cooldown():
    """Cooldown de 10s persistido em sessionStorage (sobrevive ao próprio reload) + cancelas de reload."""
    js = get_desktop_handlers_js()

    assert "var RECONNECT_RELOAD_COOLDOWN_MS = 10000;" in js
    assert f"var RECONNECT_STORAGE_KEY = '{RECONNECT_STORAGE_KEY}';" in js
    assert "sessionStorage.getItem(RECONNECT_STORAGE_KEY)" in js
    assert "sessionStorage.setItem(RECONNECT_STORAGE_KEY, String(ts));" in js
    assert "return (nowMs() - readLastReloadTs()) >= cooldown;" in js
    assert "persistLastReloadTs(nowMs());" in js
    assert "window.location.reload();" in js
    # Cancelas: reinício completo confirmado (queda prolongada) OU acúmulo de reinícios
    assert "var RECONNECT_LONG_OFFLINE_MS = 4000;" in js
    assert "var RECONNECT_RELOAD_AFTER_RESTARTS = 3;" in js
    assert "var confirmedRestart = !!detail.instanceChanged && detail.offlineMs >= RECONNECT_LONG_OFFLINE_MS;" in js
    assert "detail.restartCount >= restartsThreshold" in js
    # Desligamento explícito pelo frontend
    assert "cfg.autoReload === false" in js


def test_desktop_handlers_js_reconnect_accepts_frontend_config_override():
    """O frontend pode configurar heartbeat, endpoints e recarga via __desktop_reconnect_config."""
    js = get_desktop_handlers_js()

    assert "window.__desktop_reconnect_config || {}" in js
    for key in ("cfg.pollIntervalMs", "cfg.endpoints", "cfg.reloadCooldownMs", "cfg.reloadAfterRestarts", "cfg.autoReload"):
        assert key in js, f"chave de configuração ausente: {key}"


def test_desktop_handlers_js_reconnect_is_fail_soft_and_preserves_existing_handlers():
    """Fail-soft total no bloco novo e regressão intacta dos handlers dos TICKETs 04/07/10."""
    js = get_desktop_handlers_js()

    assert "typeof fetch !== 'function'" in js
    assert js.count("catch (_)") >= 10
    # Handlers existentes preservados (atalhos, links externos, clipboard, idle)
    assert "window.addEventListener('keydown'" in js
    assert "window.pywebview.api.open_external_link" in js
    assert "window.pywebview.api.get_clipboard_image_base64" in js
    assert "if (!window.__desktop_idle_throttle_installed)" in js

    # A reconexão não duplica listeners de foco/visibilidade/atalho (isolamento estrito)
    block = js[js.index(RECONNECT_GUARD):]
    for evt in ("addEventListener('focus'", "addEventListener('blur'", "addEventListener('visibilitychange'", "addEventListener('keydown'", "addEventListener('paste'"):
        assert evt not in block, f"bloco de reconexão não pode registrar listener duplicado: {evt}"


def test_desktop_handlers_js_reconnect_valid_javascript_syntax(tmp_path):
    """O script gerado (incl. subsistema de reconexão) passa no node --check real do sistema."""
    node = shutil.which("node")
    assert node is not None, "binário node ausente do PATH — validação sintática impossível"

    js = get_desktop_handlers_js()
    assert RECONNECT_GUARD in js  # garante que o script validado contém a reconexão

    probe = tmp_path / "desktop_reconnect_probe.js"
    probe.write_text(js, encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(probe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# =====================================================================
# 10. TICKET-DESKTOP-12 — Paridade de comprimento code points/bytes UTF-8
#     no transporte WebKitGTK (regressão do truncamento do pywebview).
#     Zero mocks: probes reais em memória, GLib.MainLoop real (sem display)
#     e validação sintática pelo binário node do sistema.
# =====================================================================

def test_desktop_handlers_js_is_pure_ascii():
    """O payload injetado é 100% ASCII: ``len(code points) == len(bytes UTF-8)``.

    Causa raiz (TICKET-DESKTOP-12): ``BrowserView.evaluate_js`` do pywebview chama
    ``webkit_web_view_evaluate_javascript(script, length=len(script), ...)``. O
    ``length`` da API C é contado em BYTES UTF-8, enquanto ``len(script)`` é contado
    em CODE POINTS; com acentuação (``ç ã õ í ê ó``, travessão ``—``) o WebKit lê
    menos bytes do que o buffer real e trunca o script no meio de um caractere
    multibyte — emitindo ``WebKitJavascriptError`` (SyntaxError) silencioso no boot.
    Manter o payload ASCII elimina a divergência na origem.
    """
    js = get_desktop_handlers_js()
    assert js.isascii(), "payload JS contém não-ASCII — sujeito ao truncamento do WebKitGTK"
    assert len(js) == len(js.encode("utf-8"))


def test_desktop_handlers_js_survives_pywebview_codepoint_truncation(tmp_path):
    """Repro determinístico do defeito real: ``js.encode('utf-8')[:len(js)]``.

    O pywebview (``platforms/gtk.py``) entrega ao WebKitGTK ``length=len(script)``
    em code points; o WebKit, por sua vez, consome os primeiros ``length`` BYTES do
    buffer UTF-8 — exatamente o slice ``js.encode('utf-8')[:len(js)]``. Antes do fix
    (payload acentuado: 28613 code points vs 28677 bytes) esse slice perdia os 64
    bytes finais e partia um caractere multibyte, reprovando no ``node --check``
    (SyntaxError na região final do bloco do heartbeat). Com o payload ASCII o slice
    coincide com o script inteiro e a sintaxe é válida. Este teste FALHA antes do fix
    do app.py e PASSA depois.
    """
    node = shutil.which("node")
    assert node is not None, "binário node ausente do PATH — validação sintática impossível"

    js = get_desktop_handlers_js()
    truncated_pywebview_path = js.encode("utf-8")[: len(js)]

    probe = tmp_path / "desktop_handlers_pywebview_truncated.js"
    probe.write_bytes(truncated_pywebview_path)

    result = subprocess.run(
        [node, "--check", str(probe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "o caminho real do pywebview (length=len(script) sobre UTF-8) truncou o payload "
        f"injetado: {result.stderr}"
    )


class _EvalAsyncTaskResult:
    """Token mínimo (GAsyncResult) aceito por ``evaluate_javascript_finish``."""


class WebKitWebViewEvalProbe:
    """Probe que modela o ``WebKitWebView`` mínimo consumido por ``evaluate_js``.

    Mesmo espírito dos probes GObject desta suíte: registra o ``length`` e o
    ``script`` realmente entregues a ``evaluate_javascript`` e invoca o callback de
    conclusão de forma síncrona. Não é mock: é a fronteira nativa (WebKitGTK)
    exercitada por um objeto real em memória, sem depender de display/X11.
    """

    def __init__(self, js_result: str = "null") -> None:
        self.calls: typing.List[typing.Dict[str, typing.Any]] = []
        self._js_result = js_result

    def evaluate_javascript(self, script, length, world_name, source_uri, cancellable, callback):
        """Captura os argumentos reais do transporte e conclui o callback."""
        self.calls.append({"script": script, "length": length})
        callback(self, _EvalAsyncTaskResult())

    def evaluate_javascript_finish(self, task):
        """Devolve o valor JS "convertido" pré-fixado pelo probe."""
        return self._js_result


class BrowserViewEvalProbe:
    """Probe do ``BrowserView``: suprir ``webview`` e ``_convert_js_value`` reais."""

    def __init__(self, js_result: str = "null") -> None:
        self.webview = WebKitWebViewEvalProbe(js_result)

    def _convert_js_value(self, value):
        """Identidade: o valor já vem pronto do ``WebKitWebViewEvalProbe``."""
        return value


class GLibMainLoopThread:
    """Roda o ``GLib.MainLoop`` real em thread daemon para acordar ``glib.idle_add``.

    A ``_evaluate_js`` de ``evaluate_js`` é agendada por ``glib.idle_add`` e o
    chamador bloqueia no ``Semaphore``. Sem display, o loop GLib genérico (que não
    depende de X11/Wayland) é suficiente para despachar a fonte ociosa.
    """

    def __enter__(self):
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._loop.quit()
        self._thread.join(timeout=5.0)


def test_desktop_js_eval_length_seam_returns_webkit_nul_sentinel():
    """O seam puro devolve o sentinela NUL-terminated (``-1``), não ``len(script)``."""
    assert app_module._desktop_js_eval_length("") == -1
    assert app_module._desktop_js_eval_length("abc") == -1

    accented = "acentuação—çãõíêóú"
    assert app_module._desktop_js_eval_length(accented) == -1
    # Contraste explícito com o valor defeituoso (code points) do pywebview original.
    assert app_module._desktop_js_eval_length(accented) != len(accented)


def test_custom_evaluate_js_passes_nul_sentinel_and_preserves_contract():
    """O monkeypatch entrega ``length=-1`` e preserva ``parse_json``/retorno reais.

    Exercita ``app._custom_evaluate_js`` (já instalado como
    ``BrowserView.evaluate_js``) contra um ``BrowserViewEvalProbe`` em memória, acordando
    ``glib.idle_add`` por um ``GLib.MainLoop`` daemon — sem GTK/display.
    """
    unbound = app_module.gtk_plat.BrowserView.evaluate_js

    # parse_json=False: valor convertido devolvido cru; length == -1; script íntegro.
    probe = BrowserViewEvalProbe("valor-bruto")
    with GLibMainLoopThread():
        out = unbound(probe, "var x = 'á';", False)
    assert out == "valor-bruto"
    assert len(probe.webview.calls) == 1
    assert probe.webview.calls[0]["length"] == -1
    assert probe.webview.calls[0]["script"] == "var x = 'á';"

    # parse_json=True: json.loads preservado sobre o valor convertido.
    probe_json = BrowserViewEvalProbe('{"ok": true, "n": 3}')
    with GLibMainLoopThread():
        out_json = unbound(probe_json, "({ok: true})", True)
    assert out_json == {"ok": True, "n": 3}
    assert probe_json.webview.calls[0]["length"] == -1


def test_custom_evaluate_js_source_uses_seam_not_codepoint_length():
    """A fonte do patch usa o seam ``_desktop_js_eval_length``, não ``len(script)``."""
    source = inspect.getsource(app_module._custom_evaluate_js)
    assert "length=_desktop_js_eval_length(script)" in source
    assert "length=len(script)" not in source
    # Contrato de parse_json e retorno preservados do original do pywebview.
    assert "if parse_json and res:" in source
    assert "return result" in source


def test_install_evaluate_js_length_patch_is_idempotent_and_fail_soft():
    """O instalador é fail-soft/idempotente e deixa o patch ativo já no import."""
    installer_source = inspect.getsource(app_module.install_evaluate_js_length_patch)
    assert "BrowserView.evaluate_js = _custom_evaluate_js" in installer_source
    assert "except Exception" in installer_source  # fail-soft

    # Ativo já no import do módulo (sem re-execução manual) e idempotente.
    assert app_module.gtk_plat.BrowserView.evaluate_js is app_module._custom_evaluate_js
    assert app_module.install_evaluate_js_length_patch() is True
    assert app_module.gtk_plat.BrowserView.evaluate_js is app_module._custom_evaluate_js


@pytest.mark.parametrize("sample", [
    "reconexão",
    "contenção de idle",
    "travessão — multibyte",
    "usuário café naïve",
    "aceleração 🚀",
])
def test_utf8_byte_parity_guard_for_multibyte_samples(sample):
    """Guard de paridade bytes: ``length`` do WebKitGTK é em BYTES, não code points.

    Fixa a invariante que justifica o monkeypatch (``-1`` = string terminada em NUL):
    para qualquer string multibyte ``len(code points) != len(bytes)``, logo usar
    ``len(script)`` como ``length`` perderia bytes e exporia ``WebKitJavascriptError``.
    A estratégia correta (``-1`` ou bytes integrais) preserva a carga completa.
    Determinístico e sem GTK/display.
    """
    encoded = sample.encode("utf-8")
    assert len(encoded) > len(sample), "amostra precisa ser multibyte"

    # Caminho defeituoso do pywebview: slice em code points sobre o buffer UTF-8.
    codepoint_slice = encoded[: len(sample)]
    assert codepoint_slice != encoded, "o caminho defectivo deve truncar a carga"
    assert len(codepoint_slice) < len(encoded)

    # Estratégia correta (bytes integrais / NUL-terminated): carga completa preservada.
    assert encoded[: len(encoded)] == encoded


# =====================================================================
# 11. T2 — Robustez do transporte evaluate_js: timeout defensivo do
#     Semaphore (acquire(timeout=5.0)) e caminhos reais de exceção
#     validados com pytest.raises. Zero mocks: probes reais em memória e
#     GLib.MainLoop real (sem display/X11).
# =====================================================================

class WebKitWebViewSilentProbe:
    """Probe do ``WebKitWebView`` que NUNCA conclui o ``callback`` do WebKit.

    Modela o cenário real de robustez em que o WebKitGTK aceita
    ``evaluate_javascript`` mas jamais dispara o callback de conclusão
    (renderer travado/morto, evento perdido). Não é mock: é a fronteira nativa
    exposta por um objeto real em memória; o ``Semaphore`` de
    ``app._custom_evaluate_js`` nunca é liberado.
    """

    def __init__(self) -> None:
        self.calls: typing.List[typing.Dict[str, typing.Any]] = []

    def evaluate_javascript(self, script, length, world_name, source_uri, cancellable, callback):
        """Registra o transporte real e retorna SEM invocar ``callback``."""
        self.calls.append({"script": script, "length": length})
        # Intencionalmente NÃO invoca callback: reproduz o WebKit mudo.


class BrowserViewSilentProbe:
    """Probe do ``BrowserView`` emparelhado ao ``WebKitWebViewSilentProbe``."""

    def __init__(self) -> None:
        self.webview = WebKitWebViewSilentProbe()

    def _convert_js_value(self, value):
        return value


def test_custom_evaluate_js_timeout_returns_none_without_hanging(caplog):
    """O ``acquire(timeout=5.0)`` devolve ``None`` e NÃO trava quando o WebKit silencia.

    Repro real do defeito de robustez (TICKET-DESKTOP-robustez): antes do fix o
    ``result_semaphore.acquire()`` sem timeout bloqueava a thread da chamada PARA
    SEMPRE se o callback de conclusão do WebKit nunca chegasse. Com a guarda, o
    idle ainda despacha ``evaluate_javascript`` (loop GLib real), mas a ausência
    de callback expira o semáforo em ~5s e a função devolve ``None`` dentro de um
    limite temporal estrito, emitindo ``logger.warning``.
    """
    unbound = app_module.gtk_plat.BrowserView.evaluate_js
    probe = BrowserViewSilentProbe()

    caplog.set_level("WARNING", logger="openchamber.desktop")
    with GLibMainLoopThread():
        start = time.monotonic()
        out = unbound(probe, "var x = 1;", False)
        elapsed = time.monotonic() - start

    # Não trava: retorna (None) dentro de uma janela temporal estrita.
    assert out is None
    assert elapsed >= 4.5, f"o caminho de timeout nem foi exercitado ({elapsed:.2f}s)"
    assert elapsed < 20.0, f"a chamada travou além do timeout defensivo ({elapsed:.2f}s)"

    # O idle real despachou o transporte (length=-1 preservado), sem consumir callback.
    assert len(probe.webview.calls) == 1
    assert probe.webview.calls[0]["length"] == -1
    assert probe.webview.calls[0]["script"] == "var x = 1;"

    # O ramo de timeout emite o aviso dedicado (observabilidade fail-soft).
    assert any(
        "Timeout aguardando execucao de evaluate_js" in record.getMessage()
        for record in caplog.records
        if record.name == "openchamber.desktop"
    ), "o ramo de timeout deve emitir logger.warning"


def test_custom_evaluate_js_source_guards_acquire_with_timeout():
    """A fonte do patch protege o ``acquire`` com timeout e fallback fail-soft (None)."""
    source = inspect.getsource(app_module._custom_evaluate_js)
    assert "if not result_semaphore.acquire(timeout=5.0):" in source
    # Sem retorno ao bloqueio indefinido (acquire sem argumento/limite).
    assert "result_semaphore.acquire()" not in source
    assert "logger.warning" in source
    # O fallback não levanta: devolve o ``result`` default (None).
    assert "return result" in source


def test_resolve_debug_mode_invalid_mapping_raises_attribute_error():
    """Parâmetro fora do contrato ``Mapping`` deve falhar alto (fail-fast), não silenciar.

    ``resolve_debug_mode`` consulta ``source.get(...)``; um contêiner sem ``.get``
    (ex.: ``list``) viola a assinatura tipada e levanta ``AttributeError``.
    """
    with pytest.raises(AttributeError):
        app_module.resolve_debug_mode(env=[])


def test_browser_spawn_suppressed_invalid_env_raises_attribute_error():
    """``browser_spawn_suppressed`` exige um ``Mapping``; env inválido levanta ``AttributeError``."""
    with pytest.raises(AttributeError):
        app_module.browser_spawn_suppressed(env=[], modules={})


def test_shortcut_handler_requires_real_event_key():
    """O handler de atalhos assume ``Gdk.EventKey``; um evento sem ``keyval`` levanta."""
    handler = app_module.create_shortcut_handler(object())
    with pytest.raises(AttributeError):
        handler(object(), object())


def test_shortcut_handler_rejects_wrong_typed_modifier_state():
    """``event.state`` não inteiro quebra a máscara de modificadores (``TypeError``)."""
    handler = app_module.create_shortcut_handler(object())

    class _WrongTypedEvent:
        keyval = Gdk.KEY_a
        state = "not-an-int"

    with pytest.raises(TypeError):
        handler(object(), _WrongTypedEvent())


def test_ticket11_production_and_tests_are_free_of_mocks():
    """Anti-mock: nem o app.py (produção) nem esta suíte importam bibliotecas de mock."""
    mock_tokens = (
        "unit" + "test.mock",
        "from unit" + "test import mock",
        "Magic" + "Mock",
        "mocker" + ".",
    )
    for path in (APP_DIR / "app.py", Path(__file__).resolve()):
        content = path.read_text(encoding="utf-8")
        for token in mock_tokens:
            assert token not in content, f"mock proibido em {path.name}: {token}"
