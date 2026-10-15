import os
import re
import json
import time
import subprocess
from io import BytesIO
import requests
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from PIL import Image, ImageDraw, ImageFont

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
GEMINI_API_KEY = '(os.getenv('GEMINI_API_KEY') or '').strip()'
NOME_DA_PLANILHA = 'BugdasPromos'

TELEGRAM_TOKEN = '(os.getenv('TELEGRAM_TOKEN') or '').strip()'
ADMIN_CHAT_ID = '5096433671'

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0'}

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

COL_PRODUTO = 1
COL_PRECO_ANTIGO = 2
COL_PRECO_NOVO = 3
COL_CUPOM = 7
COL_CONDICAO = 8
COL_POSTAR_INSTA = 9
COL_FORMATO_INSTA = 10
COL_STATUS_INSTA = 11
COL_LEGENDA_INSTA = 12

# ==========================================
# 3. NOVO DESIGN E FORMATAÇÃO DE PREÇOS
# ==========================================
def carregar_fonte(tamanho, negrito=True):
    caminhos = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrito else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    ]
    for caminho in caminhos:
        if os.path.exists(caminho):
            return ImageFont.truetype(caminho, tamanho)
    return ImageFont.load_default()

def quebrar_texto(texto, fonte, largura_max, draw):
    palavras = texto.split()
    linhas = []
    linha_atual = ""
    for p in palavras:
        teste = f"{linha_atual} {p}".strip()
        bbox = draw.textbbox((0, 0), teste, font=fonte)
        if (bbox[2] - bbox[0]) <= largura_max:
            linha_atual = teste
        else:
            if linha_atual:
                linhas.append(linha_atual)
            linha_atual = p
    if linha_atual:
        linhas.append(linha_atual)
    return linhas[:3]

def formatar_preco(valor):
    """Garante que o preço sempre tenha R$, ponto e vírgula, corrigindo erros da IA."""
    if not valor or str(valor).strip() == "": return ""
    v = str(valor).replace("R$", "").replace(" ", "").strip()
    
    # Se a IA retornou os números colados (ex: 28200 em vez de 282,00)
    if v.isdigit() and len(v) >= 3:
        v = v[:-2] + "," + v[-2:]
    elif "." in v and "," not in v:
        v = v.replace(".", ",")
        
    return f"R$ {v}"

def desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="FOTO", caminho_saida="post_insta.jpg"):
    largura = 1080
    altura = 1080 if formato == "FOTO" else 1920
    
    # Fundo Azul moderno (Inspirado na sua referência)
    img = Image.new('RGB', (largura, altura), color=(30, 70, 190))
    draw = ImageDraw.Draw(img)
    
    offset_y = 60 if formato == "FOTO" else 220
    
    # 1. Topo: Selo "BUG DAS PROMOS"
    draw.rounded_rectangle([(80, offset_y), (390, offset_y + 70)], radius=35, fill=(255, 255, 255))
    f_selo = carregar_fonte(28, True)
    draw.text((115, offset_y + 18), "BUG DAS PROMOS", fill=(30, 70, 190), font=f_selo)
    
    f_semana = carregar_fonte(32, False)
    draw.text((420, offset_y + 15), "oferta exclusiva", fill=(255, 255, 255), font=f_semana)

    # 2. Quadro Branco Central para a Imagem
    caixa_y1 = offset_y + 110
    altura_caixa = 480 if formato == "FOTO" else 650
    caixa_y2 = caixa_y1 + altura_caixa
    draw.rounded_rectangle([(140, caixa_y1), (940, caixa_y2)], radius=40, fill=(255, 255, 255))
    
    try:
        resp = requests.get(url_img, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            prod_img = Image.open(BytesIO(resp.content)).convert("RGBA")
            
            # CORTE INTELIGENTE: Remove os 15% do topo para arrancar a marca d'água do canal concorrente!
            larg_img, alt_img = prod_img.size
            prod_img = prod_img.crop((0, int(alt_img * 0.15), larg_img, alt_img))
            
            fundo_branco = Image.new("RGBA", prod_img.size, "WHITE")
            fundo_branco.paste(prod_img, (0, 0), prod_img)
            prod_img = fundo_branco.convert("RGB")
            
            prod_img.thumbnail((760, altura_caixa - 40), Image.Resampling.LANCZOS)
            px = (largura - prod_img.width) // 2
            py = caixa_y1 + (altura_caixa - prod_img.height) // 2
            img.paste(prod_img, (px, py))
    except Exception as e:
        print(f"⚠️ Aviso ao carregar imagem: {e}")

    # 3. Nome do Produto
    y_texto = caixa_y2 + 45
    f_prod = carregar_fonte(36, False)
    linhas = quebrar_texto(produto, f_prod, 900, draw)
    for linha in linhas:
        bbox = draw.textbbox((0, 0), linha, font=f_prod)
        draw.text(((largura - (bbox[2] - bbox[0])) / 2, y_texto), linha, fill=(255, 255, 255), font=f_prod)
        y_texto += 45
        
    y_texto += 25
    
    # 4. Preço Antigo com Risco (Limpo e elegante)
    preco_antigo_fmt = formatar_preco(preco_antigo)
    if preco_antigo_fmt:
        f_antigo = carregar_fonte(45, False)
        txt_antigo = f"de {preco_antigo_fmt}"
        bbox = draw.textbbox((0, 0), txt_antigo, font=f_antigo)
        x_ant = (largura - (bbox[2] - bbox[0])) / 2
        draw.text((x_ant, y_texto), txt_antigo, fill=(200, 210, 255), font=f_antigo)
        
        # Linha de corte
        meio_y = y_texto + (bbox[3] - bbox[1]) / 2 + 5
        draw.line([(x_ant - 10, meio_y), (x_ant + (bbox[2] - bbox[0]) + 10, meio_y)], fill=(255, 255, 255), width=4)
        y_texto += 60
        
    # 5. Preço Novo Gigante e Laranja
    preco_novo_fmt = formatar_preco(preco_novo)
    f_novo_menor = carregar_fonte(50, True)
    f_novo_maior = carregar_fonte(95, True)
    
    txt_por = "por "
    bbox_por = draw.textbbox((0, 0), txt_por, font=f_novo_menor)
    bbox_preco = draw.textbbox((0, 0), preco_novo_fmt, font=f_novo_maior)
    
    largura_total_preco = (bbox_por[2] - bbox_por[0]) + (bbox_preco[2] - bbox_preco[0])
    x_inicial_preco = (largura - largura_total_preco) / 2
    
    draw.text((x_inicial_preco, y_texto + 35), txt_por, fill=(255, 170, 50), font=f_novo_menor)
    draw.text((x_inicial_preco + (bbox_por[2] - bbox_por[0]), y_texto), preco_novo_fmt, fill=(255, 170, 50), font=f_novo_maior)
    y_texto += 120

    # 6. Cupom
    if cupom:
        f_cupom = carregar_fonte(30, True)
        txt_cupom = f"CUPOM: {cupom.upper()}"
        bbox = draw.textbbox((0, 0), txt_cupom, font=f_cupom)
        draw.text(((largura - (bbox[2] - bbox[0])) / 2, y_texto), txt_cupom, fill=(255, 255, 255), font=f_cupom)

    # 7. Rodapé Discreto
    rodape_y = altura - (70 if formato == "FOTO" else 200)
    f_cta = carregar_fonte(24, True)
    txt_cta = "COMENTE 'EU QUERO' PARA RECEBER O LINK!"
    bbox = draw.textbbox((0, 0), txt_cta, font=f_cta)
    draw.text(((largura - (bbox[2] - bbox[0])) / 2, rodape_y), txt_cta, fill=(200, 210, 255), font=f_cta)
    
    img.save(caminho_saida, quality=95)
    return caminho_saida

def gerar_reels_mp4(produto, preco_antigo, preco_novo, cupom, url_img, texto_narracao):
    caminho_img = "frame_reels.jpg"
    caminho_audio = "locucao.mp3"
    caminho_video = "reels_final.mp4"
    
    desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="VIDEO", caminho_saida=caminho_img)
    
    texto_limpo = texto_narracao.replace('"', '').replace("'", "").replace("\n", " ")
    subprocess.run([
        "edge-tts", "--voice", "pt-BR-AntonioNeural", "--rate=+12%",
        "--text", texto_limpo, "--write-media", caminho_audio
    ], check=True)
    
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", caminho_img, "-i", caminho_audio,
        "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p", "-shortest", caminho_video
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    return caminho_video

