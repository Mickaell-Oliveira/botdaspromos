import requests
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

CANAIS_FONTE = [
    'pechinchou',
    'promobit',
    'canaltech_ofertas',
    'achados_amazon_br'
]

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}

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
# 3. CAPTURA DIRETA DO TELEGRAM WEB
# ==========================================
def buscar_ofertas_telegram():
    ofertas_brutas = []
    for canal in CANAIS_FONTE:
        canal_limpo = canal.replace('https://t.me/s/', '').replace('https://t.me/', '').replace('@', '').strip()
        url = f"https://t.me/s/{canal_limpo}"
        
        try:
            print(f"📡 Lendo canal público: @{canal_limpo}...")
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
                
            soup = BeautifulSoup(resp.text, 'html.parser')
            blocos_msg = soup.select('div.tgme_widget_message_wrap')
            if not blocos_msg:
                continue
                
            for bloco in reversed(blocos_msg[-4:]):
                div_texto = bloco.select_one('div.tgme_widget_message_text')
                if not div_texto:
                    continue
                    
                texto_msg = div_texto.get_text(separator='\n').strip()
                links_encontrados = [a['href'] for a in div_texto.find_all('a', href=True) if a['href'].startswith('http')]
                
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
                            
                ofertas_brutas.append({
                    'canal': canal_limpo,
                    'texto': texto_msg,
                    'links': links_encontrados,
                    'imagem': imagem_url
                })
        except Exception:
            pass
    return ofertas_brutas

# ==========================================
# 4. FILTRO DE OURO: APENAS AMAZON!
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

def resolver_e_afiliar_link(url_inicial):
    url_atual = url_inicial
    try:
        for _ in range(8):
            resp = requests.get(url_atual, headers=HEADERS, allow_redirects=False, timeout=8)
            
            if resp.status_code in (301, 302, 303, 307, 308):
                prox_url = resp.headers.get('Location', '')
                if not prox_url:
                    break
                if prox_url.startswith('/'):
                    parsed = urlparse(url_atual)
                    prox_url = f"{parsed.scheme}://{parsed.netloc}{prox_url}"
                url_atual = prox_url
                
                if "amazon.com.br" in url_atual:
                    break
            else:
                match_html = re.search(r'https?://(?:www\.)?amazon\.com\.br[^\s"\'<>]+', resp.text)
                if match_html:
                    url_atual = match_html.group(0)
                    break
                match_amzn = re.search(r'https?://amzn\.to/[a-zA-Z0-9]+', resp.text)
                if match_amzn:
                    url_atual = match_amzn.group(0)
                    continue
                break

        if "amazon.com.br" in url_atual:
            match_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url_atual)
            if match_asin:
                asin = match_asin.group(1)
                link_com_tag = f"https://www.amazon.com.br/dp/{asin}?tag={AMAZON_TAG}"
            else:
                parsed = urlparse(url_atual)
                params = parse_qs(parsed.query)
                params['tag'] = [AMAZON_TAG]
                nova_query = urlencode(params, doseq=True)
                link_com_tag = urlunparse(parsed._replace(query=nova_query))
                
            print("   💰 Link da Amazon rastreado e tag substituída!")
            return encurtar_url(link_com_tag)
            
        return None
    except Exception:
        return None

def extrair_dados_com_ia(texto_post, lista_links):
    prompt = f"""
    Analise este post: {texto_post}
    Links: {lista_links}

    Se NÃO for um produto (ex: notícia, aviso), retorne: {{"valido": false}}
    
    Se for uma oferta, retorne APENAS um JSON válido:
    {{
        "valido": true,
        "produto": "Nome comercial limpo do produto (máximo 55 caracteres)",
        "preco_antigo": "Valor antigo apenas números e vírgula (ex: 199,90). Se não houver no post, calcule 30% a mais que o preco_novo",
        "preco_novo": "Valor da promoção apenas números e vírgula (ex: 129,90)",
        "cupom": "Cupom se houver (ou vazio)",
        "condicao": "Condição se houver, ex: no PIX (ou vazio)",
        "link_escolhido": "O link de compra",
        "postar_insta": "SIM se for um produto atraente (eletrônicos, casa, cozinha, beleza, games, itens de marca com ótimo desconto) para vitrine do Instagram, ou NAO se for algo desinteressante/desconto pequeno."
    }}
    """
    modelos = ['gemini-3.8-flash', 'gemini-3.8-flash-8b']
    for modelo in modelos:
        for tentativa in range(2):
            try:
                resposta = cliente_gemini.models.generate_content(model=modelo, contents=prompt)
                return json.loads(resposta.text.strip().replace('```json', '').replace('```', '').strip())
            except Exception:
                time.sleep(3)
    return None

