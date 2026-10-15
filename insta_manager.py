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

# Índices das colunas na planilha (A=1 ... L=12)
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
# 3. DESIGN AUTOMÁTICO DE CARD E REELS (PILLOW + FFMPEG)
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

def desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="FOTO", caminho_saida="post_insta.jpg"):
    largura = 1080
    altura = 1080 if formato == "FOTO" else 1920
    
    # Fundo escuro moderno (#121214)
    img = Image.new('RGB', (largura, altura), color=(18, 18, 20))
    draw = ImageDraw.Draw(img)
    
    offset_y = 60 if formato == "FOTO" else 220
    
    # 1. Topo Amarelo: BUG DAS PROMOS
    draw.rounded_rectangle([(140, offset_y), (940, offset_y + 95)], radius=25, fill=(255, 204, 0))
    f_topo = carregar_fonte(46, True)
    txt_topo = "BUG DAS PROMOS"
    bbox = draw.textbbox((0, 0), txt_topo, font=f_topo)
    draw.text(((largura - (bbox[2] - bbox[0])) / 2, offset_y + 22), txt_topo, fill=(15, 15, 15), font=f_topo)
    
    # 2. Quadro Branco para a Foto do Produto
    caixa_y1 = offset_y + 130
    altura_caixa = 460 if formato == "FOTO" else 680
    caixa_y2 = caixa_y1 + altura_caixa
    draw.rounded_rectangle([(90, caixa_y1), (990, caixa_y2)], radius=35, fill=(255, 255, 255))
    
    try:
        resp = requests.get(url_img, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            prod_img = Image.open(BytesIO(resp.content)).convert("RGB")
            prod_img.thumbnail((820, altura_caixa - 60), Image.Resampling.LANCZOS)
            px = (largura - prod_img.width) // 2
            py = caixa_y1 + (altura_caixa - prod_img.height) // 2
            img.paste(prod_img, (px, py))
    except Exception as e:
        print(f"⚠️ Aviso ao carregar imagem na arte: {e}")

    # 3. Nome do Produto
    y_texto = caixa_y2 + 40
    f_prod = carregar_fonte(40, True)
    linhas = quebrar_texto(produto, f_prod, 900, draw)
    for linha in linhas:
        bbox = draw.textbbox((0, 0), linha, font=f_prod)
        draw.text(((largura - (bbox[2] - bbox[0])) / 2, y_texto), linha, fill=(255, 255, 255), font=f_prod)
        y_texto += 52
        
    y_texto += 15
    
    # 4. Preço Antigo Riscado
    if preco_antigo:
        f_antigo = carregar_fonte(36, False)
        txt_antigo = f"De: R$ {preco_antigo}"
        bbox = draw.textbbox((0, 0), txt_antigo, font=f_antigo)
        x_ant = (largura - (bbox[2] - bbox[0])) / 2
        draw.text((x_ant, y_texto), txt_antigo, fill=(170, 170, 170), font=f_antigo)
        meio_y = y_texto + (bbox[3] - bbox[1]) / 2 + 6
        draw.line([(x_ant - 10, meio_y), (x_ant + (bbox[2] - bbox[0]) + 10, meio_y)], fill=(255, 65, 65), width=5)
        y_texto += 55
        
    # 5. Preço Novo Gigante em Verde Neon
    f_novo = carregar_fonte(76, True)
    txt_novo = f"Por: R$ {preco_novo}"
    bbox = draw.textbbox((0, 0), txt_novo, font=f_novo)
    draw.text(((largura - (bbox[2] - bbox[0])) / 2, y_texto), txt_novo, fill=(0, 235, 115), font=f_novo)
    y_texto += 95
    
    # 6. Cupom (se houver)
    if cupom:
        f_cupom = carregar_fonte(34, True)
        txt_cupom = f"CUPOM: {cupom.upper()}"
        bbox = draw.textbbox((0, 0), txt_cupom, font=f_cupom)
        draw.text(((largura - (bbox[2] - bbox[0])) / 2, y_texto), txt_cupom, fill=(255, 204, 0), font=f_cupom)
        
    # 7. Rodapé de Conversão (CTA)
    rodape_y = altura - (115 if formato == "FOTO" else 260)
    draw.rounded_rectangle([(70, rodape_y), (1010, rodape_y + 85)], radius=20, fill=(255, 90, 0))
    f_cta = carregar_fonte(33, True)
    txt_cta = "COMENTE 'EU QUERO' OU LINK NA BIO!"
    bbox = draw.textbbox((0, 0), txt_cta, font=f_cta)
    draw.text(((largura - (bbox[2] - bbox[0])) / 2, rodape_y + 24), txt_cta, fill=(255, 255, 255), font=f_cta)
    
    img.save(caminho_saida, quality=95)
    return caminho_saida

def gerar_reels_mp4(produto, preco_antigo, preco_novo, cupom, url_img, texto_narracao):
    """Cria a arte vertical 1080x1920, gera locução neural em PT-BR e renderiza o vídeo MP4."""
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
        "legenda": "Legenda engajadora para Instagram (sem colocar links clicáveis pois não funcionam no feed). Destaque a economia, diga para a pessoa comentar EU QUERO que enviamos o link no Direct ou acessar o Grupo VIP no link da Bio. Inclua 5 hashtags no final.",
        "narracao": "Texto super curto e dinâmico (máximo 22 palavras) para ser falado pelo locutor no Reels. Ex: Olha o bug que acabou de sair na Amazon! [Produto] caiu de X para apenas Y reais! Comenta EU QUERO ou corre no link da bio!"
    }}
    """
    for modelo in ['gemini-3.8-flash', 'gemini-3.8-flash-8b']:
        try:
            resp = cliente_gemini.models.generate_content(model=modelo, contents=prompt)
            return json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
        except Exception:
            time.sleep(2)
    return {
        "legenda": f"🚨 OFERTA NA AMAZON!\n\n📦 {produto}\n❌ De: R$ {preco_antigo}\n✅ Por: R$ {preco_novo}\n\n💬 Comente EU QUERO para receber o link ou acesse o grupo na Bio!\n\n#ofertas #promocao #amazonbrasil #desconto #achadinhos",
        "narracao": f"Olha essa oferta na Amazon! {produto} por apenas {preco_novo} reais! Comenta eu quero ou corre no link da bio!"
    }

def enviar_para_aprovacao_admin(indice_linha, formato, caminho_midia, legenda):
    """Envia a Foto ou Vídeo no Telegram privado do Admin com botões Inline."""
    teclado = {
        "inline_keyboard": [
            [
                {"text": "✅ Aprovar (Pronto p/ Insta)", "callback_data": f"aprovar_{indice_linha}"},
                {"text": "❌ Descartar", "callback_data": f"rejeitar_{indice_linha}"}
            ]
        ]
    }
    
    texto_msg = (
        f"🎨 PRÉVIA DO INSTAGRAM (Linha {indice_linha})\n"
        f"📌 Formato: {formato}\n\n"
        f"📝 Legenda sugerida:\n{legenda}\n\n"
        f"💡 Para pedir alterações (na legenda, preço ou trocar foto/vídeo), basta RESPONDER (Reply) a esta mensagem dizendo o que quer mudar!"
    )
    
    endpoint = "sendPhoto" if formato == "FOTO" else "sendVideo"
    campo_arquivo = "photo" if formato == "FOTO" else "video"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}"
    
    with open(caminho_midia, "rb") as f:
        files = {campo_arquivo: f}
        data = {
            "chat_id": ADMIN_CHAT_ID,
            "caption": texto_msg[:1024],
            "reply_markup": json.dumps(teclado)
        }
        resp = requests.post(url, data=data, files=files).json()
        if not resp.get("ok"):
            print(f"⚠️ Erro na API do Telegram ao enviar prévia: {resp}")
        return resp.get("result", {}).get("message_id")

# ==========================================
# 5. LEITURA DE CLIQUES E PEDIDOS DE EDIÇÃO DO ADMIN
# ==========================================
def verificar_respostas_do_admin():
    """Checa se você clicou nos botões ou respondeu alguma prévia pedindo alterações."""
    print("🕵️ Verificando se você aprovou ou pediu alterações no Telegram...")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, timeout=10).json()
    except Exception:
        return
        
    updates = resp.get("result", [])
    if not updates:
        return
        
    maior_update_id = 0
    for up in updates:
        maior_update_id = max(maior_update_id, up["update_id"])
        
        # 1. Se você clicou em um botão [✅ Aprovar] ou [❌ Descartar]
        if "callback_query" in up:
            cb = up["callback_query"]
            dados_cb = cb.get("data", "")
            msg_id = cb.get("message", {}).get("message_id")
            
            if dados_cb.startswith("aprovar_"):
                linha_idx = int(dados_cb.split("_")[1])
                planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Aprovado")
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={
                    "chat_id": ADMIN_CHAT_ID,
                    "text": f"✅ Linha {linha_idx} APROVADA! Marcada como pronta para publicação no Instagram.",
                    "reply_to_message_id": msg_id
                })
                print(f"   ✅ Linha {linha_idx} aprovada pelo Admin!")
                
            elif dados_cb.startswith("rejeitar_"):
                linha_idx = int(dados_cb.split("_")[1])
                planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Descartado")
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={
                    "chat_id": ADMIN_CHAT_ID,
                    "text": f"❌ Linha {linha_idx} DESCARTADA! Não será postada no Instagram.",
                    "reply_to_message_id": msg_id
                })
                print(f"   ❌ Linha {linha_idx} descartada pelo Admin.")

        # 2. Se você RESPONDEU (Reply) à mensagem do bot pedindo alterações
        elif "message" in up and "reply_to_message" in up["message"]:
            msg = up["message"]
            texto_original = msg["reply_to_message"].get("caption", "") or msg["reply_to_message"].get("text", "")
            pedido_edicao = msg.get("text", "")
            
            match_linha = re.search(r"Linha (\d+)", texto_original)
            if match_linha and pedido_edicao:
                linha_idx = int(match_linha.group(1))
                print(f"   ✏️ Pedido de alteração recebido na Linha {linha_idx}: '{pedido_edicao}'")
                aplicar_alteracao_com_ia(linha_idx, pedido_edicao, msg["message_id"])

    if maior_update_id > 0:
        requests.get(f"{url}?offset={maior_update_id + 1}", timeout=5)

def aplicar_alteracao_com_ia(indice_linha, pedido_admin, reply_msg_id):
    """Usa o Gemini para entender o seu pedido de edição, atualiza a planilha e agenda nova geração."""
    valores_linha = planilha.row_values(indice_linha)
    while len(valores_linha) < 12:
        valores_linha.append("")
        
    produto_atual = valores_linha[0]
    preco_ant_atual = valores_linha[1]
    preco_novo_atual = valores_linha[2]
    cupom_atual = valores_linha[6]
    formato_atual = valores_linha[9] or "FOTO"
    legenda_atual = valores_linha[11]
    
    prompt = f"""
    O administrador pediu uma alteração no post do Instagram.
    Dados atuais:
    - Produto: {produto_atual}
    - Preço Antigo: {preco_ant_atual}
    - Preço Novo: {preco_novo_atual}
    - Cupom: {cupom_atual}
    - Formato: {formato_atual} (pode ser FOTO ou VIDEO)
    - Legenda atual: {legenda_atual}
    
    Pedido do Administrador: "{pedido_admin}"
    
    Aplique as alterações solicitadas e retorne APENAS um JSON válido com as chaves atualizadas:
    {{
        "produto": "Nome do produto (mantenha ou altere se pedido)",
        "preco_antigo": "Preço antigo (mantenha ou altere se pedido)",
        "preco_novo": "Preço novo (mantenha ou altere se pedido)",
        "cupom": "Cupom (mantenha ou altere se pedido)",
        "formato": "FOTO ou VIDEO",
        "legenda": "Nova legenda com as alterações pedidas",
        "narracao": "Texto curto de até 22 palavras para narração caso seja VIDEO"
    }}
    """
    try:
        resp = cliente_gemini.models.generate_content(model='gemini-3.8-flash', contents=prompt)
        novos = json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
        
        planilha.update_cell(indice_linha, COL_PRODUTO, novos.get("produto", produto_atual))
        planilha.update_cell(indice_linha, COL_PRECO_ANTIGO, novos.get("preco_antigo", preco_ant_atual))
        planilha.update_cell(indice_linha, COL_PRECO_NOVO, novos.get("preco_novo", preco_novo_atual))
        planilha.update_cell(indice_linha, COL_CUPOM, novos.get("cupom", cupom_atual))
        planilha.update_cell(indice_linha, COL_FORMATO_INSTA, novos.get("formato", formato_atual).upper())
        planilha.update_cell(indice_linha, COL_LEGENDA_INSTA, novos.get("legenda", legenda_atual))
        
        planilha.update_cell(indice_linha, COL_STATUS_INSTA, f"Regerar|{pedido_admin}")
        
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={
            "chat_id": ADMIN_CHAT_ID,
            "text": f"🔄 Entendido! Atualizei os dados da Linha {indice_linha} e já estou gerando a nova versão para você...",
            "reply_to_message_id": reply_msg_id
        })
    except Exception as e:
        print(f"⚠️ Erro ao processar edição com IA: {e}")

# ==========================================
# 6. FLUXO PRINCIPAL DE GERAÇÃO DE PRÉVIAS
# ==========================================
def processar_fila_instagram():
    verificar_respostas_do_admin()
    
    print("\n🎨 Verificando fila de criação para o Instagram...")
    registros = planilha.get_all_records()
    gerados_nesta_rodada = 0
    
    for indice, linha in enumerate(registros, start=2):
        postar = str(linha.get('Postar_Insta', '')).upper()
        status_insta = str(linha.get('Status_Insta', ''))
        
        if postar == "SIM" and (status_insta in ["", "Aguardando Geracao"] or status_insta.startswith("Regerar")):
            produto = str(linha.get('Produto', ''))
            preco_antigo = str(linha.get('Preco_Antigo', ''))
            preco_novo = str(linha.get('Preco_Novo', ''))
            cupom = str(linha.get('Cupom', ''))
            condicao = str(linha.get('Condicao', ''))
            url_img = str(linha.get('Imagem_URL', ''))
            formato = str(linha.get('Formato_Insta', 'FOTO')).upper()
            
            instrucao_extra = status_insta.split("|")[1] if "|" in status_insta else ""
            print(f"🎬 Gerando {formato} para aprovação: {produto}...")
            
            try:
                textos = criar_textos_insta(produto, preco_antigo, preco_novo, cupom, condicao, instrucao_extra)
                legenda = linha.get('Legenda_Insta') if (status_insta.startswith("Regerar") and linha.get('Legenda_Insta')) else textos['legenda']
                
                if formato == "VIDEO":
                    caminho_midia = gerar_reels_mp4(produto, preco_antigo, preco_novo, cupom, url_img, textos['narracao'])
                else:
                    caminho_midia = desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="FOTO")
                    
                msg_id = enviar_para_aprovacao_admin(indice, formato, caminho_midia, legenda)
                
                if msg_id:
                    planilha.update_cell(indice, COL_STATUS_INSTA, "Em Aprovacao")
                    planilha.update_cell(indice, COL_LEGENDA_INSTA, legenda)
                    print(f"   ✅ Prévia enviada para o seu Telegram privado (Msg ID: {msg_id})!")
                    gerados_nesta_rodada += 1
                    
                if gerados_nesta_rodada >= 2:
                    break
            except Exception as e:
                print(f"   ❌ Erro ao gerar mídia da linha {indice}: {e}")

if __name__ == "__main__":
    processar_fila_instagram()
