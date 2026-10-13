import os
import logging
import shutil
import subprocess
import threading
import urllib.parse
import webbrowser
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk
import webview
import webview.platforms.gtk as gtk_plat

logger = logging.getLogger("openchamber.desktop")

# Força aceleração por hardware e composição via GPU no WebKitGTK
os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '0'
os.environ['LIBGL_ALWAYS_SOFTWARE'] = '0'

URL = "http://127.0.0.1:3000"


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


def _custom_browser_view_init(self, window):
    _orig_browser_view_init(self, window)

    def on_key_press(widget, event):
        is_f5 = (event.keyval == Gdk.KEY_F5)
        is_ctrl_r = bool(event.state & Gdk.ModifierType.CONTROL_MASK) and (event.keyval in (Gdk.KEY_r, Gdk.KEY_R))
        if is_f5 or is_ctrl_r:
            threading.Thread(
                target=lambda: window.evaluate_js('window.location.reload()'),
                daemon=True
            ).start()
            return True
        return False

    self.window.connect('key-press-event', on_key_press)
    self.webview.connect('key-press-event', on_key_press)


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

    # Ativa armazenamento persistente e aceleração de renderização
    webview.settings['ALLOW_FILE_URLS'] = True
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    webview.start(gui='gtk', debug=True, private_mode=False, storage_path=os.path.expanduser('~/.cache/openchamber'))
