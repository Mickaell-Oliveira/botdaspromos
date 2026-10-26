import os
import re
import json
import time
import requests
import cloudscraper
import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from google import genai
from urllib.parse import urlparse, quote

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
GEMINI_API_KEY = (os.getenv('GEMINI_API_KEY') or '').strip()
NOME_DA_PLANILHA = (os.getenv('NOME_DA_PLANILHA') or 'BugdasPromos').strip()
AMAZON_TAG = (os.getenv('AMAZON_TAG') or 'bugdaspromo07-20').strip()
DESCONTO_MINIMO_PCT = 12.0
PRECO_MINIMO_INSTA = 80.0

TERMOS_BLOQUEADOS_INSTA = [
    'pilha', 'bateria', 'cr2032', 'alcalina', 'papel higiênico', 'papel higienico',
    'sabão', 'sabao', 'amaciante', 'detergente', 'desinfetante', 'esponja',
    'fralda', 'lenço umedecido', 'lenco umedecido', 'absorvente', 'desodorante',
    'shampoo', 'condicionador', 'sabonete', 'pasta de dente', 'creme dental',
    'ração', 'racao', 'areia sanitária', 'tapete higiênico', 'antipulgas',
    'cápsula', 'capsula', 'ômega', 'omega', 'óleo de chia', 'vitamina',
    'cabo usb', 'cabo hdmi', 'película', 'pelicula', 'capinha', 'adaptador',
    'cartucho', 'toner', 'papel a4', 'resma', 'caneta', 'lápis',
    'lâmpada', 'lampada', 'fita isolante', 'fita dupla face', 'extensão',
    'cueca', 'meia ', 'meias ', 'calcinha'
]

CANAIS_FONTE = [
    'pechinchou',
    'promobit',
    'canaltech_ofertas',
    'descontos_top',
    'ofertasdecomputador',
    'chinasuperofertas',
    'achados_amazon_br',
    'economize'
]

LOJAS_CONCORRENTES = [
    'mercadolivre', 'mercado livre', 'meli.la', 'melhordoml',
    'shopee', 'shp.ee', 'magalu', 'magazine luiza', 'magazinevoce',
    'aliexpress', 's.click.aliexpress', 'kabum', 'pichau',
    'terabyteshop', 'casas bahia', 'casasbahia', 'pontofrio',
    'carrefour', 'fastshop', 'netshoes', 'centauro', 'shein', 'nike.com'
]

CUPONS_INVALIDOS = {
    'NENHUM', 'VAZIO', 'SEM CUPOM', 'NAO', 'NÃO', 'NONE', 'NULL',
    'PRIME', 'NO APP', 'APP', 'PIX', 'NO PIX', 'APLICAR', 'ATIVAR'
}

MODELOS_GEMINI = ['gemini-3.8-flash-lite', 'gemini-3.8-flash']
MODELOS_INDISPONIVEIS = set()

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
scraper_amz = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7'
}

# ==========================================
# 2. CONEXÃO COM GOOGLE SHEETS
# ==========================================
escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

google_creds = (os.getenv("GOOGLE_CREDENTIALS") or "").strip()
if google_creds:
    credenciais = Credentials.from_service_account_info(json.loads(google_creds), scopes=escopos)
elif os.path.exists("credentials.json"):
    credenciais = Credentials.from_service_account_file("credentials.json", scopes=escopos)
else:
    raise FileNotFoundError("Credenciais do Google não encontradas! Defina a variável GOOGLE_CREDENTIALS ou mantenha o arquivo credentials.json.")

cliente_sheets = gspread.authorize(credenciais)
planilha = cliente_sheets.open(NOME_DA_PLANILHA).sheet1

# ==========================================
# 3. FUNÇÕES AUXILIARES DE PREÇO E VALIDAÇÃO
# ==========================================
def preco_para_float(valor_str):
    """Converte string monetária ('R$ 1.749,00' ou '1749,00') para float."""
    if not valor_str:
        return 0.0
    s = str(valor_str).replace("R$", "").replace(" ", "").strip()
    s = re.sub(r'[^\d.,]', '', s)
    if not s:
        return 0.0
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    elif '.' in s:
        partes = s.split('.')
        if len(partes[-1]) == 3 and len(partes) > 1:
            s = s.replace('.', '')
    try:
        return float(s)
    except ValueError:
        return 0.0

