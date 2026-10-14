import os
import base64
import logging
import shutil
import subprocess
import sys
import threading
import time
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


def browser_spawn_suppressed(
    env: typing.Optional[typing.Mapping[str, str]] = None,
    modules: typing.Optional[typing.Container[str]] = None,
) -> bool:
    """Indica se o spawn de navegador deve ser suprimido no ambiente atual.

    Duas condições independentes, avaliadas sem nenhum efeito colateral:

    * ``OPENCHAMBER_DISABLE_BROWSER_SPAWN=1`` — corte explícito para scripts,
      smoke tests e sessões headless;
    * ``pytest`` presente em ``sys.modules`` — qualquer execução da suíte
      determinística bloqueia o spawn, blindando a máquina da usuária contra
      abas/janelas reais de navegador.

    O contrato dos argumentos é o mesmo de ``resolve_debug_mode``: quando os
    parâmetros são ``None`` a fonte real é consultada (``os.environ`` e
    ``sys.modules``); quando fornecidos, são a única fonte consultada — o que
    torna cada ramificação testável de forma pura e determinística.

    Args:
        env: Mapping opcional de ambiente (padrão ``os.environ``).
        modules: Container opcional de módulos carregados (padrão
            ``sys.modules``).

    Exemplo:
        >>> browser_spawn_suppressed(env={}, modules={})
        False
        >>> browser_spawn_suppressed(env={}, modules={'pytest': None})
        True
        >>> browser_spawn_suppressed(
        ...     env={'OPENCHAMBER_DISABLE_BROWSER_SPAWN': '1'}, modules={})
        True
        >>> browser_spawn_suppressed(
        ...     env={'OPENCHAMBER_DISABLE_BROWSER_SPAWN': '0'}, modules={})
        False
    """
    source: typing.Mapping[str, str] = os.environ if env is None else env
    loaded: typing.Container[str] = sys.modules if modules is None else modules
    return source.get('OPENCHAMBER_DISABLE_BROWSER_SPAWN') == '1' or 'pytest' in loaded


def open_external(url: str) -> None:
    """Abre URL externa no navegador padrão do sistema operacional.

    Guardas fail-fast avaliadas ANTES de qualquer side-effect no sistema
    operacional (nenhuma das duas rotas — ``webbrowser.open`` ou
    ``subprocess.run(['xdg-open', ...])`` — é alcançada quando uma guarda
    dispara):

    1. **Validação preliminar estrita:** ``url`` vazia/nula ou que não passe
       em ``is_external_url`` (esquemas desconhecidos como ``javascript:``,
       ``file:``, ``data:``, ``about:`` e hosts locais como ``localhost``/
       ``127.0.0.1``) → retorno imediato.
    2. **Ambiente de teste automatizado:** ``OPENCHAMBER_DISABLE_BROWSER_SPAWN=1``
       ou ``pytest`` em ``sys.modules`` → retorno imediato, garantindo que a
       suíte nunca abra abas reais na máquina da usuária.

    Args:
        url: URL a ser aberta. Vazias, inválidas ou locais são descartadas em
            silêncio, sem levantar exceção.

    Exemplo:
        >>> open_external("")
        >>> open_external("javascript:void(0)")
        >>> open_external("http://localhost:3000")
    """
    if not url or not is_external_url(url):
        return
    if browser_spawn_suppressed():
        return
    try:
        webbrowser.open(url)
    except Exception:
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


# TICKET-DESKTOP-11: janela pywebview ativa registrada para a ponte nativa de
# reciclagem de sessão (DesktopAPI.recycle_session -> window.location.reload).
_desktop_window: typing.Optional[webview.Window] = None

# Cooldown anti-loop em segundos entre reciclagens de sessão acionadas via API.
_RECYCLE_COOLDOWN_S: float = 10.0
_last_recycle_ts: float = 0.0


