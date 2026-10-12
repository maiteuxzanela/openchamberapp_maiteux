import os
import webview

# Força aceleração por hardware e composição via GPU no WebKitGTK
os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '0'
os.environ['LIBGL_ALWAYS_SOFTWARE'] = '0'

URL = "http://127.0.0.1:3000"

if __name__ == '__main__':
    window = webview.create_window(
        title='Open Chamber',
        url=URL,
        width=1200,
        height=800,
        resizable=True
    )
    
    # Ativa armazenamento persistente e aceleração de renderização
    webview.settings['ALLOW_FILE_URLS'] = True
    webview.start(gui='gtk', private_mode=False, storage_path=os.path.expanduser('~/.cache/openchamber'))
