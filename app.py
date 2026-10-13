import os
import logging
import shutil
import subprocess
import threading
import typing
import urllib.parse
import webbrowser
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk
import webview
import webview.platforms.gtk as gtk_plat

logger = logging.getLogger("openchamber.desktop")

URL = "http://127.0.0.1:3000"


def configure_graphics_environment() -> None:
    """Calibra o ambiente de renderização WebKitGTK para o host NVIDIA (TICKET-DESKTOP-08).

    No host Pop!_OS 24.04 com GPU NVIDIA proprietária (GeForce GTX 1050 Ti,
    driver 580.173.x) o renderer DMA-BUF do WebKitGTK entra em conflito com o
    caminho EGL/GBM do driver e provoca blank/flicker com CPU alta no
    ``WebKitWebProcess``. ``WEBKIT_DISABLE_DMABUF_RENDERER=1`` remove apenas o
    caminho zero-copy DMA-BUF e deixa o WebKit cair no transporte SharedMemory;
    a aceleração por GL/EGL continua ativa — **não** é fallback de software.

    As duas flags herdadas abaixo são removidas do ambiente do processo porque
    ambas forçam caminhos de renderização indesejados neste host:

    * ``WEBKIT_DISABLE_COMPOSITING_MODE`` — força o Accelerated Compositing
      permanentemente desligado (conflita com o caminho acelerado).
    * ``LIBGL_ALWAYS_SOFTWARE`` — força o fallback de software (llvmpipe),
      que empurra a composição para a CPU e dispara o consumo observado.

    Exemplo:
        >>> import os
        >>> configure_graphics_environment()
        >>> os.environ.get('WEBKIT_DISABLE_DMABUF_RENDERER')
        '1'
        >>> os.environ.get('LIBGL_ALWAYS_SOFTWARE') is None
        True
    """
    # ``update`` força o valor sem passar por subscript defensivo e garante que a
    # flag de desativação do DMA-BUF esteja ativa antes de qualquer import de WebKit.
    os.environ.update({'WEBKIT_DISABLE_DMABUF_RENDERER': '1'})
    os.environ.pop('WEBKIT_DISABLE_COMPOSITING_MODE', None)
    os.environ.pop('LIBGL_ALWAYS_SOFTWARE', None)


configure_graphics_environment()

# TICKET-DESKTOP-08: o Web Inspector não pode abrir compulsoriamente no boot.
# Com ``debug=True`` o pywebview chamava ``webview.get_inspector().show()``
# logo na inicialização (gui/gtk), e o Inspector ficava serializando em loop
# os streams de SSE e WebSockets do OpenChamber — ~37% de CPU e ~989 MB de RAM
# no WebKitWebProcess. Desligar OPEN_DEVTOOLS_IN_DEBUG mantém o menu de
# contexto "Inspecionar Elemento" ativo, mas apenas sob demanda.
webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False


def is_external_url(url: str) -> bool:
    """Verifica se a URL aponta para fora do host local."""
    if not url:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in ('http', 'https'):
            host = (parsed.hostname or '').lower()
            return host not in ('127.0.0.1', 'localhost', '0.0.0.0', '::1')
        if parsed.scheme in ('mailto', 'tel'):
            return True
        return False
    except Exception:
        return False