# ==========================================
# 5. EXECUÇÃO PRINCIPAL
# ==========================================
def executar_hunter():
    print("🏹 Iniciando o Hunter de Promoções (Exclusivo Amazon)...")
    ofertas = buscar_ofertas_telegram()
    registros_atuais = planilha.get_all_records()
    produtos_ja_cadastrados = [str(linha.get('Produto', '')).lower() for linha in registros_atuais]
    novas_adicionadas = 0
    
    for oferta in ofertas:
        print(f"\n🔎 Analisando post: {oferta['texto'].splitlines()[0][:60]}...")
        
        dados = extrair_dados_com_ia(oferta['texto'], oferta['links'])
        if not dados or not dados.get('valido') or not dados.get('preco_novo'):
            continue
            
        nome_produto = dados['produto']
        if any(nome_produto.lower()[:15] in prod for prod in produtos_ja_cadastrados if prod):
            print(f"   ⏭️ Pulando: '{nome_produto}' já cadastrado.")
            continue
            
        link_final_curto = resolver_e_afiliar_link(dados.get('link_escolhido') or oferta['links'][0])
        if not link_final_curto:
            print("   ⏭️ DESCARTADO: A oferta não é da Amazon ou usa bloqueador de link.")
            continue
            
        # CÉREBRO DE INTERCALAÇÃO DO INSTAGRAM (FOTO vs VIDEO)
        vai_pro_insta = dados.get('postar_insta', 'SIM').upper()
        formato_escolhido = ""
        status_insta = ""
        
        if vai_pro_insta == "SIM":
            formatos_anteriores = [str(l.get('Formato_Insta', '')).upper() for l in registros_atuais if l.get('Formato_Insta')]
            ultimo_formato = formatos_anteriores[-1] if formatos_anteriores else "VIDEO"
            
            formato_escolhido = "FOTO" if ultimo_formato == "VIDEO" else "VIDEO"
            status_insta = "Aguardando Geracao"
            registros_atuais.append({'Formato_Insta': formato_escolhido}) # Atualiza memória local para a próxima oferta do loop
            print(f"   📸 Selecionado para o Instagram no formato: {formato_escolhido}")
            
        nova_linha = [
            nome_produto,                  # Coluna A: Produto
            dados.get('preco_antigo', ''), # Coluna B: Preco_Antigo
            dados.get('preco_novo', ''),   # Coluna C: Preco_Novo
            link_final_curto,              # Coluna D: Link
            oferta['imagem'],              # Coluna E: Imagem_URL
            "",                            # Coluna F: Status (Telegram)
            dados.get('cupom', ''),        # Coluna G: Cupom
            dados.get('condicao', ''),     # Coluna H: Condicao
            vai_pro_insta,                 # Coluna I: Postar_Insta
            formato_escolhido,             # Coluna J: Formato_Insta
            status_insta,                  # Coluna K: Status_Insta
            ""                             # Coluna L: Legenda_Insta
        ]
        
        planilha.append_row(nova_linha)
        produtos_ja_cadastrados.append(nome_produto.lower())
        novas_adicionadas += 1
        print(f"   ✅ Adicionada na fila: {nome_produto} | R$ {dados.get('preco_novo')}")
        
        if novas_adicionadas >= 2:
            break

    print(f"\n🎯 Caçada finalizada! {novas_adicionadas} novas ofertas da AMAZON cadastradas.")

if __name__ == "__main__":
    executar_hunter()
