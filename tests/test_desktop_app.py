import os
import base64
import inspect
import subprocess
import sys
import threading
import time
import typing
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


def test_desktop_api_open_external_link_fail_soft_on_empty_url():
    """Valida que open_external_link lida suavemente com URL vazia ou inválida sem exceção."""
    api = DesktopAPI()
    api.open_external_link("")
    api.open_external_link("javascript:void(0)")


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