def open_external(url: str) -> None:
    """Abre URL externa no navegador padrão do sistema operacional."""
    try:
        webbrowser.open(url)
    except Exception:
        import subprocess
        subprocess.run(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Interceptação de links externos no evento de navegação do WebKitGTK
_orig_on_navigation = gtk_plat.BrowserView.on_navigation


def _custom_on_navigation(self, webview_instance, decision, decision_type):
    if type(decision) is gtk_plat.webkit.NavigationPolicyDecision:
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri()
        if is_external_url(uri):
            open_external(uri)
            decision.ignore()
            return True
        elif action.get_frame_name() == '_blank':
            self.load_url(uri)
            decision.ignore()
            return True
    return _orig_on_navigation(self, webview_instance, decision, decision_type)


gtk_plat.BrowserView.on_navigation = _custom_on_navigation


# Suporte a recarregamento com atalhos (F5 / Ctrl+R) no container GTK
_orig_browser_view_init = gtk_plat.BrowserView.__init__


def create_shortcut_handler(window: webview.Window) -> typing.Callable[[typing.Any, typing.Any], bool]:
    """Cria o handler de atalhos F5 / Ctrl+R sobre o objeto Window do pywebview.

    O handler é devolvido como função pura (``widget, event -> bool``) para que a
    conexão ao sinal ``key-press-event`` seja feita uma única vez, na janela GTK.

    Exemplo:
        >>> import webview
        >>> w = webview.Window(uid='demo', title='demo', url='http://127.0.0.1:3000')
        >>> handler = create_shortcut_handler(w)
        >>> callable(handler)
        True
    """

    def on_key_press(widget: typing.Any, event: typing.Any) -> bool:
        is_f5 = (event.keyval == Gdk.KEY_F5)
        is_ctrl_r = bool(event.state & Gdk.ModifierType.CONTROL_MASK) and (event.keyval in (Gdk.KEY_r, Gdk.KEY_R))
        if is_f5 or is_ctrl_r:
            threading.Thread(
                target=lambda: window.evaluate_js('window.location.reload()'),
                daemon=True
            ).start()
            return True
        return False

    return on_key_press


def install_shortcut_listener(browse_view: typing.Any, window: webview.Window) -> None:
    """Conecta o listener de atalhos exclusivamente na janela GTK (nível top-level).

    TICKET-DESKTOP-08: a conexão duplicada também em ``browse_view.webview``
    fazia o mesmo ``key-press-event`` subir duas vezes pelo pipeline de eventos
    do WebKitGTK (janela GTK -> widget WebKit) e sobrecarregava o main loop do
    GTK com trabalho redundante a cada tecla pressionada.

    Exemplo:
        >>> import webview
        >>> w = webview.Window(uid='demo2', title='demo2', url='http://127.0.0.1:3000')
        >>> install_shortcut_listener(None, w)  # browse_view inválido não derruba o boot
    """
    try:
        browse_view.window.connect('key-press-event', create_shortcut_handler(window))
    except Exception as err:
        logger.debug("Falha ao conectar listener de atalhos na janela GTK: %s", err)


def _custom_browser_view_init(self: typing.Any, window: webview.Window) -> None:
    """Estende o ``BrowserView.__init__`` do pywebview injetando o listener de atalhos.

    Mantém a inicialização original e delega a conexão do ``key-press-event``
    para :func:`install_shortcut_listener`, garantindo um único handler na janela.

    Exemplo:
        >>> callable(_custom_browser_view_init)
        True
    """
    _orig_browser_view_init(self, window)
    install_shortcut_listener(self, window)


gtk_plat.BrowserView.__init__ = _custom_browser_view_init


def resolve_openchamber_bin() -> str:
    """Resolve o executável do openchamber no PATH ou fallback local."""
    return shutil.which('openchamber') or os.path.expanduser('~/.local/node/bin/openchamber')


def on_closed():
    if os.environ.get('OPENCHAMBER_KEEP_SERVER') == '1':
        return
    bin_path = resolve_openchamber_bin()
    try:
        subprocess.run([bin_path, 'stop'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as err:
        logger.debug("Falha ao parar openchamber no fechamento: %s", err)


def setup_window_handlers(window):
    # Exposição de API para abertura de links externos via JavaScript
    def open_external_link(url: str):
        open_external(url)

    try:
        window.expose(open_external_link)
    except Exception as err:
        logger.debug("Falha ao expor open_external_link: %s", err)

    # Registro de listeners em JavaScript (atalhos e links externos)
    def on_loaded():
        js_code = """
        if (!window.__desktop_handlers_installed) {
            window.__desktop_handlers_installed = true;
            window.addEventListener('keydown', function(e) {
                if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R'))) {
                    e.preventDefault();
                    window.location.reload();
                }
            }, true);
            document.addEventListener('click', function(e) {
                const a = e.target.closest('a');
                if (a && a.href) {
                    try {
                        const u = new URL(a.href, window.location.href);
                        if (u.hostname && u.hostname !== '127.0.0.1' && u.hostname !== 'localhost') {
                            e.preventDefault();
                            e.stopPropagation();
                            if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external_link) {
                                window.pywebview.api.open_external_link(a.href);
                            }
                        }
                    } catch (_) {}
                }
            }, true);
        }
        """
        try:
            window.evaluate_js(js_code)
        except Exception as err:
            logger.debug("Falha ao injetar script de desktop handlers: %s", err)

    window.events.loaded += on_loaded


if __name__ == '__main__':
    window = webview.create_window(
        title='Open Chamber',
        url=URL,
        width=1200,
        height=800,
        resizable=True
    )

    window.events.closed += on_closed
    setup_window_handlers(window)

    # Ativa armazenamento persistente; debug=True mantém o menu de contexto
    # "Inspecionar Elemento" — a janela do inspector não abre no boot porque
    # OPEN_DEVTOOLS_IN_DEBUG foi desligado no topo do módulo (TICKET-DESKTOP-08).
    webview.settings['ALLOW_FILE_URLS'] = True
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    webview.start(gui='gtk', debug=True, private_mode=False, storage_path=os.path.expanduser('~/.cache/openchamber'))