def float_para_str_planilha(valor_float):
    """Formata float como '1749,00' para manter padrão da planilha."""
    if not valor_float or valor_float <= 0:
        return ""
    return f"{valor_float:.2f}".replace('.', ',')

def limpar_cupom(cupom_raw):
    """Valida se o cupom é real e não pertence a lojas concorrentes."""
    if not cupom_raw:
        return ""
    c = str(cupom_raw).strip().upper()
    if c in CUPONS_INVALIDOS or len(c) < 3 or len(c) > 25:
        return ""
    for concorrente in ['ML', 'MELI', 'SHOPEE', 'MAGALU', 'ALI', 'KABUM']:
        if concorrente in c:
            return ""
    return c

def limpar_titulo_produto(titulo_bruto):
    """Higieniza o nome comercial do produto em Python sem precisar de IA."""
    if not titulo_bruto:
        return ""
    t = re.sub(r'https?://\S+', '', str(titulo_bruto))
    # Remove emojis e símbolos decorativos iniciais
    t = re.sub(r'^[^\w\dÀ-ÿ]+', '', t).strip()
    # Corta sufixos muito longos após hífen ou vírgula se já tiver tamanho suficiente
    if len(t) > 55 and ' - ' in t:
        parte = t.split(' - ')[0].strip()
        if len(parte) >= 18:
            t = parte
    if len(t) > 55:
        t = t[:55].rsplit(' ', 1)[0].strip()
    return t

def eh_post_de_concorrente(texto, links):
    """Descarta rapidamente posts que mencionam lojas concorrentes no texto ou nos links."""
    texto_lower = texto.lower()
    links_lower = " ".join(links).lower()
    combinado = f"{texto_lower} {links_lower}"

    if 'amazon' not in combinado and 'amzn.to' not in combinado:
        for loja in LOJAS_CONCORRENTES:
            if loja in combinado:
                return True
    return False

def validar_oferta_e_desconto(dados):
    """
    Garante que o preço é válido e que a oferta tem:
    - Pelo menos DESCONTO_MINIMO_PCT% de desconto real (preco_antigo -> preco_novo), OU
    - Um cupom de desconto válido.
    Retorna (aprovado: bool, motivo: str, desconto_pct: float, cupom_limpo: str)
    """
    preco_novo = preco_para_float(dados.get('preco_novo'))
    preco_antigo = preco_para_float(dados.get('preco_antigo'))
    cupom = limpar_cupom(dados.get('cupom'))

    if preco_novo <= 0:
        return False, "Preço promocional inválido ou ausente", 0.0, ""

    desconto_pct = 0.0
    if preco_antigo > preco_novo:
        desconto_pct = ((preco_antigo - preco_novo) / preco_antigo) * 100.0
        if desconto_pct >= 95.0:
            return False, f"Desconto irreal detectado ({desconto_pct:.1f}%)", desconto_pct, cupom

    if desconto_pct >= DESCONTO_MINIMO_PCT:
        return True, f"Desconto real de {desconto_pct:.1f}%", desconto_pct, cupom

    if cupom:
        return True, f"Cupom de desconto encontrado ({cupom})", desconto_pct, cupom

    if desconto_pct > 0:
        return False, f"Desconto de apenas {desconto_pct:.1f}% (mínimo exigido: {DESCONTO_MINIMO_PCT:.0f}% ou cupom)", desconto_pct, ""
    return False, f"Sem preço antigo para comprovar {DESCONTO_MINIMO_PCT:.0f}% de desconto e sem cupom", 0.0, ""

# ==========================================
# 4. CAPTURA DIRETA DO TELEGRAM WEB
# ==========================================
def extrair_blocos_html_telegram(html_texto, canal_limpo, limite=10):
    ofertas = []
    soup = BeautifulSoup(html_texto, 'html.parser')
    blocos_msg = soup.select('div.tgme_widget_message_wrap')
    if not blocos_msg:
        return ofertas

    for bloco in reversed(blocos_msg[-limite:]):
        div_texto = bloco.select_one('div.tgme_widget_message_text')
        if not div_texto:
            continue

        texto_msg = div_texto.get_text(separator='\n').strip()
        links_encontrados = [
            a['href'] for a in bloco.select('div.tgme_widget_message_text a[href], a.tgme_widget_message_inline_button[href]')
            if a['href'].startswith('http') and 't.me/' not in a['href']
        ]

        if not links_encontrados:
            continue

        imagem_url = "https://placehold.co/600x600/111111/FFCC00/png?text=OFERTA+IMPERDIVEL"
        for seletor in ['a.tgme_widget_message_photo_wrap', 'i.link_preview_image', 'a.link_preview_right_image']:
            elem_foto = bloco.select_one(seletor)
            if elem_foto and elem_foto.get('style'):
                match_bg = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", elem_foto['style'])
                if match_bg:
                    imagem_url = match_bg.group(1)
                    break

        ofertas.append({
            'canal': canal_limpo,
            'texto': texto_msg,
            'links': links_encontrados,
            'imagem': imagem_url
        })
    return ofertas

