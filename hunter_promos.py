import os
import re
import json
import time
import requests
import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from google import genai
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
GEMINI_API_KEY = (os.getenv('GEMINI_API_KEY') or '').strip()
NOME_DA_PLANILHA = (os.getenv('NOME_DA_PLANILHA') or 'BugdasPromos').strip()
AMAZON_TAG = (os.getenv('AMAZON_TAG') or 'bugdaspromo07-20').strip()
DESCONTO_MINIMO_PCT = 30.0

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
    'PRIME', 'NO APP', 'APP', 'PIX', 'NO PIX'
}

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
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

def eh_post_de_concorrente(texto, links):
    """Descarta rapidamente posts que mencionam lojas concorrentes no texto ou nos links."""
    texto_lower = texto.lower()
    links_lower = " ".join(links).lower()
    combinado = f"{texto_lower} {links_lower}"

    # Se menciona explicitamente outra loja e não menciona amazon/amzn
    if 'amazon' not in combinado and 'amzn.to' not in combinado:
        for loja in LOJAS_CONCORRENTES:
            if loja in combinado:
                return True
    return False

def validar_oferta_e_desconto(dados):
    """
    Garante que o preço é válido e que a oferta tem:
    - Pelo menos 30% de desconto real (preco_antigo -> preco_novo), OU
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
        # Desconfia de erros absurdos de parsing (ex: 98% de desconto por erro de digitação)
        if desconto_pct >= 95.0:
            return False, f"Desconto irreal detectado ({desconto_pct:.1f}%)", desconto_pct, cupom

    if desconto_pct >= DESCONTO_MINIMO_PCT:
        return True, f"Desconto real de {desconto_pct:.1f}%", desconto_pct, cupom

    if cupom:
        return True, f"Cupom de desconto encontrado ({cupom})", desconto_pct, cupom

    if desconto_pct > 0:
        return False, f"Desconto de apenas {desconto_pct:.1f}% (mínimo exigido: {DESCONTO_MINIMO_PCT:.0f}% ou cupom)", desconto_pct, ""
    return False, "Sem preço antigo para comprovar 30% de desconto e sem cupom", 0.0, ""

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
    # 1. Procura meta refresh ou script location
    match_meta = re.search(r'http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=(https?://[^"\'\s>]+)', html_texto, re.IGNORECASE)
    if match_meta:
        return match_meta.group(1)

    match_loc = re.search(r'(?:window\.location(?:\.href|\.replace)?|location\.href)\s*[=(]\s*["\'](https?://[^"\']+)["\']', html_texto)
    if match_loc:
        return match_loc.group(1)

    # 2. Remove áreas de barra lateral, rodapé e ofertas relacionadas antes de procurar links
    soup = BeautifulSoup(html_texto, 'html.parser')
    for tag_lixo in soup.select('aside, footer, nav, .sidebar, .related, .outras-ofertas, .recommendations'):
        tag_lixo.decompose()

    # Se a página do agregador indicar explicitamente outra loja no título/texto principal, aborta
    titulo_pag = (soup.title.string if soup.title else "").lower()
    if any(loja in titulo_pag for loja in ['mercado livre', 'shopee', 'magalu', 'aliexpress', 'kabum']):
        return None

    # Procura botões diretos de ir para a loja ou links amzn.to no conteúdo principal
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
                # Não aceita links genéricos da Amazon sem ASIN de produto
                return None, None, None

            link_com_tag = f"https://www.amazon.com.br/dp/{asin}?tag={AMAZON_TAG}"
            return encurtar_url(link_com_tag), asin, f"https://www.amazon.com.br/dp/{asin}"

        return None, None, None
    except Exception:
        return None, None, None

def consultar_pagina_amazon(asin):
    """
    Tenta buscar título real, foto limpa e preços diretamente na página da Amazon Brasil
    para validar o produto e enriquecer os dados.
    """
    info = {"titulo_amazon": "", "imagem_amazon": "", "preco_atual_amz": "", "preco_antigo_amz": ""}
    try:
        url = f"https://www.amazon.com.br/dp/{asin}"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return info

        soup = BeautifulSoup(resp.text, 'html.parser')

        elem_titulo = soup.select_one('#productTitle')
        if elem_titulo:
            info["titulo_amazon"] = elem_titulo.get_text(strip=True)

        elem_img = soup.select_one('#landingImage, #imgBlkFront')
        if elem_img:
            img_src = elem_img.get('data-old-hires') or elem_img.get('src') or ''
            if img_src.startswith('http'):
                info["imagem_amazon"] = img_src

        elem_preco = soup.select_one('.priceToPay .a-offscreen, .apexPriceToPay .a-offscreen, #priceblock_ourprice, #priceblock_dealprice')
        if elem_preco:
            info["preco_atual_amz"] = elem_preco.get_text(strip=True)

        elem_antigo = soup.select_one('.basisPrice .a-offscreen, .a-text-price .a-offscreen')
        if elem_antigo:
            info["preco_antigo_amz"] = elem_antigo.get_text(strip=True)
    except Exception:
        pass
    return info

# ==========================================
# 6. EXTRAÇÃO E VALIDAÇÃO (GEMINI + FALLBACK)
# ==========================================
def extrair_dados_fallback(texto_post, info_amazon):
    """
    Extrator determinístico via Regex + dados da Amazon caso a API do Gemini
    sofra rate-limit ou indisponibilidade temporária.
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
    titulo = titulo[:55].strip()

    # 2. Preços (do texto do post e da página da Amazon)
    matches_precos = re.findall(r'R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)', texto_post, re.IGNORECASE)
    valores = [preco_para_float(m) for m in matches_precos if preco_para_float(m) > 0]

    p_novo_amz = preco_para_float(info_amazon.get("preco_atual_amz"))
    p_antigo_amz = preco_para_float(info_amazon.get("preco_antigo_amz"))

    preco_novo_val = 0.0
    preco_antigo_val = 0.0

    if len(valores) >= 2:
        preco_antigo_val = max(valores[:2])
        preco_novo_val = min(valores[:2])
    elif len(valores) == 1:
        preco_novo_val = valores[0]
        if p_antigo_amz > preco_novo_val:
            preco_antigo_val = p_antigo_amz
    elif p_novo_amz > 0:
        preco_novo_val = p_novo_amz
        preco_antigo_val = p_antigo_amz

    # 3. Cupom
    cupom = ""
    match_cupom = re.search(r'cupom\s*[:\-]?\s*["\'`]*([A-Z0-9]{4,20})["\'`]*', texto_post, re.IGNORECASE)
    if match_cupom:
        cupom = limpar_cupom(match_cupom.group(1))

    # 4. Condição
    condicao = "no PIX" if "pix" in texto_post.lower() else ("à vista" if "vista" in texto_post.lower() else "")

    if not titulo or preco_novo_val <= 0:
        return None

    return {
        "valido": True,
        "produto": titulo,
        "preco_antigo": float_para_str_planilha(preco_antigo_val),
        "preco_novo": float_para_str_planilha(preco_novo_val),
        "cupom": cupom,
        "condicao": condicao,
        "postar_insta": "SIM"
    }