# ==========================================
# 4. TEXTOS COM IA E ENVIO PARA APROVAÇÃO
# ==========================================
def criar_textos_insta(produto, preco_antigo, preco_novo, cupom, condicao, instrucao_extra=""):
    obs = f"\nATENÇÃO - O DONO PEDIU ESTA ALTERAÇÃO: {instrucao_extra}" if instrucao_extra else ""
    prompt = f"""
    Crie o conteúdo de um post de Instagram para a página @BugdasPromos:
    Produto: {produto}
    Preço Antigo: R$ {preco_antigo}
    Preço Promoção: R$ {preco_novo}
    Cupom: {cupom} | Condição: {condicao}{obs}

    Retorne APENAS um JSON válido com estas 2 chaves:
    {{
        "legenda": "Legenda para Instagram (sem links). Fale para comentar EU QUERO ou ir no link da Bio. Inclua 5 hashtags.",
        "narracao": "Texto dinâmico (máximo 22 palavras) para locução de vídeo."
    }}
    """
    for modelo in ['gemini-3.8-flash', 'gemini-3.8-flash-8b']:
        try:
            resp = cliente_gemini.models.generate_content(model=modelo, contents=prompt)
            return json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
        except Exception:
            time.sleep(2)
    return {
        "legenda": f"🚨 Baita oferta na Amazon!\n\n📦 {produto}\n❌ De: R$ {preco_antigo}\n✅ Por: R$ {preco_novo}\n\n💬 Comente EU QUERO para receber o link no Direct!\n\n#ofertas #promocao #amazonbrasil #achadinhos",
        "narracao": f"Se liga nesse bug da Amazon! {produto} por apenas {preco_novo}! Comenta eu quero que te mando o link!"
    }

def enviar_para_aprovacao_admin(indice_linha, formato, caminho_midia, legenda):
    teclado = {
        "inline_keyboard": [
            [
                {"text": "✅ Aprovar (Pronto)", "callback_data": f"aprovar_{indice_linha}"},
                {"text": "❌ Descartar", "callback_data": f"rejeitar_{indice_linha}"}
            ]
        ]
    }
    
    texto_msg = (
        f"🎨 PRÉVIA DO INSTAGRAM (Linha {indice_linha})\n"
        f"📌 Formato: {formato}\n\n"
        f"📝 Legenda sugerida:\n{legenda}\n\n"
        f"💡 IMPORTANTE: Para pedir edição, clique em RESPONDER (Reply) a esta mensagem!"
    )
    
    endpoint = "sendPhoto" if formato == "FOTO" else "sendVideo"
    campo_arquivo = "photo" if formato == "FOTO" else "video"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}"
    
    with open(caminho_midia, "rb") as f:
        resp = requests.post(url, data={"chat_id": ADMIN_CHAT_ID, "caption": texto_msg[:1024], "reply_markup": json.dumps(teclado)}, files={campo_arquivo: f}).json()
        return resp.get("result", {}).get("message_id")

# ==========================================
# 5. LEITURA DE CLIQUES E PEDIDOS DE EDIÇÃO
# ==========================================
def verificar_respostas_do_admin():
    print("🕵️ Verificando respostas do Telegram...")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, timeout=10).json()
    except Exception:
        return
        
    updates = resp.get("result", [])
    if not updates: return
        
    maior_update_id = 0
    for up in updates:
        maior_update_id = max(maior_update_id, up["update_id"])
        
        if "callback_query" in up:
            cb = up["callback_query"]
            dados_cb = cb.get("data", "")
            msg_id = cb.get("message", {}).get("message_id")
            
            if dados_cb.startswith("aprovar_"):
                linha_idx = int(dados_cb.split("_")[1])
                planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Aprovado")
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"✅ Linha {linha_idx} APROVADA!", "reply_to_message_id": msg_id})
                print(f"   ✅ Linha {linha_idx} aprovada!")
                
            elif dados_cb.startswith("rejeitar_"):
                linha_idx = int(dados_cb.split("_")[1])
                planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Descartado")
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha_idx} DESCARTADA!", "reply_to_message_id": msg_id})

        elif "message" in up and "reply_to_message" in up["message"]:
            msg = up["message"]
            texto_original = msg["reply_to_message"].get("caption", "")
            pedido = msg.get("text", "")
            
            match_linha = re.search(r"Linha (\d+)", texto_original)
            if match_linha and pedido:
                linha_idx = int(match_linha.group(1))
                print(f"   ✏️ Alteração Linha {linha_idx}: '{pedido}'")
                aplicar_alteracao_com_ia(linha_idx, pedido, msg["message_id"])

    if maior_update_id > 0:
        requests.get(f"{url}?offset={maior_update_id + 1}", timeout=5)