def buscar_ofertas_telegram():
    ofertas_brutas = []
    textos_vistos = set()

    for canal in CANAIS_FONTE:
        canal_limpo = canal.replace('https://t.me/s/', '').replace('https://t.me/', '').replace('@', '').strip()
        urls_consulta = [
            (f"https://t.me/s/{canal_limpo}", 10),
            (f"https://t.me/s/{canal_limpo}?q=amzn.to", 6)
        ]

        for url, limite in urls_consulta:
            try:
                if "?q=" not in url:
                    print(f"📡 Lendo canal público: @{canal_limpo}...")
                resp = requests.get(url, headers=HEADERS, timeout=10)
                if resp.status_code != 200:
                    continue

                extraidas = extrair_blocos_html_telegram(resp.text, canal_limpo, limite=limite)
                for item in extraidas:
                    chave = item['texto'][:80].lower()
                    if chave not in textos_vistos:
                        textos_vistos.add(chave)
                        ofertas_brutas.append(item)
            except Exception as e:
                print(f"   ⚠️ Erro ao ler {canal_limpo}: {e}")

    return ofertas_brutas

# ==========================================
# 5. FILTRO DE OURO: APENAS AMAZON (BLINDADO)
# ==========================================
def encurtar_url(url_longa):
    try:
        api_tinyurl = f"https://tinyurl.com/api-create.php?url={quote(url_longa, safe='')}"
        resposta = requests.get(api_tinyurl, timeout=6)
        if resposta.status_code == 200 and resposta.text.strip().startswith("http"):
            return resposta.text.strip()
    except Exception:
        pass
    return url_longa

def extrair_asin_amazon(url):
    if not url or "amazon.com.br" not in url.lower():
        return None
    match_asin = re.search(r'/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN)/([A-Z0-9]{10})', url, re.IGNORECASE)
    return match_asin.group(1).upper() if match_asin else None

def url_eh_de_concorrente(url):
    u = url.lower()
    dominios_bloqueados = [
        'mercadolivre.com', 'mercadolivre.com.br', 'meli.la',
        'shopee.com.br', 'shp.ee', 'magazineluiza.com.br', 'magazinevoce.com.br',
        'aliexpress.com', 'kabum.com.br', 'pichau.com.br', 'terabyteshop.com.br',
        'casasbahia.com.br', 'pontofrio.com.br', 'carrefour.com.br', 'fastshop.com.br'
    ]
    return any(d in u for d in dominios_bloqueados)

def extrair_link_principal_de_agregador(html_texto):
    """
    Extrai apenas o link de redirecionamento principal de páginas de agregadores
    (evitando capturar links de 'Outras ofertas' na barra lateral ou rodapé).
    """
    match_meta = re.search(r'http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=(https?://[^"\'\s>]+)', html_texto, re.IGNORECASE)
    if match_meta:
        return match_meta.group(1)

    match_loc = re.search(r'(?:window\.location(?:\.href|\.replace)?|location\.href)\s*[=(]\s*["\'](https?://[^"\']+)["\']', html_texto)
    if match_loc:
        return match_loc.group(1)

    soup = BeautifulSoup(html_texto, 'html.parser')
    for tag_lixo in soup.select('aside, footer, nav, .sidebar, .related, .outras-ofertas, .recommendations'):
        tag_lixo.decompose()

    titulo_pag = (soup.title.string if soup.title else "").lower()
    if any(loja in titulo_pag for loja in ['mercado livre', 'shopee', 'magalu', 'aliexpress', 'kabum']):
        return None

    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'amzn.to/' in href or ('amazon.com.br' in href and extrair_asin_amazon(href)):
            return href

    return None

