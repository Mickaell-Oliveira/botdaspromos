import feedparser
import requests
import cloudscraper
from bs4 import BeautifulSoup
import re
import json
import time
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
GEMINI_API_KEY = '(os.getenv('GEMINI_API_KEY') or '').strip()'
NOME_DA_PLANILHA = 'BugdasPromos'
AMAZON_TAG = 'bugdaspromo07-20'

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})

# ==========================================
# 2. CONEXÃO COM GOOGLE SHEETS
# ==========================================
escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
credenciais = Credentials.from_service_account_file("credentials.json", scopes=escopos)
cliente_sheets = gspread.authorize(credenciais)
planilha = cliente_sheets.open(NOME_DA_PLANILHA).sheet1

# ==========================================
# 3. FUNÇÕES DE CAPTURA MULTI-FONTE
# ==========================================
def buscar_ofertas_brutas():
    """Tenta capturar ofertas de múltiplas fontes (Feeds RSS e páginas de agregadores)."""
    ofertas_encontradas = []
    
    # Lista de Feeds RSS de promoções/hardware/varejo para tentar em ordem
    urls_rss = [
        'https://www.pelando.com.br/rss',
        'https://www.pelando.com.br/rss/quentes',
        'https://www.hardmob.com.br/external.php?type=RSS2&forumids=407', # Fórum Hardmob Promoções (muito famoso por bugs reais!)
        'https://adrenaline.com.br/rss'
    ]
    
    for url in urls_rss:
        try:
            print(f"📡 Tentando fonte RSS: {url}")
            resp = scraper.get(url, timeout=12)
            print(f"   ↳ Status HTTP: {resp.status_code}")
            
            if resp.status_code == 200:
                feed = feedparser.parse(resp.content)
                if feed.entries:
                    print(f"   ✅ Sucesso! {len(feed.entries)} itens encontrados.")
                    for item in feed.entries[:8]:
                        ofertas_encontradas.append({
                            'titulo': item.get('title', ''),
                            'descricao': item.get('summary', item.get('description', '')),
                            'link': item.get('link', ''),
                            'raw_item': item
                        })
                    if len(ofertas_encontradas) >= 5:
                        return ofertas_encontradas
        except Exception as e:
            print(f"   ⚠️ Erro ao ler {url}: {e}")

    # Fallback: Se todos os RSS falharem, tenta extrair direto do HTML/Next.js do Pelando ou Promobit
    try:
        print("📡 Tentando extração direta via HTML (Pelando Recentes)...")
        resp = scraper.get("https://www.pelando.com.br/recentes", timeout=15)
        print(f"   ↳ Status HTTP: {resp.status_code}")
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            script_next = soup.find('script', id='__NEXT_DATA__')
            if script_next:
                dados_json = json.loads(script_next.string)
                # Varre o JSON em busca de objetos que pareçam ofertas (com title e price)
                texto_json = json.dumps(dados_json)
                matches = re.findall(r'\{"id":[^}]*"title":"([^"]+)"[^}]*"price":([0-9.]+)[^}]*\}', texto_json)
                for titulo, preco in matches[:6]:
                    ofertas_encontradas.append({
                        'titulo': f"{titulo} - R\$ {preco}",
                        'descricao': f"Oferta capturada: {titulo} por R\$ {preco}",
                        'link': 'https://www.pelando.com.br',
                        'raw_item': {}
                    })
    except Exception as e:
        print(f"   ⚠️ Erro na extração HTML: {e}")

    return ofertas_encontradas

# ==========================================
# 4. FUNÇÕES DE TRATAMENTO E AFILIAÇÃO
# ==========================================
def encurtar_url(url_longa):
    try:
        api_tinyurl = f"https://tinyurl.com/api-create.php?url={quote(url_longa, safe='')}"
        resposta = requests.get(api_tinyurl, timeout=5)
        if resposta.status_code == 200:
            return resposta.text.strip()
    except Exception:
        pass
    return url_longa

def extrair_link_de_loja_do_html(url_pagina):
    """Se o link for de um fórum (como Hardmob) ou agregador, procura o link direto da loja (Amazon, Meli, etc) dentro do post."""
    try:
        resp = scraper.get(url_pagina, timeout=10)
        if resp.status_code == 200:
            # Procura links de grandes lojas dentro do código da página
            padrao_lojas = r'https?://(?:www\.)?(?:amazon\.com\.br|amzn\.to|mercadolivre\.com\.br|meli\.la|kabum\.com\.br|magazineluiza\.com\.br|magalu\.com|shopee\.com\.br)[^\s"\'<>]+'
            links_achados = re.findall(padrao_lojas, resp.text)
            if links_achados:
                return links_achados[0]
    except Exception:
        pass
    return url_pagina

def resolver_e_afiliar_link(url_inicial):
    try:
        # Primeiro verifica se é link de fórum/agregador e tenta achar o link da loja lá dentro
        if any(dominio in url_inicial for dominio in ['hardmob.com.br', 'pelando.com.br', 'adrenaline.com.br']):
            url_inicial = extrair_link_de_loja_do_html(url_inicial)

        resposta = scraper.get(url_inicial, allow_redirects=True, timeout=10)
        url_final = resposta.url
        
        if "amazon.com.br" in url_final:
            match_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url_final)
            if match_asin:
                asin = match_asin.group(1)
                link_com_tag = f"https://www.amazon.com.br/dp/{asin}?tag={AMAZON_TAG}"
            else:
                parsed = urlparse(url_final)
                params = parse_qs(parsed.query)
                params['tag'] = [AMAZON_TAG]
                nova_query = urlencode(params, doseq=True)
                link_com_tag = urlunparse(parsed._replace(query=nova_query))
                
            return encurtar_url(link_com_tag)
                
        return encurtar_url(url_final)
    except Exception:
        return encurtar_url(url_inicial)