def aplicar_alteracao_com_ia(indice_linha, pedido_admin, reply_msg_id):
    valores_linha = planilha.row_values(indice_linha)
    while len(valores_linha) < 12: valores_linha.append("")
        
    produto, preco_ant, preco_novo, cupom, formato = valores_linha[0], valores_linha[1], valores_linha[2], valores_linha[6], (valores_linha[9] or "FOTO")
    
    prompt = f"""
    O admin pediu para alterar o post.
    Atual: Produto: {produto} | Preço Antigo: {preco_ant} | Preço Novo: {preco_novo} | Cupom: {cupom} | Formato: {formato}
    Pedido: "{pedido_admin}"
    Retorne JSON com as chaves: "produto", "preco_antigo", "preco_novo", "cupom", "formato" (FOTO ou VIDEO), "legenda", "narracao".
    """
    try:
        resp = cliente_gemini.models.generate_content(model='gemini-3.8-flash', contents=prompt)
        novos = json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
        
        planilha.update_cell(indice_linha, COL_PRODUTO, novos.get("produto", produto))
        planilha.update_cell(indice_linha, COL_PRECO_ANTIGO, novos.get("preco_antigo", preco_ant))
        planilha.update_cell(indice_linha, COL_PRECO_NOVO, novos.get("preco_novo", preco_novo))
        planilha.update_cell(indice_linha, COL_CUPOM, novos.get("cupom", cupom))
        planilha.update_cell(indice_linha, COL_FORMATO_INSTA, novos.get("formato", formato).upper())
        planilha.update_cell(indice_linha, COL_LEGENDA_INSTA, novos.get("legenda", valores_linha[11]))
        planilha.update_cell(indice_linha, COL_STATUS_INSTA, f"Regerar|{pedido_admin}")
        
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"🔄 Refazendo a Linha {indice_linha}...", "reply_to_message_id": reply_msg_id})
    except Exception as e:
        print(f"Erro IA: {e}")

# ==========================================
# 6. FLUXO PRINCIPAL
# ==========================================
def processar_fila_instagram():
    verificar_respostas_do_admin()
    print("\n🎨 Verificando fila de criação...")
    registros = planilha.get_all_records()
    gerados = 0
    
    for indice, linha in enumerate(registros, start=2):
        if str(linha.get('Postar_Insta', '')).upper() == "SIM" and str(linha.get('Status_Insta', '')) in ["", "Aguardando Geracao"] or str(linha.get('Status_Insta', '')).startswith("Regerar"):
            produto, preco_antigo, preco_novo = str(linha.get('Produto', '')), str(linha.get('Preco_Antigo', '')), str(linha.get('Preco_Novo', ''))
            cupom, condicao, url_img, formato = str(linha.get('Cupom', '')), str(linha.get('Condicao', '')), str(linha.get('Imagem_URL', '')), str(linha.get('Formato_Insta', 'FOTO')).upper()
            status = str(linha.get('Status_Insta', ''))
            inst_extra = status.split("|")[1] if "|" in status else ""
            
            try:
                textos = criar_textos_insta(produto, preco_antigo, preco_novo, cupom, condicao, inst_extra)
                legenda = linha.get('Legenda_Insta') if (status.startswith("Regerar") and linha.get('Legenda_Insta')) else textos['legenda']
                
                if formato == "VIDEO": caminho_midia = gerar_reels_mp4(produto, preco_antigo, preco_novo, cupom, url_img, textos['narracao'])
                else: caminho_midia = desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="FOTO")
                    
                msg_id = enviar_para_aprovacao_admin(indice, formato, caminho_midia, legenda)
                if msg_id:
                    planilha.update_cell(indice, COL_STATUS_INSTA, "Em Aprovacao")
                    planilha.update_cell(indice, COL_LEGENDA_INSTA, legenda)
                    gerados += 1
                if gerados >= 2: break
            except Exception as e:
                print(f"❌ Erro linha {indice}: {e}")

if __name__ == "__main__":
    processar_fila_instagram()
