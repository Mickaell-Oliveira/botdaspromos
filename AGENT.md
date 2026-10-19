# 🤖 Bug das Promos — Autonomous Affiliate Marketing Engine

> **Visão Geral:** Sistema autônomo *serverless* de marketing de afiliados focado na **Amazon Brasil**. O pipeline realiza scraping de ofertas em tempo real, filtragem estrita de links, substituição de tags de afiliado, disparo automatizado em canal do Telegram e geração de criativos dinâmicos para Instagram (Cards e Reels estilo retenção) com aprovação *Human-in-the-Loop*.

---

## 🏗️ Arquitetura do Sistema (Pipeline)

O projeto opera em nuvem via **GitHub Actions** (execução a cada 30 minutos via `cron`) dividido em três agentes principais que utilizam o **Google Sheets** como banco de dados central (State Machine):

```text
[Canais Públicos Telegram (t.me/s/)] 
        │
        ▼ (Scraping HTML + BeautifulSoup)
┌─────────────────────────┐
│ 1. HUNTER               │ -> Filtra só Amazon, remove páginas de espera,
│ (hunter_promos.py)      │    injeta tag (bugdaspromo07-20), encurta URL e
└─────────────────────────┘    classifica relevância (FEED, STORY, NAO) via Gemini.
        │
        ▼ (Grava linha pendente)
┌─────────────────────────┐
│ BANCO DE DADOS          │ -> Planilha Google Sheets: "BugdasPromos" (Colunas A até L)
│ (Google Sheets API)     │
└─────────────────────────┘
        │
        ├─────────────────────────────────────────────┐
        ▼                                             ▼
┌─────────────────────────┐                 ┌─────────────────────────┐
│ 2. TELEGRAM PUBLISHER   │                 │ 3. INSTAGRAM MANAGER    │
│ (bot_promos_sheets.py)  │                 │ (insta_manager.py)      │
├─────────────────────────┤                 ├─────────────────────────┤
│ • Corta marca d'água    │                 │ • Checa cliques/replies │
│   (15% topo da imagem)  │                 │   do Admin no Telegram  │
│ • Formata moeda (R$)    │                 │ • Gera Card (Pillow) ou │
│ • Cria copy persuasiva  │                 │   Reels c/ voz neural e │
│ • Dispara no canal      │                 │   legenda dinâmica ASS  │
│   @OBugdasPromos        │                 │ • Envia prévia c/ botões│
└─────────────────────────┘                 └─────────────────────────┘
🛠️ Stack TecnológicaLinguagem & Runtime: Python 3.11 (Ubuntu Latest via GitHub Actions)Inteligência Artificial (LLM): Google Gemini API (google-genai — modelos gemini-3.8-flash / gemini-3.8-flash-8b)Banco de Dados: Google Sheets API (gspread + google-auth)Scraping & Rede: requests, BeautifulSoup4, cloudscraper, API TinyURLProcessamento de Imagem: Pillow (PIL) — geração de layouts, tipografia e smart crop anti-marca d'águaSíntese de Voz & Vídeo: edge-tts (Vozes Neurais Microsoft PT-BR + extração de timestamps .vtt) e FFmpeg (renderização libx264, filtro zoompan, split-screen e queima de legendas .ass)Mensageria & Controle: Telegram Bot API (sendPhoto, sendVideo, sendMessage, inline_keyboard, getUpdates)📊 Estrutura do Banco de Dados (BugdasPromos)A planilha possui 12 colunas (A até L) que controlam o estado de cada oferta:ColunaÍndiceNomeDescrição / Regra de NegócioA1ProdutoNome comercial limpo (extraído e higienizado pelo Gemini).B2Preco_AntigoPreço original (ou estimativa +30% caso ausente no post original).C3Preco_NovoValor promocional em destaque.D4LinkURL final da Amazon com a tag bugdaspromo07-20 encurtada via TinyURL.E5Imagem_URLURL pública da foto do produto extraída do CDN do Telegram.F6StatusControle do Canal Telegram ("" = Pendente, "Postado", ou "Erro: ...").G7CupomCódigo de cupom promocional (se aplicável).H8CondicaoCondição de pagamento (ex: no PIX, parcelado).I9Postar_InstaFiltro de qualidade: FEED, STORY ou NAO.J10Formato_InstaRegra de intercalação automática: alterna entre FOTO e VIDEO.K11Status_InstaMáquina de estados do Instagram (Aguardando Geracao, Em Aprovacao, Regerar|<pedido>, Aprovado, Descartado).L12Legenda_InstaLegenda gerada pela IA focada em conversão para o link da Bio.🧠 Regras de Negócio & Diretrizes dos Agentes1. Agente Caçador (hunter_promos.py)Bypass de Firewall (Anti-403): Nunca raspar sites protegidos por Cloudflare (ex: Pelando/Hardmob) diretamente de IPs da Azure/GitHub. Utilizar sempre a visualização web pública de canais do Telegram (https://t.me/s/<canal>).Filtro Exclusivo Amazon ("Filtro de Ouro"): Seguir até 8 saltos de redirecionamento HTTP (301/302) e inspecionar o HTML em caso de páginas de espera (timers). Se o destino final não contiver amazon.com.br, descartar o produto imediatamente.Injeção de Afiliado: Extrair o ASIN de 10 dígitos (/(?:dp|gp/product)/([A-Z0-9]{10})) e reconstruir a URL limpa (https://www.amazon.com.br/dp/{ASIN}?tag=bugdaspromo07-20) antes de encurtar no TinyURL.Curadoria de Instagram:FEED: Exclusivo para itens de alto desejo (eletrônicos, eletrodomésticos, marcas fortes, ticket > R$ 80).STORY: Achadinhos baratos ou itens simples.NAO: Produtos sem apelo visual para o Instagram.2. Agente Publicador do Telegram (bot_promos_sheets.py)Remoção de Marca d'Água Concorrente: Baixar a imagem em memória (BytesIO) e aplicar corte superior de 15% (img.crop((0, int(alt * 0.15), larg, alt))) para remover cabeçalhos/avatares de prints de outros canais.Blindagem de Upload: Enviar o binário da imagem via multipart/form-data para a API do Telegram (evitando o erro Bad Request: failed to get HTTP URL content).Saneamento Monetário: Sempre passar os valores brutos pela função formatar_preco() para garantir a máscara R$ X.XXX,XX.3. Agente Gerenciador de Instagram (insta_manager.py)Trava Anti-Ticket Baixo no Reels: Produtos abaixo de R$ 45,00 nunca devem gerar VIDEO para o Feed; são convertidos automaticamente em FOTO para STORY.Conversão para o Telegram: É proibido usar chamadas do tipo "Comente EU QUERO que mando no Direct". Toda CTA visual, legenda e locução deve direcionar exclusivamente para o Grupo do Telegram no Link da Bio.Pronúncia Monetária Perfeita (preco_para_fala): Antes de enviar o roteiro ao edge-tts, converter strings numéricas (R$ 29,69) para texto por extenso (29 reais e 69 centavos), impedindo que o sintetizador leia números inteiros na casa dos milhares.Reels de Alta Retenção (Estilo Rick / Split-Screen):Ganchos Incisivos: Roteiros devem iniciar com quebra de expectativa (ex: "Nunca pague preço cheio...").Legendas Dinâmicas: Converter o .vtt gerado pelo edge-tts em .ass com fonte amarela (&H0000EAFF), borda preta grossa e alinhamento central.Fundo Dinâmico: Caso exista bg_minecraft.mp4 na raiz do repositório, renderizar em Split-Screen com o Card do produto no topo; caso contrário, aplicar filtro zoompan contínuo.Fluxo Human-in-the-Loop (Aprovação via Telegram):Ler getUpdates no início de cada execução.Cliques em aprovar_<linha> ou rejeitar_<linha> atualizam a Coluna K.Replies (Respostas de texto) à prévia enviam o feedback para o Gemini, atualizam os dados da planilha e mudam o status para Regerar|<instrucao>.📂 Estrutura de Arquivos do RepositórioPlaintext📦 botdaspromos
 ┣ 📂 .github/workflows
 ┃ ┗ 📜 main.yml              # Configuração do Cron Job (30 min), apt-get ffmpeg e pip
 ┣ 📜 credentials.json        # Credenciais da Service Account do Google Cloud
 ┣ 📜 hunter_promos.py        # Agente 1: Scraping, Filtro Amazon e Classificação IA
 ┣ 📜 bot_promos_sheets.py    # Agente 2: Disparo no canal público @OBugdasPromos
 ┣ 📜 insta_manager.py        # Agente 3: Gerador de Cards/Reels + Botões de Aprovação
 ┣ 📜 bg_minecraft.mp4        # (Opcional) Vídeo vertical de fundo para Reels estilo retenção
 ┗ 📜 AGENT.md                # Documentação de arquitetura e regras de negócio
🚀 Guia Rápido de Operação e ManutençãoForçar Nova Geração de Arte/Vídeo no Instagram:Abra a planilha BugdasPromos, garanta que a coluna Postar_Insta está como FEED (ou SIM) e apague o conteúdo da célula Status_Insta (deixe em branco). Na próxima execução da Action, a mídia será recriada do zero.Reenviar Oferta no Canal do Telegram:Apague a palavra "Postado" da coluna Status (Coluna F) na linha desejada.Pedir Edição pelo Celular:No chat privado com o bot no Telegram, deslize a mensagem da prévia para a direita (Responder/Reply) e digite o comando natural (ex: "Muda o preço novo para 99,90 e destaca que é frete grátis Prime").