def resolver_e_afiliar_link(url_inicial):
    """
    Segue redirecionamentos e retorna (link_afiliado_curto, asin, url_amazon_limpa).
    Retorna (None, None, None) se não for um produto válido da Amazon.
    """
    url_atual = url_inicial
    try:
        for _ in range(8):
            if url_eh_de_concorrente(url_atual):
                return None, None, None

            if "amazon.com.br" in url_atual.lower():
                break

            resp = requests.get(url_atual, headers=HEADERS, allow_redirects=False, timeout=8)

            if resp.status_code in (301, 302, 303, 307, 308):
                prox_url = resp.headers.get('Location', '')
                if not prox_url:
                    break
                if prox_url.startswith('/'):
                    parsed = urlparse(url_atual)
                    prox_url = f"{parsed.scheme}://{parsed.netloc}{prox_url}"
                url_atual = prox_url
            else:
                prox_link = extrair_link_principal_de_agregador(resp.text)
                if prox_link and prox_link != url_atual:
                    url_atual = prox_link
                    continue
                break

        if "amazon.com.br" in url_atual.lower():
            asin = extrair_asin_amazon(url_atual)
            if not asin:
                return None, None, None

            link_com_tag = f"https://www.amazon.com.br/dp/{asin}?tag={AMAZON_TAG}"
            return encurtar_url(link_com_tag), asin, f"https://www.amazon.com.br/dp/{asin}"

        return None, None, None
    except Exception:
        return None, None, None

def consultar_pagina_amazon(asin):
    """
    Usa cloudscraper para buscar título real, foto limpa, preço atual, preço antigo,
    porcentagem de desconto (.savingsPercentage) e cupom on-page direto na Amazon Brasil.
    """
    info = {
        "titulo_amazon": "",
        "imagem_amazon": "",
        "preco_atual_amz": "",
        "preco_antigo_amz": "",
        "desconto_pct_amz": 0.0,
        "cupom_amz": ""
    }
    try:
        url = f"https://www.amazon.com.br/dp/{asin}"
        resp = scraper_amz.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return info

        soup = BeautifulSoup(resp.text, 'html.parser')

        elem_titulo = soup.select_one('#productTitle, #title')
        if elem_titulo:
            info["titulo_amazon"] = elem_titulo.get_text(strip=True)

        elem_img = soup.select_one('#landingImage, #imgBlkFront, #main-image')
        if elem_img:
            img_src = elem_img.get('data-old-hires') or elem_img.get('src') or ''
            if img_src.startswith('http'):
                info["imagem_amazon"] = img_src

        elem_preco = soup.select_one(
            '.priceToPay .a-offscreen, .apexPriceToPay .a-offscreen, '
            '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, '
            '#corePrice_feature_div .a-price .a-offscreen, '
            '#priceblock_ourprice, #priceblock_dealprice'
        )
        if elem_preco:
            info["preco_atual_amz"] = elem_preco.get_text(strip=True)

        elem_antigo = soup.select_one(
            '.basisPrice .a-offscreen, span[data-a-strike="true"] .a-offscreen, '
            '.a-text-price .a-offscreen, #listPrice, #priceblock_saleprice_lbl'
        )
        if elem_antigo:
            info["preco_antigo_amz"] = elem_antigo.get_text(strip=True)

        # Captura tag explícita de porcentagem de desconto na Amazon (ex: "-28%")
        elem_pct = soup.select_one('.savingsPercentage, .reinventPriceSavingsPercentageMargin')
        if elem_pct:
            match_pct = re.search(r'(\d{1,2})\s*%', elem_pct.get_text())
            if match_pct:
                info["desconto_pct_amz"] = float(match_pct.group(1))

        # Captura cupom aplicável na própria página da Amazon
        elem_cupom = soup.select_one('#couponBadgeRegularVpc, .promoPriceBlockMessage, label[id*="coupon"]')
        if elem_cupom:
            txt_c = elem_cupom.get_text(" ", strip=True)
            match_c = re.search(r'(\d{1,2}%\s*off|R\$\s*\d+(?:,\d{2})?\s*off)', txt_c, re.IGNORECASE)
            if match_c:
                info["cupom_amz"] = f"NO APP ({match_c.group(1).upper()})"
    except Exception:
        pass
    return info