def extrair_dados_com_ia(titulo_bruto, descricao_bruta):
    prompt = f"""
    Analise este item capturado de um feed/fórum de promoções.
    Título: {titulo_bruto}
    Descrição: {descricao_bruta[:1000]}

    Se este item NÃO for uma promoção de produto (ex: for uma notícia comum sem preço), retorne apenas: {{"valido": false}}
    Se for uma promoção com preço, retorne APENAS um objeto JSON válido (sem markdown ```json) com estas chaves:
    {{
        "valido": true,
        "produto": "Nome limpo e comercial do produto (sem o preço e sem tags como [Loja])",
        "preco_antigo": "Valor antigo apenas números e vírgula (se não houver, estime 25% a mais que o preço novo)",
        "preco_novo": "Valor da promoção apenas números e vírgula (ex: 1.299,90)",
        "cupom": "Código do cupom se mencionado, caso contrário deixe vazio",
        "condicao": "Condição de pagamento se houver (ex: no PIX, Cartão), caso contrário deixe vazio",
        "termo_busca_imagem": "Nome curto do produto em 3 palavras para busca"
    }}
    """
    
    modelos = ['gemini-3.8-flash', 'gemini-3.8-flash-8b']
    for modelo in modelos:
        for tentativa in range(2):
            try:
                resposta = cliente_gemini.models.generate_content(model=modelo, contents=prompt)
                texto_limpo = resposta.text.strip().replace('```json', '').replace('```', '').strip()
                return json.loads(texto_limpo)
            except Exception as e:
                if "503" in str(e) or "429" in str(e):
                    time.sleep(3 * (tentativa + 1))
                else:
                    break
    return None

def buscar_imagem_produto(oferta, url_loja):
    """Tenta achar a imagem no RSS ou direto na meta tag (og:image) da página da loja."""
    item = oferta.get('raw_item', {})
    if 'media_content' in item and len(item['media_content']) > 0:
        return item['media_content'][0]['url']
    if 'media_thumbnail' in item and len(item['media_thumbnail']) > 0:
        return item['media_thumbnail'][0]['url']
        
    match_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', oferta.get('descricao', ''))
    if match_img and not "emoji" in match_img.group(1) and not "icon" in match_img.group(1):
        return match_img.group(1)

    # Se não achou no RSS, entra na página do produto e pega a foto de capa (OpenGraph Image)!
    try:
        resp = scraper.get(url_loja, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            og_image = soup.find("meta", property="og:image")
            if og_image and og_image.get("content"):
                return og_image["content"]
    except Exception:
        pass
        
    return "[https://placehold.co/600x600/111111/FFCC00/png?text=BUG+DAS+PROMOS](https://placehold.co/600x600/111111/FFCC00/png?text=BUG+DAS+PROMOS)"

# ==========================================
# 5. EXECUÇÃO PRINCIPAL
# ==========================================
def executar_hunter():
    print("🏹 Iniciando o Hunter de Promoções (Modo Blindado)...")
    ofertas = buscar_ofertas_brutas()
    
    if not ofertas:
        print("❌ Todas as fontes bloquearam a conexão ou estão vazias.")
        return

    registros_atuais = planilha.get_all_records()
    produtos_ja_cadastrados = [str(linha.get('Produto', '')).lower() for linha in registros_atuais]
    
    novas_adicionadas = 0
    
    for oferta in ofertas:
        print(f"\n🔎 Analisando: {oferta['titulo']}")
        
        dados = extrair_dados_com_ia(oferta['titulo'], oferta['descricao'])
        if not dados or not dados.get('valido') or not dados.get('preco_novo'):
            print("⏭️ Pulando: Item não é uma oferta válida com preço.")
            continue
            
        nome_produto = dados['produto']
        
        if any(nome_produto.lower()[:15] in prod for prod in produtos_ja_cadastrados if prod):
            print(f"⏭️ Pulando: '{nome_produto}' já está na planilha.")
            continue
            
        # Extrai o link real da loja e encurta
        link_loja_bruto = extrair_link_de_loja_do_html(oferta['link'])
        link_final_curto = resolver_e_afiliar_link(link_loja_bruto)
        imagem_url = buscar_imagem_produto(oferta, link_loja_bruto)
        
        nova_linha = [
            nome_produto,                  # Coluna A: Produto
            dados.get('preco_antigo', ''), # Coluna B: Preco_Antigo
            dados.get('preco_novo', ''),   # Coluna C: Preco_Novo
            link_final_curto,              # Coluna D: Link
            imagem_url,                    # Coluna E: Imagem_URL
            "",                            # Coluna F: Status
            dados.get('cupom', ''),        # Coluna G: Cupom
            dados.get('condicao', '')      # Coluna H: Condicao
        ]
        
        planilha.append_row(nova_linha)
        produtos_ja_cadastrados.append(nome_produto.lower())
        novas_adicionadas += 1
        print(f"✅ Adicionada na fila: {nome_produto} | R$ {dados.get('preco_novo')} | Link: {link_final_curto}")
        
        time.sleep(4)
        
        if novas_adicionadas >= 2:
            break

    print(f"\n🎯 Caçada finalizada! {novas_adicionadas} novas ofertas colocadas na planilha.")

if __name__ == "__main__":
    executar_hunter()