def extrair_dados_com_ia(texto_post, info_amazon):
    contexto_amz = ""
    if info_amazon.get("titulo_amazon"):
        contexto_amz = f"""
        Dados oficiais da página da Amazon para conferência:
        - Título na Amazon: {info_amazon.get('titulo_amazon')}
        - Preço atual na Amazon: {info_amazon.get('preco_atual_amz')}
        - Preço antigo (De:) na Amazon: {info_amazon.get('preco_antigo_amz')}
        """

    prompt = f"""
    Analise esta publicação de oferta:
    Texto do Post:
    {texto_post}
    {contexto_amz}

    REGRAS OBRIGATÓRIAS:
    1. Se NÃO for uma oferta de um produto específico, ou se o Texto do Post falar de um produto COMPLETAMENTE DIFERENTE do Título na Amazon (caso informado), ou se o post for exclusivo de outra loja (Mercado Livre, Shopee, Magalu, AliExpress), retorne: {{"valido": false}}
    2. NUNCA invente nem estime o preco_antigo! Preencha "preco_antigo" APENAS se houver um preço antigo/original real mencionado no post (ex: "De R$ X por R$ Y") ou nos dados oficiais da Amazon acima. Caso contrário, deixe "preco_antigo": "".
    3. Preencha "cupom" APENAS se existir um código de cupom explícito aplicável na Amazon (ex: "CASA10", "APP20"). Não coloque cupons de outras lojas (como MELHORDOML) nem palavras como "Prime" ou "No App".

    Se for uma oferta válida da Amazon, retorne APENAS um JSON válido neste formato:
    {{
        "valido": true,
        "produto": "Nome comercial limpo e claro do produto (máximo 55 caracteres)",
        "preco_antigo": "Valor antigo apenas números e vírgula (ex: 199,90) ou vazio se não existir",
        "preco_novo": "Valor promocional apenas números e vírgula (ex: 129,90)",
        "cupom": "Código do cupom se houver (ou vazio)",
        "condicao": "Condição de pagamento se houver, ex: no PIX, à vista, Prime (ou vazio)",
        "postar_insta": "SIM se for um produto atraente (eletrônicos, eletrodomésticos, casa, cozinha, beleza, games, ferramentas, itens de marca) ou NAO se for item sem apelo visual."
    }}
    """
    try:
        resposta = cliente_gemini.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        texto_limpo = resposta.text.strip().replace('```json', '').replace('```', '').strip()
        return json.loads(texto_limpo)
    except Exception as e:
        print(f"   ⚠️ Aviso Gemini (usando extração direta de fallback): {e}")
        return extrair_dados_fallback(texto_post, info_amazon)