# ==========================================
# 6. EXTRAÇÃO EM PYTHON PRIMEIRO + IA EM LOTE
# ==========================================
PISOS_CATEGORIA_PRECO = [
    (["ar condicionado", "ar-condicionado", "split inverter"], 1100.0),
    (["geladeira", "refrigerador", "freezer", "lava e seca", "lava-loucas", "lava louças"], 1100.0),
    (["lavadora de roupas", "maquina de lavar", "máquina de lavar", "fogao 4 bocas", "fogão 4 bocas", "fogao 5 bocas", "fogão 5 bocas"], 700.0),
    (["smart tv", "televisao", "televisão", "tv 50", "tv 55", "tv 65", "tv 43", "tv 32"], 650.0),
    (["notebook", "macbook", "playstation 5", "ps5", "xbox series", "nintendo switch"], 1200.0),
    (["iphone", "galaxy s2", "galaxy s3", "smartphone", "celular"], 550.0),
    (["placa de video", "placa de vídeo", "rtx ", "rx 6", "rx 7"], 700.0),
    (["micro-ondas", "microondas", "forno eletrico", "forno elétrico"], 280.0),
    (["air fryer", "fritadeira", "aspirador robo", "aspirador robô", "monitor"], 180.0),
]

TERMOS_ACESSORIO_OU_PECA = [
    "controle", "capa", "suporte", "filtro", "defletor", "peca", "peça", "cabo",
    "mangueira", "sensor", "placa fonte", "capacitor", "adesivo", "pelicula", "película",
    "protetor", "bolsa", "estojo", "case", "fonte carregador", "pilha", "bateria"
]

def validar_coerencia_produto_preco(titulo, preco_novo_val, p_novo_amz=0.0):
    """
    Impede promoções falsas/absurdas (ex: 'Ar Condicionado Split Inverter' por R$ 59,90
    quando na verdade o preço é de um controle remoto/acessório ou parcela solta).
    """
    if not titulo or preco_novo_val <= 0:
        return False, "Sem título ou preço"

    t_lower = str(titulo).lower()

    # 1. Se a própria página da Amazon retornou um preço atual e o preço extraído do post
    # é menos de 40% do preço real na Amazon (ex: pegou valor da parcela "10x de R$ 59,90" ou frete), corrige ou descarta!
    if p_novo_amz > 0:
        if preco_novo_val < (p_novo_amz * 0.45) and (p_novo_amz - preco_novo_val) > 150.0:
            return False, f"Preço extraído (R$ {preco_novo_val:.2f}) diverge drasticamente do preço na Amazon (R$ {p_novo_amz:.2f})"

    # 2. Se o título é de um eletrodoméstico/eletrônico caro (sem ser acessório/peça),
    # verifica se o preço não é absurdo para a categoria:
    eh_acessorio = any(termo in t_lower for termo in TERMOS_ACESSORIO_OU_PECA)
    if not eh_acessorio:
        for palavras_cat, piso_minimo in PISOS_CATEGORIA_PRECO:
            if any(p in t_lower for p in palavras_cat):
                if preco_novo_val < piso_minimo:
                    return False, f"Preço inverossímil para '{titulo[:30]}' (R$ {preco_novo_val:.2f} < mínimo R$ {piso_minimo:.0f})"

    return True, "OK"

def avaliar_vitrine_insta(titulo, preco_novo_val):
    """
    Seleciona para a vitrine do Instagram APENAS produtos de desejo com ticket >= R$ 80,00,
    bloqueando consumíveis, pilhas, cabos simples, higiene, limpeza e miudezas.
    """
    if not preco_novo_val or preco_novo_val < PRECO_MINIMO_INSTA:
        return "NAO"
    ok_coerencia, _ = validar_coerencia_produto_preco(titulo, preco_novo_val)
    if not ok_coerencia:
        return "NAO"
    t_lower = str(titulo).lower()
    for termo in TERMOS_BLOQUEADOS_INSTA:
        if termo in t_lower:
            return "NAO"
    return "SIM"