def register_desktop_window(window: webview.Window) -> None:
    """Registra a janela pywebview ativa para que a DesktopAPI possa reciclá-la.

    TICKET-DESKTOP-11: ponte nativa de reciclagem — o método
    ``DesktopAPI.recycle_session()`` precisa alcançar a janela criada em
    ``__main__`` para disparar ``window.location.reload()`` sem acoplar a API
    ao ciclo de vida do GTK. Fail-soft: qualquer falha é logada em debug.
    """
    global _desktop_window
    try:
        _desktop_window = window
    except Exception as err:
        logger.debug("Falha ao registrar janela desktop para reciclagem: %s", err)


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

    def recycle_session(self, reason: str = 'manual', force: bool = False) -> bool:
        """Solicita reload limpo da janela para forçar GC e descarte de nós órfãos.

        TICKET-DESKTOP-11: ponte nativa acionável pelo frontend
        (``window.pywebview.api.recycle_session()``) ou pelo desktop quando
        ciclos de reinício do backend deixaram streams SSE/WebSocket, turnos e
        chunks textuais antigos acumulados na árvore React/JavaScriptCore.

        Proteção anti-loop: reciclagens consecutivas são suprimidas por
        ``_RECYCLE_COOLDOWN_S`` (10s) salvo ``force=True``. O ``location.reload()``
        é despachado em thread daemon (mesmo padrão do atalho F5/Ctrl+R) para
        nunca bloquear a thread da chamada. Retorna True somente quando o
        reload foi efetivamente despachado; qualquer falha é fail-soft (False).
        """
        global _last_recycle_ts
        try:
            now = time.monotonic()
            if not force and (now - _last_recycle_ts) < _RECYCLE_COOLDOWN_S:
                logger.debug(
                    "Reciclagem de sessão suprimida pelo cooldown de %.0fs (reason=%s)",
                    _RECYCLE_COOLDOWN_S, reason,
                )
                return False
            target = _desktop_window
            if target is None:
                try:
                    target = webview.active_window()
                except Exception:
                    target = None
            if target is None:
                logger.debug(
                    "Reciclagem de sessão ignorada: janela desktop indisponível (reason=%s)",
                    reason,
                )
                return False

            def dispatch_reload() -> None:
                """Avalia window.location.reload() na janela alvo sem propagar erros."""
                try:
                    target.evaluate_js('window.location.reload()')
                except Exception as err:
                    logger.debug("Falha ao despachar reload na janela desktop: %s", err)

            _last_recycle_ts = now
            threading.Thread(target=dispatch_reload, daemon=True).start()
            logger.debug("Reciclagem de sessão despachada (reason=%s)", reason)
            return True
        except Exception as err:
            logger.debug("Falha ao reciclar sessão desktop: %s", err)
            return False

    def reload_page(self, reason: str = 'frontend', force: bool = False) -> bool:
        """Alias semântico de ``recycle_session`` para o frontend pedir reload limpo."""
        return self.recycle_session(reason=reason, force=force)