# ==========================================
# 7. EXECUÇÃO PRINCIPAL
# ==========================================
def executar_hunter():
    print("🏹 Iniciando o Hunter de Promoções (Exclusivo Amazon | Mínimo 30% OFF ou Cupom)...")
    ofertas = buscar_ofertas_telegram()
    print(f"📦 Total de mensagens brutas coletadas nos canais: {len(ofertas)}")

    registros_atuais = planilha.get_all_records()
    produtos_ja_cadastrados = [str(linha.get('Produto', '')).lower() for linha in registros_atuais]
    links_ja_cadastrados = [str(linha.get('Link', '')).strip() for linha in registros_atuais]
    asins_processados = set()
    novas_adicionadas = 0
    analises_amazon = 0

    for oferta in ofertas:
        if analises_amazon >= 10 or novas_adicionadas >= 3:
            break

        resumo_post = oferta['texto'].splitlines()[0][:65] if oferta['texto'] else "Sem título"

        # 1. Pré-filtro instantâneo contra lojas concorrentes
        if eh_post_de_concorrente(oferta['texto'], oferta['links']):
            continue

        # 2. Primeiro resolve o link para garantir que é da Amazon ANTES de gastar chamada de IA
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

        # 3. Consulta dados oficiais na página da Amazon (título, foto limpa e preço de referência)
        info_amz = consultar_pagina_amazon(asin_encontrado)

        # 4. Extrai e valida dados com o Gemini (ou Fallback automático)
        dados = extrair_dados_com_ia(oferta['texto'], info_amz)
        if not dados or not dados.get('valido') or not dados.get('preco_novo'):
            print("   ⏭️ DESCARTADO: Produto inválido, divergente ou sem preço.")
            continue

        # Se a Amazon informou preço antigo oficial e o post não tinha, aproveita o preço antigo oficial da Amazon
        if not dados.get('preco_antigo') and info_amz.get('preco_antigo_amz'):
            dados['preco_antigo'] = float_para_str_planilha(preco_para_float(info_amz['preco_antigo_amz']))

        # 5. Validação estrita: Desconto >= 30% OU Cupom válido
        aprovada, motivo, desconto_pct, cupom_limpo = validar_oferta_e_desconto(dados)
        if not aprovada:
            print(f"   ⏭️ DESCARTADO ({motivo}): {dados.get('produto')}")
            continue

        nome_produto = dados['produto'].strip()
        if any(nome_produto.lower()[:18] in prod for prod in produtos_ja_cadastrados if prod):
            print(f"   ⏭️ Pulando: '{nome_produto}' já cadastrado na planilha.")
            continue

        # Padroniza preços para salvar na planilha
        preco_antigo_fmt = float_para_str_planilha(preco_para_float(dados.get('preco_antigo')))
        preco_novo_fmt = float_para_str_planilha(preco_para_float(dados.get('preco_novo')))
        imagem_final = info_amz.get("imagem_amazon") or oferta['imagem']

        # 6. Cérebro de intercalação do Instagram (FOTO vs VIDEO)
        vai_pro_insta = str(dados.get('postar_insta', 'SIM')).upper()
        formato_escolhido = ""
        status_insta = ""

        if vai_pro_insta == "SIM":
            formatos_anteriores = [str(l.get('Formato_Insta', '')).upper() for l in registros_atuais if l.get('Formato_Insta')]
            ultimo_formato = formatos_anteriores[-1] if formatos_anteriores else "VIDEO"

            formato_escolhido = "FOTO" if ultimo_formato == "VIDEO" else "VIDEO"
            status_insta = "Aguardando Geracao"
            registros_atuais.append({'Formato_Insta': formato_escolhido})
            print(f"   📸 Selecionado para o Instagram no formato: {formato_escolhido}")

        nova_linha = [
            nome_produto,                  # Coluna A: Produto
            preco_antigo_fmt,              # Coluna B: Preco_Antigo
            preco_novo_fmt,                # Coluna C: Preco_Novo
            link_final_curto,              # Coluna D: Link
            imagem_final,                  # Coluna E: Imagem_URL
            "",                            # Coluna F: Status (Telegram - vai para aprovação)
            cupom_limpo,                   # Coluna G: Cupom
            dados.get('condicao', ''),     # Coluna H: Condicao
            vai_pro_insta,                 # Coluna I: Postar_Insta
            formato_escolhido,             # Coluna J: Formato_Insta
            status_insta,                  # Coluna K: Status_Insta
            ""                             # Coluna L: Legenda_Insta
        ]

        planilha.append_row(nova_linha)
        produtos_ja_cadastrados.append(nome_produto.lower())
        links_ja_cadastrados.append(link_final_curto)
        novas_adicionadas += 1
        print(f"   ✅ APROVADA ({motivo}): {nome_produto} | De R$ {preco_antigo_fmt or '--'} por R$ {preco_novo_fmt}")

        if novas_adicionadas >= 3:
            break

    print(f"\n🎯 Caçada finalizada! {novas_adicionadas} novas ofertas verificadas da AMAZON cadastradas.")

if __name__ == "__main__":
    executar_hunter()