def extrair_dados_python(texto_post, info_amazon):
    """
    Extrai título, preços, desconto e cupom 100% em Python (0 chamadas de IA)
    combinando o texto do post no Telegram com os dados da página da Amazon.
    """
    # 1. Nome do Produto
    titulo = (info_amazon.get("titulo_amazon") or "").strip()
    if not titulo:
        linhas = [l.strip() for l in texto_post.splitlines() if l.strip()]
        for l in linhas:
            l_limpa = re.sub(r'https?://\S+', '', l).strip()
            if len(l_limpa) >= 12 and not any(g in l_limpa.upper() for g in ['CORRE', 'MENOR PREÇO', 'RÁPIDO', 'CUPOM', 'LINK', 'R$']):
                titulo = l_limpa
                break
        if not titulo and linhas:
            titulo = re.sub(r'https?://\S+', '', linhas[0]).strip()
    titulo = limpar_titulo_produto(titulo)

    # 2. Extração de Preços do Texto (remove antes trechos de parcelamento "10x de R$ 59,90" ou "R$ 50 OFF" para não confundir com o preço final)
    texto_sem_parcelas = re.sub(
        r'\d{1,2}\s*x\s*(?:de\s*)?R\$\s*(?:\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)',
        ' ',
        texto_post,
        flags=re.IGNORECASE
    )
    texto_sem_parcelas = re.sub(
        r'R\$\s*(?:\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)\s*(?:off|de\s*desconto)',
        ' ',
        texto_sem_parcelas,
        flags=re.IGNORECASE
    )

    matches_r = re.findall(r'R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)', texto_sem_parcelas, re.IGNORECASE)
    valores = [preco_para_float(m) for m in matches_r if preco_para_float(m) > 0]

    # Procura padrão "De X por Y" mesmo quando o primeiro não tem R$
    match_de_por = re.search(
        r'de\s*:?\s*(?:R\$\s*)?~?(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)~?\s*(?:por|->|➡|👉|\n)+\s*(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)',
        texto_sem_parcelas,
        re.IGNORECASE
    )

    p_novo_amz = preco_para_float(info_amazon.get("preco_atual_amz"))
    p_antigo_amz = preco_para_float(info_amazon.get("preco_antigo_amz"))
    pct_amz = float(info_amazon.get("desconto_pct_amz") or 0.0)

    preco_novo_val = 0.0
    preco_antigo_val = 0.0

    if match_de_por:
        v1 = preco_para_float(match_de_por.group(1))
        v2 = preco_para_float(match_de_por.group(2))
        if v1 > v2 > 0:
            preco_antigo_val, preco_novo_val = v1, v2

    if preco_novo_val <= 0:
        if len(valores) >= 2:
            preco_antigo_val = max(valores[:2])
            preco_novo_val = min(valores[:2])
        elif len(valores) == 1:
            preco_novo_val = valores[0]
        elif p_novo_amz > 0:
            preco_novo_val = p_novo_amz

    # Complementa o preço antigo com os dados oficiais da Amazon se não estava no texto
    if preco_antigo_val <= preco_novo_val and p_antigo_amz > preco_novo_val > 0:
        preco_antigo_val = p_antigo_amz

    # Se o post ou a página da Amazon informou porcentagem de desconto explícita (ex: "25% OFF"), calcula o preço antigo correspondente
    if preco_antigo_val <= preco_novo_val and preco_novo_val > 0:
        match_pct_post = re.search(r'(\d{2})\s*%\s*(?:off|de\s*desconto)', texto_post, re.IGNORECASE)
        pct_ref = float(match_pct_post.group(1)) if match_pct_post else pct_amz
        if 15.0 <= pct_ref <= 85.0:
            preco_antigo_val = round(preco_novo_val / (1.0 - (pct_ref / 100.0)), 2)

    # 3. Cupom
    cupom = ""
    match_cupom = re.search(r'cupom\s*[:\-]?\s*["\'`]*([A-Z0-9]{4,20})["\'`]*', texto_post, re.IGNORECASE)
    if match_cupom:
        cupom = limpar_cupom(match_cupom.group(1))

    # 4. Condição
    condicao = "no PIX" if "pix" in texto_post.lower() else ("à vista" if "vista" in texto_post.lower() else "")

    if not titulo or preco_novo_val <= 0:
        return None

    # 5. Trava de Veracidade Produto x Preço (barra ex: Ar Condicionado por R$ 59,90 ou divergência grave com a Amazon)
    coerente, motivo_coerencia = validar_coerencia_produto_preco(titulo, preco_novo_val, p_novo_amz)
    if not coerente:
        print(f"   🚫 BLOQUEIO DE VERACIDADE: {motivo_coerencia}")
        return None

    return {
        "valido": True,
        "produto": titulo,
        "preco_antigo": float_para_str_planilha(preco_antigo_val),
        "preco_novo": float_para_str_planilha(preco_novo_val),
        "cupom": cupom,
        "condicao": condicao,
        "postar_insta": avaliar_vitrine_insta(titulo, preco_novo_val)
    }

