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

# Lista de canais públicos do Telegram para o Hunter monitorar.
# Você pode adicionar o @ de qualquer canal público de ofertas que você goste!
CANAIS_FONTE = [
    'pechinchou',
    'promobit',
    'canaltech_ofertas',
    'adrenaline',
    'mundoconectado'
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
# 3. CAPTURA DIRETA DO TELEGRAM WEB (SEM 403)
# ==========================================
def buscar_ofertas_telegram():
    """Lê a página pública (t.me/s/) dos canais de ofertas sem bloqueio de firewall."""
    ofertas_brutas = []
    
    for canal in CANAIS_FONTE:
        canal_limpo = canal.replace('https://t.me/s/', '').replace('https://t.me/', '').replace('@', '').strip()
        url = f"https://t.me/s/{canal_limpo}"
        
        try:
            print(f"📡 Lendo canal público: @{canal_limpo}...")
            resp = requests.get(url, headers=HEADERS, timeout=12)
            print(f"   ↳ Status HTTP: {resp.status_code}")
            
            if resp.status_code != 200:
                continue
                
            soup = BeautifulSoup(resp.text, 'html.parser')
            blocos_msg = soup.select('div.tgme_widget_message_wrap')
            
            if not blocos_msg:
                print(f"   ⚠️ Canal @{canal_limpo} não possui visualização web pública ativa.")
                continue
                
            print(f"   ✅ {len(blocos_msg)} mensagens encontradas! Analisando as mais recentes...")
            
            # Pega as últimas 4 mensagens do canal (de baixo para cima, que são as mais recentes)
            for bloco in reversed(blocos_msg[-4:]):
                div_texto = bloco.select_one('div.tgme_widget_message_text')
                if not div_texto:
                    continue
                    
                texto_msg = div_texto.get_text(separator='\n').strip()
                
                # Extrai todos os links externos presentes na mensagem
                links_encontrados = []
                for a_tag in div_texto.find_all('a', href=True):
                    href = a_tag['href']
                    # Ignora links de convite para outros grupos ou redes sociais
                    if href.startswith('http') and not any(ignorar in href.lower() for ignorar in ['t.me/', 'whatsapp.com', 'instagram.com', 'linktr.ee', '/ofertas']):
                        links_encontrados.append(href)
                        
                if not links_encontrados:
                    continue
                    
                # Extrai a foto do produto (do post ou do preview do link, ignorando avatar do canal)
                imagem_url = ""
                for seletor_foto in ['a.tgme_widget_message_photo_wrap', 'i.link_preview_image', 'a.link_preview_right_image']:
                    elem_foto = bloco.select_one(seletor_foto)
                    if elem_foto and elem_foto.get('style'):
                        match_bg = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", elem_foto['style'])
                        if match_bg:
                            imagem_url = match_bg.group(1)
                            break
                            
                if not imagem_url:
                    imagem_url = "https://placehold.co/600x600/111111/FFCC00/png?text=OFERTA+IMPERDIVEL"
                    
                ofertas_brutas.append({
                    'canal': canal_limpo,
                    'texto': texto_msg,
                    'links': links_encontrados,
                    'imagem': imagem_url
                })
                
        except Exception as e:
            print(f"   ⚠️ Erro ao ler @{canal_limpo}: {e}")
            
    return ofertas_brutas

# ==========================================
# 4. TRATAMENTO DE LINKS E AFILIAÇÃO AMAZON
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
    """Desencurta links (amzn.to, link.amazon, etc.) pulando de salto em salto e troca a tag pela sua."""
    url_atual = url_inicial
    try:
        # Segue até 6 redirecionamentos manualmente para pegar a URL da loja sem tomar bloqueio
        for _ in range(6):
            resp = requests.get(url_atual, headers=HEADERS, allow_redirects=False, timeout=8)
            
            if resp.status_code in (301, 302, 303, 307, 308):
                prox_url = resp.headers.get('Location', '')
                if not prox_url:
                    break
                if prox_url.startswith('/'):
                    parsed_atual = urlparse(url_atual)
                    prox_url = f"{parsed_atual.scheme}://{parsed_atual.netloc}{prox_url}"
                url_atual = prox_url
                
                # Assim que identificar o domínio da Amazon, já para para não precisar carregar o site
                if "amazon.com.br" in url_atual:
                    break
            else:
                # Verifica se o redirecionamento está no HTML (comum em alguns encurtadores)
                match_html = re.search(r'https?://(?:www\.)?amazon\.com\.br[^\s"\'<>]+', resp.text)
                if match_html:
                    url_atual = match_html.group(0)
                break

        # Se descobriu que é Amazon, arranca a tag do concorrente e coloca a sua!
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
                
            print("   💰 Link da Amazon detectado! Tag substituída pela sua com sucesso.")
            return encurtar_url(link_com_tag)
            
        return encurtar_url(url_atual)
    except Exception:
        return encurtar_url(url_atual)

def extrair_dados_com_ia(texto_post, lista_links):
    prompt = f"""
    Analise este post capturado de um canal de promoções no Telegram.
    Texto do Post:
    {texto_post}
    
    Links encontrados no post: {lista_links}

    Se este post NÃO for a oferta de um produto específico com preço (ex: for apenas aviso, lista de cupons sem produto ou propaganda do canal), retorne apenas: {{"valido": false}}
    
    Se for uma oferta válida, retorne APENAS um objeto JSON válido (sem markdown ```json) com estas chaves:
    {{
        "valido": true,
        "produto": "Nome limpo e comercial do produto (sem o preço e sem emojis)",
        "preco_antigo": "Valor antigo apenas números e vírgula (se não estiver explícito no texto, calcule um valor 25% maior que o preco_novo)",
        "preco_novo": "Valor promocional apenas números e vírgula (ex: 1.299,90)",
        "cupom": "Código do cupom se houver (ex: PROMODEHOJE), caso contrário deixe vazio",
        "condicao": "Condição de pagamento se houver (ex: pagando no PIX, no cartão), caso contrário deixe vazio",
        "link_escolhido": "Escolha da lista de links qual é o link de compra do produto (ignore links de grupos)"
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

# ==========================================
# 5. EXECUÇÃO PRINCIPAL
# ==========================================
def executar_hunter():
    print("🏹 Iniciando o Hunter via Canais do Telegram...")
    ofertas = buscar_ofertas_telegram()
    
    if not ofertas:
        print("⚠️ Nenhuma mensagem pública encontrada nos canais configurados.")
        return

    registros_atuais = planilha.get_all_records()
    produtos_ja_cadastrados = [str(linha.get('Produto', '')).lower() for linha in registros_atuais]
    
    novas_adicionadas = 0
    
    for oferta in ofertas:
        primeira_linha = oferta['texto'].split('\n')[0][:60]
        print(f"\n🔎 Analisando post de @{oferta['canal']}: {primeira_linha}...")
        
        dados = extrair_dados_com_ia(oferta['texto'], oferta['links'])
        if not dados or not dados.get('valido') or not dados.get('preco_novo'):
            print("   ⏭️ Pulando: Post não é uma oferta de produto válida.")
            continue
            
        nome_produto = dados['produto']
        
        # Evita cadastrar o mesmo produto duas vezes
        if any(nome_produto.lower()[:15] in prod for prod in produtos_ja_cadastrados if prod):
            print(f"   ⏭️ Pulando: '{nome_produto}' já está na planilha.")
            continue
            
        link_bruto = dados.get('link_escolhido') or oferta['links'][0]
        link_final_curto = resolver_e_afiliar_link(link_bruto)
        
        nova_linha = [
            nome_produto,                  # Coluna A: Produto
            dados.get('preco_antigo', ''), # Coluna B: Preco_Antigo
            dados.get('preco_novo', ''),   # Coluna C: Preco_Novo
            link_final_curto,              # Coluna D: Link
            oferta['imagem'],              # Coluna E: Imagem_URL
            "",                            # Coluna F: Status
            dados.get('cupom', ''),        # Coluna G: Cupom
            dados.get('condicao', '')      # Coluna H: Condicao
        ]
        
        planilha.append_row(nova_linha)
        produtos_ja_cadastrados.append(nome_produto.lower())
        novas_adicionadas += 1
        print(f"   ✅ Adicionada na fila: {nome_produto} | R$ {dados.get('preco_novo')} | Link: {link_final_curto}")
        
        time.sleep(4)
        
        # Adiciona no máximo 2 ofertas por rodada para manter um ritmo natural no grupo
        if novas_adicionadas >= 2:
            break

    print(f"\n🎯 Caçada finalizada! {novas_adicionadas} novas ofertas colocadas na planilha.")

if __name__ == "__main__":
    executar_hunter()
