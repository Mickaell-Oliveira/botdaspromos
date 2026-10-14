import feedparser
import requests
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

# Feed RSS das promoções mais quentes do Pelando
RSS_URL = 'https://www.pelando.com.br/rss/quentes'

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)

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
# 3. FUNÇÕES DO CAÇADOR (HUNTER)
# ==========================================
def encurtar_url(url_longa):
    """Transforma o link longo com a tag visível em um link curto e limpo."""
    try:
        # Usa quote para garantir que caracteres especiais na URL não quebrem a requisição
        api_tinyurl = f"https://tinyurl.com/api-create.php?url={quote(url_longa, safe='')}"
        resposta = requests.get(api_tinyurl, timeout=5)
        if resposta.status_code == 200:
            return resposta.text.strip()
    except Exception as e:
        print(f"⚠️ Aviso: Não foi possível encurtar a URL ({e}). Usando original.")
    return url_longa

def resolver_e_afiliar_link(url_curta):
    """Segue redirecionamentos, injeta a tag da Amazon e devolve um link curto limpo."""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}
    try:
        resposta = requests.get(url_curta, headers=headers, allow_redirects=True, timeout=10)
        url_final = resposta.url
        
        # Se o link final for da Amazon, limpa, injeta a Tag e ENCURTA!
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
                
        # Se for de outra loja, encurta também para manter o padrão visual do canal
        return encurtar_url(url_final)
    except Exception:
        return encurtar_url(url_curta)

def extrair_dados_com_ia(titulo_bruto, descricao_bruta):
    """Usa o Gemini para limpar o texto do RSS e devolver variáveis perfeitas em JSON."""
    prompt = f"""
    Analise esta oferta capturada de um feed de promoções e extraia os dados estruturados.
    Título: {titulo_bruto}
    Descrição: {descricao_bruta}

    Retorne APENAS um objeto JSON válido (sem blocos de código markdown ```json) exatamente com estas chaves:
    {{
        "produto": "Nome limpo e comercial do produto (sem o preço no título)",
        "preco_antigo": "Valor antigo apenas números e vírgula (se não houver no texto, estime um valor de mercado 20% maior que o preço novo)",
        "preco_novo": "Valor da promoção apenas números e vírgula (ex: 1.299,90)",
        "cupom": "Código do cupom se mencionado, caso contrário deixe vazio",
        "condicao": "Condição de pagamento se houver (ex: no PIX, no Ame, Cartão da loja), caso contrário deixe vazio"
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

def buscar_imagem_no_rss(item):
    """Tenta extrair a URL da imagem do produto de dentro do item do RSS."""
    if 'media_content' in item and len(item.media_content) > 0:
        return item.media_content[0]['url']
    if 'media_thumbnail' in item and len(item.media_thumbnail) > 0:
        return item.media_thumbnail[0]['url']
        
    descricao = item.get('summary', '')
    match_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', descricao)
    if match_img:
        return match_img.group(1)
        
    return "[https://via.placeholder.com/600x600.png?text=Oferta+Imperdivel](https://via.placeholder.com/600x600.png?text=Oferta+Imperdivel)"

# ==========================================
# 4. EXECUÇÃO PRINCIPAL
# ==========================================
def executar_hunter():
    print("🏹 Iniciando o Hunter de Promoções...")
    feed = feedparser.parse(RSS_URL)
    
    if not feed.entries:
        print("⚠️ Nenhuma oferta encontrada no Feed RSS no momento.")
        return

    # Pega todos os títulos que já estão na planilha para NÃO duplicar ofertas
    registros_atuais = planilha.get_all_records()
    produtos_ja_cadastrados = [str(linha.get('Produto', '')).lower() for linha in registros_atuais]
    
    novas_adicionadas = 0
    
    # Analisa as 5 promoções mais recentes do topo do feed por execução
    for item in feed.entries[:5]:
        titulo_bruto = item.title
        descricao_bruta = item.get('summary', '')
        link_rss = item.link
        
        print(f"\n🔎 Analisando item do feed: {titulo_bruto}")
        
        # 1. Passa na IA para separar Nome, Preço, Cupom e Condição
        dados = extrair_dados_com_ia(titulo_bruto, descricao_bruta)
        if not dados or not dados.get('preco_novo'):
            print("⏭️ Pulando: Não foi possível identificar um preço claro na oferta.")
            continue
            
        nome_produto = dados['produto']
        
        # Verifica se já temos algo muito parecido na planilha
        if any(nome_produto.lower()[:15] in prod for prod in produtos_ja_cadastrados if prod):
            print(f"⏭️ Pulando: '{nome_produto}' já está na planilha.")
            continue
            
        # 2. Captura a imagem e resolve/encurta o link (injetando tag Amazon se for o caso)
        imagem_url = buscar_imagem_no_rss(item)
        link_final_curto = resolver_e_afiliar_link(link_rss)
        
        # 3. Monta a linha exatamente na ordem das colunas A até H da planilha
        nova_linha = [
            nome_produto,                  # Coluna A: Produto
            dados.get('preco_antigo', ''), # Coluna B: Preco_Antigo
            dados.get('preco_novo', ''),   # Coluna C: Preco_Novo
            link_final_curto,              # Coluna D: Link (Já encurtado!)
            imagem_url,                    # Coluna E: Imagem_URL
            "",                            # Coluna F: Status (Fica VAZIO pro bot postar!)
            dados.get('cupom', ''),        # Coluna G: Cupom
            dados.get('condicao', '')      # Coluna H: Condicao
        ]
        
        # 4. Insere na planilha!
        planilha.append_row(nova_linha)
        produtos_ja_cadastrados.append(nome_produto.lower())
        novas_adicionadas += 1
        print(f"✅ Oferta adicionada na fila: {nome_produto} | R$ {dados.get('preco_novo')} | Link: {link_final_curto}")
        
        time.sleep(4)
        
        # Limita a adicionar no máximo 2 ofertas novas por rodada
        if novas_adicionadas >= 2:
            break

    print(f"\n🎯 Caçada finalizada! {novas_adicionadas} novas ofertas colocadas na planilha.")

if __name__ == "__main__":
    executar_hunter()
