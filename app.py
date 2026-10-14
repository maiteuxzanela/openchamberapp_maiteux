import os
import base64
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
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gdk, GObject, Gtk, GdkPixbuf, GLib
import webview
import webview.platforms.gtk as gtk_plat

logger = logging.getLogger("openchamber.desktop")

URL = "http://127.0.0.1:3000"

# TICKET-DESKTOP-09: valores aceitos como "verdadeiros" para OPENCHAMBER_DEBUG.
# Qualquer outro valor (incluindo ausência da variável) mantém o modo de produção
# com debug desligado — o padrão do app.
_DEBUG_TRUTHY_VALUES: typing.FrozenSet[str] = frozenset({'1', 'true', 'yes', 'on'})


def resolve_debug_mode(env: typing.Optional[typing.Mapping[str, str]] = None) -> bool:
    """Resolve se o app deve subir com o modo de depuração do pywebview (TICKET-DESKTOP-09).

    A resolução é pura e determinística: lê ``OPENCHAMBER_DEBUG`` do módulo
    ``env`` (por padrão ``os.environ``), aplica ``strip()`` e compara em
    minúsculas contra o conjunto de valores verdadeiros ``{'1', 'true', 'yes',
    'on'}``. Qualquer outro valor ou a ausência da variável retorna ``False``,
    mantendo o modo de produção como padrão.

    Com ``debug=True`` o wrapper GTK do pywebview força
    ``webkit_settings.enable_developer_extras = True``, o que mantém a
    infraestrutura de depuração do JavaScriptCore alocada (threads
    ``DebuggerDispatcher``/``InterruptDispatcher`` e heap anônimo retido). A
    ativação passa a ser exclusivamente sob demanda: o ``run.sh`` exporta a
    variável quando invocado com ``--debug``/``-d``.

    Args:
        env: Mapping opcional de ambiente. Quando ``None``, usa
            ``os.environ``; quando fornecido, é a única fonte consultada
            (o ambiente do processo não é contaminado).

    Exemplo:
        >>> resolve_debug_mode({'OPENCHAMBER_DEBUG': '1'})
        True
        >>> resolve_debug_mode({'OPENCHAMBER_DEBUG': '  True '})
        True
        >>> resolve_debug_mode({})
        False
        >>> resolve_debug_mode({'OPENCHAMBER_DEBUG': 'off'})
        False
    """
    source: typing.Mapping[str, str] = os.environ if env is None else env
    raw = source.get('OPENCHAMBER_DEBUG', '')
    if not isinstance(raw, str):
        return False
    return raw.strip().lower() in _DEBUG_TRUTHY_VALUES


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


def preserve_default_context_menu(browser_view: typing.Any) -> None:
    """Restaura o menu de contexto padrão do WebKitGTK quando o debug está desligado (TICKET-DESKTOP-09).

    Com ``debug=False`` o pywebview conecta em ``self.webview`` um handler
    ``context-menu`` que devolve ``True`` e suprime o menu de clique direito
    inteiro (copiar/colar/selecionar). Como o modo de produção agora nasce com
    ``debug=False``, esse handler é desconectado logo após o ``__init__`` para
    preservar a compatibilidade do menu de contexto com o comportamento atual do
    app — a única perda em relação a ``debug=True`` continua sendo o item
    "Inspecionar Elemento" (``enable_developer_extras`` segue desligado).

    A desconexão é feita por ``g_signal_handlers_disconnect_matched`` com
    ``G_SIGNAL_MATCH_ID``, atingindo somente os handlers do sinal
    ``context-menu`` (o closure de classe do WebKit, que desenha o menu
    padrão, não é afetado). Toda falha é fail-soft: nunca derruba o boot da
    janela.

    Exemplo:
        >>> preserve_default_context_menu(object())  # sem .webview: fail-soft, não levanta exceção
    """
    if resolve_debug_mode():
        return
    widget = getattr(browser_view, 'webview', None)
    if widget is None:
        return
    try:
        signal_id = GObject.signal_lookup('context-menu', type(widget))
        if not signal_id:
            logger.debug("Sinal 'context-menu' indisponível em %s", type(widget).__name__)
            return
        GObject.signal_handlers_disconnect_matched(
            widget, GObject.SignalMatchType.ID, signal_id, 0, None, None, None
        )
    except Exception as err:
        logger.debug("Falha ao restaurar menu de contexto padrão: %s", err)


def _custom_browser_view_init(self: typing.Any, window: webview.Window) -> None:
    """Estende o ``BrowserView.__init__`` do pywebview injetando o listener de atalhos.

    Mantém a inicialização original e delega a conexão do ``key-press-event``
    para :func:`install_shortcut_listener`, garantindo um único handler na janela.
    Logo em seguida, :func:`preserve_default_context_menu` remove o bloqueio do
    menu de contexto que o pywebview instala quando ``debug=False``.

    Exemplo:
        >>> callable(_custom_browser_view_init)
        True
    """
    _orig_browser_view_init(self, window)
    preserve_default_context_menu(self)
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