def refinar_lote_com_ia(ofertas_aprovadas):
    """
    Faz 1 ÚNICA chamada em lote (Batch) ao Gemini APENAS para as ofertas que
    JÁ PASSARAM no filtro de >= 20% OFF ou cupom!
    Se a cota diária estiver esgotada (429), mantém os dados limpos pelo Python.
    """
    if not ofertas_aprovadas:
        return ofertas_aprovadas

    itens_prompt = []
    for idx, item in enumerate(ofertas_aprovadas):
        itens_prompt.append({
            "id": idx,
            "titulo_bruto": item["dados"]["produto"],
            "titulo_amazon": item["info_amz"].get("titulo_amazon", ""),
            "preco_novo": item["dados"]["preco_novo"]
        })

    prompt = f"""
    Refine os nomes comerciais desta lista de produtos aprovados da Amazon Brasil (máximo 52 caracteres cada, limpo e atraente).
    Para o campo "postar_insta", retorne "SIM" SOMENTE se o produto custar a partir de R$ 80,00 E for um item de alto desejo/interesse para vitrine (Eletrônicos, Eletrodomésticos, Eletroportáteis, Gamer, Hardware, Casa/Cozinha moderna, Áudio, TV, Smartphones). Retorne "NAO" para consumíveis, baterias/pilhas, cabos, itens básicos de mercado/limpeza/higiene ou produtos baratos.
    Lista: {json.dumps(itens_prompt, ensure_ascii=False)}

    Retorne APENAS um JSON válido no formato de lista:
    [
        {{"id": 0, "produto": "Nome Comercial Limpo", "postar_insta": "SIM"}}
    ]
    """

    for modelo in MODELOS_GEMINI:
        if modelo in MODELOS_INDISPONIVEIS:
            continue
        try:
            resposta = cliente_gemini.models.generate_content(
                model=modelo,
                contents=prompt,
                config={'response_mime_type': 'application/json'}
            )
            lista_refinada = json.loads(resposta.text.strip().replace('```json', '').replace('```', '').strip())
            if isinstance(lista_refinada, list):
                for ref in lista_refinada:
                    idx = ref.get("id")
                    if isinstance(idx, int) and 0 <= idx < len(ofertas_aprovadas):
                        if ref.get("produto"):
                            ofertas_aprovadas[idx]["dados"]["produto"] = limpar_titulo_produto(ref["produto"])
                        p_val = preco_para_float(ofertas_aprovadas[idx]["dados"]["preco_novo"])
                        decisao_py = avaliar_vitrine_insta(ofertas_aprovadas[idx]["dados"]["produto"], p_val)
                        if decisao_py == "NAO":
                            ofertas_aprovadas[idx]["dados"]["postar_insta"] = "NAO"
                        elif ref.get("postar_insta") in ("SIM", "NAO"):
                            ofertas_aprovadas[idx]["dados"]["postar_insta"] = ref["postar_insta"]
            print(f"   ✨ Lote de {len(ofertas_aprovadas)} oferta(s) refinado com 1 única chamada ({modelo})!")
            return ofertas_aprovadas
        except Exception as e:
            MODELOS_INDISPONIVEIS.add(modelo)
            print(f"   ℹ️ Modelo {modelo} sem cota/indisponível no momento — mantendo higienização nativa do Python.")

    return ofertas_aprovadas