def get_desktop_handlers_js() -> str:
    """Gera o script JavaScript injetado na janela para atalhos, links externos e colagem de imagens (TICKET-DESKTOP-07).

    Inclui também o subsistema de contenção de animações CSS em idle/perda de
    foco/janela oculta (TICKET-DESKTOP-10): pausa ``animation-play-state`` de
    todos os elementos via classe ``oc-desktop-idle`` no ``<html>`` quando a
    janela perde o foco, fica oculta (minimizada) ou o usuário fica inativo por
    30s — eliminando repaints a 60 FPS e o transporte SharedMemory contínuo que
    dispara o consumo de CPU do ``WebKitWebProcess`` + GTK main loop.

    Inclui ainda o subsistema de reconexão ao backend (TICKET-DESKTOP-11):
    heartbeat leve a cada 5s contra ``/api/system/info`` (fallback ``/health``)
    detectando offline -> online e renovação de instância do daemon, com
    evento ``openchamber:backend-reconnected``, descarte de EventSource/
    WebSocket órfãos e recarga controlada com cooldown anti-loop de 10s.
    """
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

            // =====================================================================
            // TICKET-DESKTOP-10 — Contenção de repaints em idle/desfocada/minimizada
            // Pausa animações CSS contínuas (spinners, cursores, pulsos) para cortar
            // os repaints a 60 FPS que mantêm WebKitWebProcess + GTK em CPU alta.
            // =====================================================================
            if (!window.__desktop_idle_throttle_installed) {
                window.__desktop_idle_throttle_installed = true;
                (function() {
                    'use strict';
                    // Estado idempotente: razões independentes (blur/oculto vs inatividade)
                    // para que a liberação de uma não desfaça a outra.
                    window.__desktop_idle_active = false;
                    window.__desktop_idle_reasons = { blur: false, idle: false };
                    window.__desktop_idle_timer = null;
                    window.__desktop_idle_last_tick = 0;
                    window.__desktop_idle_timeout_ms = 30000;
                    window.__desktop_idle_activity_throttle_ms = 250;
                    window.__desktop_idle_style_el = null;

                    // a) Regra CSS dinâmica: pausa global de animações sob a classe raiz.
                    var ensureIdleStyle = function() {
                        try {
                            if (window.__desktop_idle_style_el && window.__desktop_idle_style_el.parentNode) {
                                return window.__desktop_idle_style_el;
                            }
                            if (typeof document === 'undefined' || !document.head) {
                                return null;
                            }
                            var el = document.getElementById('__desktop_idle_style');
                            if (!el) {
                                el = document.createElement('style');
                                el.id = '__desktop_idle_style';
                                el.setAttribute('data-ticket', 'TICKET-DESKTOP-10');
                                el.textContent = 'html.oc-desktop-idle *, html.oc-desktop-idle *::before, html.oc-desktop-idle *::after { animation-play-state: paused !important; }';
                                document.head.appendChild(el);
                            }
                            window.__desktop_idle_style_el = el;
                            return el;
                        } catch (_) {
                            return null;
                        }
                    };

                    // Aplica/remove a contenção de forma idempotente e fail-soft.
                    var syncIdleState = function() {
                        try {
                            var reasons = window.__desktop_idle_reasons;
                            var active = !!(reasons.blur || reasons.idle);
                            if (active === window.__desktop_idle_active) {
                                return;
                            }
                            var root = (typeof document !== 'undefined') ? document.documentElement : null;
                            if (!root || !root.classList) {
                                return;
                            }
                            if (active) {
                                if (!ensureIdleStyle()) {
                                    return;
                                }
                                root.classList.add('oc-desktop-idle');
                            } else {
                                root.classList.remove('oc-desktop-idle');
                            }
                            window.__desktop_idle_active = active;
                        } catch (_) {}
                    };

                    var setReason = function(name, value) {
                        try {
                            window.__desktop_idle_reasons[name] = !!value;
                            syncIdleState();
                        } catch (_) {}
                    };

                    // Watchdog de inatividade: ativa a contenção após 30s sem interação.
                    var scheduleIdleWatchdog = function() {
                        try {
                            if (window.__desktop_idle_timer) {
                                clearTimeout(window.__desktop_idle_timer);
                            }
                            window.__desktop_idle_timer = setTimeout(function() {
                                window.__desktop_idle_timer = null;
                                setReason('idle', true);
                            }, window.__desktop_idle_timeout_ms);
                        } catch (_) {}
                    };

                    // Atividade do usuário: libera a contenção de idle instantaneamente
                    // e reagenda o watchdog (throttled para mousemove/wheel de alta taxa).
                    var noteActivity = function(immediate) {
                        try {
                            if (window.__desktop_idle_reasons.idle) {
                                setReason('idle', false);
                            }
                            var now = (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0;
                            if (!immediate && (now - window.__desktop_idle_last_tick) < window.__desktop_idle_activity_throttle_ms) {
                                return;
                            }
                            window.__desktop_idle_last_tick = now;
                            scheduleIdleWatchdog();
                        } catch (_) {}
                    };

                    // c) Listeners de atividade do usuário (throttled).
                    var activityEvents = ['mousemove', 'wheel', 'mousedown', 'touchstart'];
                    for (var i = 0; i < activityEvents.length; i++) {
                        (function(evt) {
                            try {
                                document.addEventListener(evt, function() {
                                    noteActivity(evt === 'mousedown' || evt === 'touchstart');
                                }, true);
                            } catch (_) {}
                        })(activityEvents[i]);
                    }
                    try {
                        window.addEventListener('keydown', function() {
                            noteActivity(true);
                        }, true);
                    } catch (_) {}

                    // b) Eventos de janela / visibilidade: blur e oculto contenem
                    // imediatamente; focus e visível removem a contenção imediatamente.
                    try {
                        window.addEventListener('blur', function() {
                            setReason('blur', true);
                        }, true);
                        window.addEventListener('focus', function() {
                            setReason('blur', false);
                            setReason('idle', false);
                            window.__desktop_idle_last_tick = (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0;
                            scheduleIdleWatchdog();
                        }, true);
                        document.addEventListener('visibilitychange', function() {
                            try {
                                setReason('blur', !!(document && document.hidden));
                                if (!(document && document.hidden)) {
                                    setReason('idle', false);
                                    scheduleIdleWatchdog();
                                }
                            } catch (_) {}
                        }, true);
                    } catch (_) {}

                    // Armadura inicial: inicia o watchdog (ainda sem contenção ativa).
                    try {
                        ensureIdleStyle();
                        scheduleIdleWatchdog();
                    } catch (_) {}
                })();
            }

            // =====================================================================
            // TICKET-DESKTOP-11 — Reconexão ao backend + reciclagem de streams
            // Heartbeat leve (5s) contra /api/system/info com fallback /health:
            // detecta queda -> retorno do daemon e renovação de instância
            // (server_instance_id / pid), dispara openchamber:backend-reconnected,
            // descarta EventSource/WebSocket órfãos e força reload limpo (GC da
            // árvore React/JavaScriptCore) com cooldown anti-loop de 10s persistido
            // em sessionStorage — o cooldown sobrevive ao próprio location.reload()
            // e blinda contra loops de reload a cada boot do backend.
            // =====================================================================
            if (!window.__desktop_reconnect_installed) {
                window.__desktop_reconnect_installed = true;
                (function() {
                    'use strict';

                    // Parâmetros seguros por padrão; o frontend pode sobrescrevê-los
                    // via window.__desktop_reconnect_config antes desta injeção.
                    var RECONNECT_POLL_MS = 5000;
                    var RECONNECT_FETCH_TIMEOUT_MS = 4000;
                    var RECONNECT_LONG_OFFLINE_MS = 4000;
                    var RECONNECT_RELOAD_COOLDOWN_MS = 10000;
                    var RECONNECT_RELOAD_AFTER_RESTARTS = 3;
                    var RECONNECT_STORAGE_KEY = 'openchamber:desktop:reconnect:last-reload';
                    var RECONNECT_ENDPOINTS = ['/api/system/info', '/health'];

                    window.__desktop_reconnect_timer = null;
                    window.__desktop_reconnect_online = true;
                    window.__desktop_reconnect_offline_since = null;
                    window.__desktop_reconnect_instance_id = null;
                    window.__desktop_reconnect_restart_count = 0;
                    window.__desktop_reconnect_last_reload_ts = 0;
                    window.__desktop_reconnect_ticks = 0;
                    window.__desktop_reconnect_streams_discarded = 0;

                    var nowMs = function() {
                        try {
                            return (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0;
                        } catch (_) {
                            return 0;
                        }
                    };

                    var readConfig = function() {
                        try {
                            return window.__desktop_reconnect_config || {};
                        } catch (_) {
                            return {};
                        }
                    };

                    var readLastReloadTs = function() {
                        try {
                            if (typeof sessionStorage !== 'undefined' && sessionStorage) {
                                var raw = sessionStorage.getItem(RECONNECT_STORAGE_KEY);
                                var parsed = raw ? parseInt(raw, 10) : 0;
                                if (isFinite(parsed) && parsed > 0) {
                                    return parsed;
                                }
                            }
                        } catch (_) {}
                        return window.__desktop_reconnect_last_reload_ts || 0;
                    };

                    var persistLastReloadTs = function(ts) {
                        window.__desktop_reconnect_last_reload_ts = ts;
                        try {
                            if (typeof sessionStorage !== 'undefined' && sessionStorage) {
                                sessionStorage.setItem(RECONNECT_STORAGE_KEY, String(ts));
                            }
                        } catch (_) {}
                    };

                    var reloadCooldownElapsed = function() {
                        var cooldown = RECONNECT_RELOAD_COOLDOWN_MS;
                        try {
                            var cfg = readConfig();
                            if (typeof cfg.reloadCooldownMs === 'number' && cfg.reloadCooldownMs > 0) {
                                cooldown = cfg.reloadCooldownMs;
                            }
                        } catch (_) {}
                        return (nowMs() - readLastReloadTs()) >= cooldown;
                    };

                    var extractInstanceId = function(data) {
                        try {
                            if (!data || typeof data !== 'object') {
                                return null;
                            }
                            var keys = ['server_instance_id', 'serverInstanceId', 'instance_id', 'instanceId', 'runtime_id', 'pid'];
                            for (var i = 0; i < keys.length; i++) {
                                var value = data[keys[i]];
                                if (value !== undefined && value !== null && value !== '') {
                                    return String(value);
                                }
                            }
                        } catch (_) {}
                        return null;
                    };

                    var closeStreamTarget = function(target) {
                        try {
                            if (target && typeof target.close === 'function') {
                                target.close();
                                return true;
                            }
                        } catch (_) {}
                        return false;
                    };

                    // Descarte preventivo de streams pendentes: fecha EventSource e
                    // WebSocket registrados em window.__openchamber_streams (array ou
                    // mapa) e nas globais residuais de conexões de turnos anteriores.
                    var discardPendingStreams = function() {
                        var closed = 0;
                        try {
                            var registry = window.__openchamber_streams;
                            if (registry && typeof registry === 'object') {
                                if (Array.isArray(registry)) {
                                    for (var i = 0; i < registry.length; i++) {
                                        if (closeStreamTarget(registry[i])) {
                                            closed++;
                                        }
                                    }
                                } else {
                                    var mapKeys = Object.keys(registry);
                                    for (var k = 0; k < mapKeys.length; k++) {
                                        if (closeStreamTarget(registry[mapKeys[k]])) {
                                            closed++;
                                        }
                                    }
                                }
                            }
                        } catch (_) {}
                        var globals = ['__openchamber_eventsource', '__openchamber_sse', '__openchamber_socket', '__openchamber_ws', '__eventSource', '__sse', '__socket', '__ws'];
                        for (var g = 0; g < globals.length; g++) {
                            try {
                                if (closeStreamTarget(window[globals[g]])) {
                                    closed++;
                                    window[globals[g]] = null;
                                }
                            } catch (_) {}
                        }
                        window.__desktop_reconnect_streams_discarded = closed;
                        return closed;
                    };

                    // Recarga controlada: somente após reinício completo confirmado
                    // (mudança de instância + queda prolongada) ou após acúmulo de
                    // reinícios, e sempre respeitando o cooldown anti-loop de 10s.
                    var maybeReload = function(detail) {
                        try {
                            var cfg = readConfig();
                            if (cfg.autoReload === false) {
                                return;
                            }
                            var restartsThreshold = RECONNECT_RELOAD_AFTER_RESTARTS;
                            if (typeof cfg.reloadAfterRestarts === 'number' && cfg.reloadAfterRestarts > 0) {
                                restartsThreshold = cfg.reloadAfterRestarts;
                            }
                            var confirmedRestart = !!detail.instanceChanged && detail.offlineMs >= RECONNECT_LONG_OFFLINE_MS;
                            var accumulatedRestarts = detail.restartCount >= restartsThreshold;
                            if (!confirmedRestart && !accumulatedRestarts) {
                                return;
                            }
                            if (!reloadCooldownElapsed()) {
                                return;
                            }
                            persistLastReloadTs(nowMs());
                            window.location.reload();
                        } catch (_) {}
                    };

                    // Coordenada de reconexão: evento personalizado -> descarte de
                    // streams -> recarga controlada (ordem do TICKET-DESKTOP-11).
                    var handleReconnect = function(detail) {
                        try {
                            window.dispatchEvent(new CustomEvent('openchamber:backend-reconnected', { detail: detail }));
                        } catch (_) {}
                        try {
                            discardPendingStreams();
                        } catch (_) {}
                        maybeReload(detail);
                    };

                    var onBackendOffline = function() {
                        try {
                            if (window.__desktop_reconnect_online) {
                                window.__desktop_reconnect_online = false;
                                window.__desktop_reconnect_offline_since = nowMs();
                            }
                        } catch (_) {}
                    };

                    var onBackendOnline = function(data) {
                        try {
                            var wasOffline = !window.__desktop_reconnect_online;
                            var offlineMs = 0;
                            if (wasOffline && window.__desktop_reconnect_offline_since) {
                                offlineMs = Math.max(0, nowMs() - window.__desktop_reconnect_offline_since);
                            }
                            var instanceId = extractInstanceId(data);
                            var previousInstanceId = window.__desktop_reconnect_instance_id;
                            var instanceChanged = !!(instanceId && previousInstanceId && instanceId !== previousInstanceId);
                            if (instanceChanged) {
                                window.__desktop_reconnect_restart_count += 1;
                            }
                            if (wasOffline || instanceChanged) {
                                handleReconnect({
                                    at: nowMs(),
                                    wasOffline: wasOffline,
                                    offlineMs: offlineMs,
                                    instanceChanged: instanceChanged,
                                    previousInstanceId: previousInstanceId,
                                    serverInstanceId: instanceId,
                                    restartCount: window.__desktop_reconnect_restart_count
                                });
                            }
                            if (instanceId) {
                                window.__desktop_reconnect_instance_id = instanceId;
                            }
                            window.__desktop_reconnect_online = true;
                            window.__desktop_reconnect_offline_since = null;
                        } catch (_) {}
                    };

                    // Heartbeat: tenta /api/system/info e cai para /health se falhar;
                    // só marca offline quando TODOS os endpoints responderem mal.
                    var probeBackend = function() {
                        var endpoints = RECONNECT_ENDPOINTS;
                        try {
                            var cfg = readConfig();
                            if (Array.isArray(cfg.endpoints) && cfg.endpoints.length > 0) {
                                endpoints = cfg.endpoints;
                            }
                        } catch (_) {}

                        var attempt = 0;
                        var settled = false;

                        var failAttempt = function() {
                            if (settled) {
                                return;
                            }
                            attempt += 1;
                            if (attempt >= endpoints.length) {
                                settled = true;
                                onBackendOffline();
                                return;
                            }
                            fetchEndpoint();
                        };

                        var fetchEndpoint = function() {
                            try {
                                if (typeof fetch !== 'function') {
                                    failAttempt();
                                    return;
                                }
                                var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
                                var guard = setTimeout(function() {
                                    try {
                                        if (controller) {
                                            controller.abort();
                                        }
                                    } catch (_) {}
                                }, RECONNECT_FETCH_TIMEOUT_MS);
                                fetch(endpoints[attempt], { cache: 'no-store', signal: controller ? controller.signal : undefined })
                                    .then(function(response) {
                                        clearTimeout(guard);
                                        if (!response || !response.ok) {
                                            throw new Error('backend indisponível');
                                        }
                                        return response.json().catch(function() { return {}; });
                                    })
                                    .then(function(body) {
                                        if (settled) {
                                            return;
                                        }
                                        settled = true;
                                        onBackendOnline(body || {});
                                    })
                                    .catch(function() {
                                        clearTimeout(guard);
                                        failAttempt();
                                    });
                            } catch (_) {
                                failAttempt();
                            }
                        };

                        fetchEndpoint();
                    };

                    // Loop de heartbeat fail-soft: tick fixo de 5s (ativo ou após
                    // erro de rede), com guarda de idempotência de timer único.
                    var scheduleTick = function() {
                        try {
                            if (window.__desktop_reconnect_timer) {
                                clearTimeout(window.__desktop_reconnect_timer);
                            }
                            var interval = RECONNECT_POLL_MS;
                            try {
                                var cfg = readConfig();
                                if (typeof cfg.pollIntervalMs === 'number' && cfg.pollIntervalMs >= 1000) {
                                    interval = cfg.pollIntervalMs;
                                }
                            } catch (_) {}
                            window.__desktop_reconnect_timer = setTimeout(function() {
                                window.__desktop_reconnect_timer = null;
                                window.__desktop_reconnect_ticks += 1;
                                try {
                                    probeBackend();
                                } catch (_) {}
                                scheduleTick();
                            }, interval);
                        } catch (_) {}
                    };

                    try {
                        scheduleTick();
                    } catch (_) {}
                })();
            }
        }
    """


def setup_window_handlers(window: webview.Window) -> None:
    """Configura handlers de links externos e injeta listeners de desktop na janela.

    Expõe a função open_external_link no escopo JavaScript da janela e registra
    o listener no evento loaded para injetar os handlers de desktop (atalhos, links
    e colagem de imagem da área de transferência).
    """
    # TICKET-DESKTOP-11: registra a janela como alvo da reciclagem de sessão
    # (DesktopAPI.recycle_session) antes de expor os handlers ao frontend.
    register_desktop_window(window)

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
