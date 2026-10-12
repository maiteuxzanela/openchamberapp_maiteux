# 🚀 OpenChamber Desktop App (Linux / WebKitGTK)

> Cliente desktop nativo, ultraleve e integrado para o [OpenChamber](https://github.com/openchamber) no Linux (Pop!_OS / Ubuntu), combinando o servidor local em segundo plano com uma janela gráfica GTK acelerada por GPU via `pywebview`.

---

## 📌 Visão Geral

Este repositório contém o wrapper de desktop do **OpenChamber** para Linux. Em vez de depender de um navegador pesado em abas convencionais (Chrome/Firefox) ou do consumo excessivo de memória de Electron, este cliente utiliza o motor nativo **WebKitGTK** integrado ao sistema operacional.

O app é composto por três peças principais:
1. **`run.sh`:** Script de bootstrap e orquestração do daemon. Garante que os caminhos do `opencode` e do Node estejam disponíveis no `PATH`, verifica se o servidor já está ativo na porta 3000 (ou o inicia em background) e dispara o cliente gráfico.
2. **`app.py`:** Aplicação Python com `pywebview` e `WebKitGTK` que renderiza a interface do OpenChamber em uma janela nativa independente, com aceleração por hardware ativada e armazenamento persistente em `~/.cache/openchamber`.
3. **`openchamber.desktop`:** Entrada de aplicativo XDG para integração no menu de aplicativos do GNOME/Pop!_OS e atalho na Área de Trabalho.

---

## 🏗️ Arquitetura e Fluxo de Execução

```
[ Usuária clica no ícone / Desktop ]
                │
                ▼
      ┌──────────────────┐
      │      run.sh      │
      └─────────┬────────┘
                │
        Porta 3000 ativa?
       ├─── NÃO ───► Inicia 'openchamber serve' em background
       └─── SIM  ───► Reaproveita servidor existente
                │
                ▼
      ┌──────────────────┐
      │      app.py      │ (Python 3 + pywebview / WebKitGTK)
      └─────────┬────────┘
                │
                ├── Força WebKit Compositing (GPU / DRI2)
                ├── Conecta em http://127.0.0.1:3000
                └── Armazenamento persistente em ~/.cache/openchamber
```

---

## 💻 Estrutura de Arquivos

```text
openchamber-app/
├── app.py                  # Criação da janela WebKitGTK nativa via pywebview
├── run.sh                  # Bootstrap do servidor e resolução de PATH/ambiente
├── openchamber.desktop     # Atalho desktop integrado ao sistema (GNOME/Pop!_OS)
├── requirements.txt        # Dependências Python (pywebview, PyGObject)
├── .gitignore              # Ignora venv/, debug.log e artefatos de compilação
└── README.md               # Esta documentação
```

---

## ⚙️ Pré-requisitos

### 1. Sistema Linux (Debian / Ubuntu / Pop!_OS)
Pacotes do sistema operacional necessários para o WebKitGTK e bindings Python:
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1
```

### 2. Runtime Node.js & OpenCode CLI
* **Node.js** `>= 22.0.0`
* **OpenCode CLI:** Binário instalado (ex: `~/.opencode/bin/opencode`)
* **OpenChamber CLI:** Pacote `@openchamber/web` instalado e disponível no PATH (ex: `~/.local/node/bin/openchamber`).

---

## 🚀 Instalação e Configuração

### 1. Clonar o repositório
```bash
git clone git@github.com:maiteuxzanela/openchamberapp_maiteux.git ~/openchamber-app
cd ~/openchamber-app
```

### 2. Configurar o Ambiente Virtual Python
```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Permissões de Execução
```bash
chmod +x run.sh openchamber.desktop
```

### 4. Instalar o Atalho no Sistema
Para disponibilizar o aplicativo no menu de aplicativos do Pop!_OS:
```bash
cp openchamber.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/
```

---

## 🔧 Configurações Detalhadas dos Scripts

### `run.sh`
Configura variáveis de ambiente vitais para o funcionamento do ecossistema:
* `PATH`: Injeta os diretórios dos binários do Node (`~/.local/node/bin`) e do OpenCode (`~/.opencode/bin`).
* `OPENCODE_BINARY`: Define o caminho absoluto para execução do motor de IA.
* Redireciona saídas e erros para `debug.log` para facilitar diagnóstico caso a interface não carregue.

### `app.py`
Ajustes do WebKitGTK para desempenho ideal:
```python
# Habilita composição de hardware e aceleração por GPU
os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '0'
os.environ['LIBGL_ALWAYS_SOFTWARE'] = '0'

# Cria janela 1200x800 com armazenamento persistente de sessão e cache
webview.start(gui='gtk', private_mode=False, storage_path=os.path.expanduser('~/.cache/openchamber'))
```

---

## 📋 Melhorias Mapeadas no Kanban

Melhorias de ciclo de vida e usabilidade mapeadas no backlog do projeto:
1. **Encerramento Automático do Servidor Node:** Trap de `EXIT`/`SIGTERM` para finalizar processos em background quando a janela gráfica for fechada.
2. **Habilitação de DevTools (`debug=True`):** Ativação do menu *"Inspecionar Elemento"* no clique direito.
3. **Interceptação de Links Externos:** Abrir automaticamente URLs de repositórios e documentações no navegador padrão do sistema (`xdg-open`).
4. **Atalhos Nativos de Recarregamento:** Suporte às teclas `F5` e `Ctrl+R`.

---

## 📄 Licença

Distribuído sob licença MIT. Criado e mantido por **Maiteux Zanela**.