# ==========================================
# 7. EXECUÇÃO PRINCIPAL
# ==========================================
def executar_hunter():
    print(f"🏹 Iniciando o Hunter de Promoções (Exclusivo Amazon | Mínimo {DESCONTO_MINIMO_PCT:.0f}% OFF ou Cupom)...")
    ofertas = buscar_ofertas_telegram()
    print(f"📦 Total de mensagens brutas coletadas nos canais: {len(ofertas)}")

    registros_atuais = planilha.get_all_records(numericise_ignore=['all'])
    produtos_ja_cadastrados = [str(linha.get('Produto', '')).lower() for linha in registros_atuais]
    links_ja_cadastrados = [str(linha.get('Link', '')).strip() for linha in registros_atuais]
    asins_processados = set()
    candidatas_aprovadas = []
    analises_amazon = 0

    for oferta in ofertas:
        if analises_amazon >= 12 or len(candidatas_aprovadas) >= 3:
            break

        resumo_post = oferta['texto'].splitlines()[0][:65] if oferta['texto'] else "Sem título"

        # 1. Pré-filtro instantâneo contra lojas concorrentes
        if eh_post_de_concorrente(oferta['texto'], oferta['links']):
            continue

        # 2. Resolve o link para garantir que é da Amazon
        link_final_curto = None
        asin_encontrado = None
        for link_candidato in oferta['links']:
            link_curto, asin, _ = resolver_e_afiliar_link(link_candidato)
            if link_curto and asin:
                link_final_curto = link_curto
                asin_encontrado = asin
                break

        if not link_final_curto or not asin_encontrado:
            continue

        if asin_encontrado in asins_processados or link_final_curto in links_ja_cadastrados:
            continue
        asins_processados.add(asin_encontrado)
        analises_amazon += 1

        print(f"\n🔎 Oferta Amazon detectada ({asin_encontrado}): {resumo_post}...")

        # 3. Consulta dados oficiais na página da Amazon (título, foto limpa, preço e % OFF)
        info_amz = consultar_pagina_amazon(asin_encontrado)

        # 4. Extrai dados e valida desconto em Python ANTES de gastar qualquer cota da IA!
        dados = extrair_dados_python(oferta['texto'], info_amz)
        if not dados or not dados.get('valido') or not dados.get('preco_novo'):
            print("   ⏭️ DESCARTADO: Não foi possível identificar preço promocional.")
            continue

        aprovada, motivo, desconto_pct, cupom_limpo = validar_oferta_e_desconto(dados)
        if not aprovada:
            print(f"   ⏭️ DESCARTADO ({motivo}): {dados.get('produto')}")
            continue

        nome_preliminar = dados['produto'].strip()
        if any(nome_preliminar.lower()[:18] in prod for prod in produtos_ja_cadastrados if prod):
            print(f"   ⏭️ Pulando: '{nome_preliminar}' já cadastrado na planilha.")
            continue

        candidatas_aprovadas.append({
            "oferta": oferta,
            "info_amz": info_amz,
            "dados": dados,
            "link_final_curto": link_final_curto,
            "motivo": motivo,
            "cupom_limpo": cupom_limpo
        })

    # 5. Faz no máximo 1 chamada em lote ao Gemini apenas se houver ofertas aprovadas!
    if candidatas_aprovadas:
        candidatas_aprovadas = refinar_lote_com_ia(candidatas_aprovadas)

    novas_adicionadas = 0
    for item in candidatas_aprovadas:
        dados = item["dados"]
        info_amz = item["info_amz"]
        oferta = item["oferta"]
        nome_produto = dados["produto"].strip()

        preco_antigo_fmt = float_para_str_planilha(preco_para_float(dados.get('preco_antigo')))
        preco_novo_fmt = float_para_str_planilha(preco_para_float(dados.get('preco_novo')))
        imagem_final = info_amz.get("imagem_amazon") or oferta['imagem']

        vai_pro_insta = str(dados.get('postar_insta', 'SIM')).upper()
        formato_escolhido = ""
        status_insta = ""

        if vai_pro_insta == "SIM":
            formato_escolhido = "FOTO"
            status_insta = "Aguardando Geracao"
            print(f"   📸 Selecionado para o Instagram no formato: FOTO")

        nova_linha = [
            nome_produto,                  # Coluna A: Produto
            preco_antigo_fmt,              # Coluna B: Preco_Antigo
            preco_novo_fmt,                # Coluna C: Preco_Novo
            item["link_final_curto"],      # Coluna D: Link
            imagem_final,                  # Coluna E: Imagem_URL
            "",                            # Coluna F: Status (Telegram - vai para aprovação)
            item["cupom_limpo"],           # Coluna G: Cupom
            dados.get('condicao', ''),     # Coluna H: Condicao
            vai_pro_insta,                 # Coluna I: Postar_Insta
            formato_escolhido,             # Coluna J: Formato_Insta
            status_insta,                  # Coluna K: Status_Insta
            ""                             # Coluna L: Legenda_Insta
        ]

        planilha.append_row(nova_linha, value_input_option='RAW')
        produtos_ja_cadastrados.append(nome_produto.lower())
        novas_adicionadas += 1
        print(f"   ✅ APROVADA ({item['motivo']}): {nome_produto} | De R$ {preco_antigo_fmt or '--'} por R$ {preco_novo_fmt}")

    print(f"\n🎯 Caçada finalizada! {novas_adicionadas} novas ofertas verificadas da AMAZON cadastradas.")

if __name__ == "__main__":
    executar_hunter()