class DesktopAPI:
    """API nativa exposta ao frontend via pywebview (window.pywebview.api)."""

    def get_clipboard_image_base64(self) -> typing.Optional[str]:
        """Obtém imagem da área de transferência do sistema (Gdk.SELECTION_CLIPBOARD) em base64 PNG.

        TICKET-DESKTOP-07: O WebKit2GTK no Linux não transfere dados binários de imagem
        do Gdk.SELECTION_CLIPBOARD para o event.clipboardData do DOM nativamente.
        Para evitar deadlock no Wayland ao ser chamado de worker threads do pywebview,
        a requisição é despachada para a main thread via GLib.idle_add e aguarda
        de forma assíncrona o callback de imagem via threading.Event(timeout=1.0).
        Retorna a imagem serializada em base64 ascii ou None se não houver imagem,
        em caso de timeout ou falha.
        """
        try:
            event = threading.Event()
            holder: typing.Dict[str, typing.Any] = {'pixbuf': None}

            def on_image_received(clipboard: typing.Any, pixbuf: typing.Any, *args: typing.Any) -> None:
                """Callback invocado pelo GTK quando a imagem da área de transferência é recebida."""
                holder['pixbuf'] = pixbuf
                event.set()

            def request_image_in_main_thread() -> bool:
                """Solicita a imagem do clipboard dentro do contexto da main thread do GTK."""
                try:
                    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                    cb.request_image(on_image_received)
                except Exception as exc:
                    logger.debug("Falha ao solicitar imagem na main thread: %s", exc)
                    event.set()
                return False

            GLib.idle_add(request_image_in_main_thread)
            event.wait(timeout=1.0)

            pixbuf = holder.get('pixbuf')
            if pixbuf is None:
                return None
            success, buffer = pixbuf.save_to_bufferv('png', [], [])
            if not success or not buffer:
                return None
            return base64.b64encode(buffer).decode('ascii')
        except Exception as err:
            logger.debug("Falha ao capturar imagem do clipboard: %s", err)
            return None

    def open_external_link(self, url: str) -> None:
        """Abre URL externa no navegador padrão do sistema operacional."""
        open_external(url)


def get_desktop_handlers_js() -> str:
    """Gera o script JavaScript injetado na janela para atalhos, links externos e colagem de imagens (TICKET-DESKTOP-07)."""
    return """
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
            window.addEventListener('paste', function(e) {
                if (e.clipboardData && e.clipboardData.files && e.clipboardData.files.length > 0) {
                    return;
                }
                if (e.clipboardData && e.clipboardData.getData('text') && (!e.clipboardData.files || e.clipboardData.files.length === 0)) {
                    return;
                }
                if (window.pywebview && window.pywebview.api && window.pywebview.api.get_clipboard_image_base64) {
                    window.pywebview.api.get_clipboard_image_base64().then(function(base64Data) {
                        if (base64Data) {
                            try {
                                e.preventDefault();
                            } catch (_) {}
                            fetch('data:image/png;base64,' + base64Data)
                                .then(function(res) { return res.blob(); })
                                .then(function(blob) {
                                    const file = new File([blob], 'pasted-image.png', { type: 'image/png' });
                                    const fileInput = document.querySelector('input[type="file"][multiple]') || document.querySelector('input[type="file"]');
                                    if (fileInput) {
                                        const dt = new DataTransfer();
                                        if (fileInput.files) {
                                            for (let i = 0; i < fileInput.files.length; i++) {
                                                dt.items.add(fileInput.files[i]);
                                            }
                                        }
                                        dt.items.add(file);
                                        fileInput.files = dt.files;
                                        fileInput.dispatchEvent(new Event('change', { bubbles: true }));
                                    }
                                })
                                .catch(function(err) {
                                    console.error('Erro ao processar imagem colada:', err);
                                });
                        }
                    }).catch(function() {});
                }
            }, true);
        }
    """


def setup_window_handlers(window: webview.Window) -> None:
    """Configura handlers de links externos e injeta listeners de desktop na janela.

    Expõe a função open_external_link no escopo JavaScript da janela e registra
    o listener no evento loaded para injetar os handlers de desktop (atalhos, links
    e colagem de imagem da área de transferência).
    """
    # Exposição de API para abertura de links externos via JavaScript
    def open_external_link(url: str) -> None:
        """Abre URL externa no navegador padrão."""
        open_external(url)

    try:
        window.expose(open_external_link)
    except Exception as err:
        logger.debug("Falha ao expor open_external_link: %s", err)

    # Registro de listeners em JavaScript (atalhos, links externos e colagem de imagens)
    def on_loaded() -> None:
        """Injeta os scripts de atalhos e handlers de desktop no carregamento da janela."""
        js_code = get_desktop_handlers_js()
        try:
            window.evaluate_js(js_code)
        except Exception as err:
            logger.debug("Falha ao injetar script de desktop handlers: %s", err)

    window.events.loaded += on_loaded


if __name__ == '__main__':
    desktop_api = DesktopAPI()
    window = webview.create_window(
        title='Open Chamber',
        url=URL,
        width=1200,
        height=800,
        resizable=True,
        js_api=desktop_api
    )

    window.events.closed += on_closed
    setup_window_handlers(window)

    # TICKET-DESKTOP-09: debug sob demanda. ``resolve_debug_mode()`` devolve
    # False em produção (OPENCHAMBER_DEBUG ausente), o que desliga
    # ``enable_developer_extras`` no WebKitGTK e evita a alocação das threads
    # DebuggerDispatcher/InterruptDispatcher com o respectivo vazamento de heap
    # anônimo do JavaScriptCore. Com a flag ativa (``./run.sh --debug``) o menu
    # de contexto "Inspecionar Elemento" volta a ficar disponível, sem abrir a
    # janela do inspector no boot (OPEN_DEVTOOLS_IN_DEBUG=False, TICKET-DESKTOP-08).
    webview.settings['ALLOW_FILE_URLS'] = True
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    webview.start(gui='gtk', debug=resolve_debug_mode(), private_mode=False, storage_path=os.path.expanduser('~/.cache/openchamber'))
