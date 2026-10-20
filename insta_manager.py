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
GEMINI_API_KEY = (os.getenv('GEMINI_API_KEY') or '').strip()
NOME_DA_PLANILHA = (os.getenv('NOME_DA_PLANILHA') or 'BugdasPromos').strip()
TELEGRAM_TOKEN = (os.getenv('TELEGRAM_TOKEN') or '').strip()
ADMIN_CHAT_ID = (os.getenv('ADMIN_CHAT_ID') or '5096433671').strip()
DESCONTO_MINIMO_PCT = 20.0

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0'}

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

COL_PRODUTO = 1
COL_PRECO_ANTIGO = 2
COL_PRECO_NOVO = 3
COL_STATUS_TG = 6
COL_CUPOM = 7
COL_CONDICAO = 8
COL_POSTAR_INSTA = 9
COL_FORMATO_INSTA = 10
COL_STATUS_INSTA = 11
COL_LEGENDA_INSTA = 12

CUPONS_INVALIDOS = {
    'NENHUM', 'VAZIO', 'SEM CUPOM', 'NAO', 'NÃO', 'NONE', 'NULL',
    'PRIME', 'NO APP', 'APP', 'PIX', 'NO PIX'
}

# ==========================================
# 3. VALIDAÇÃO DE PREÇO, DESIGN E VÍDEO
# ==========================================
def carregar_fonte(tamanho, negrito=True):
    caminhos = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrito else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if negrito else "C:/Windows/Fonts/arial.ttf"
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

def preco_para_float(valor_str):
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

def limpar_cupom(cupom_raw):
    if not cupom_raw:
        return ""
    c = str(cupom_raw).strip().upper()
    if c in CUPONS_INVALIDOS or len(c) < 3 or len(c) > 25:
        return ""
    for concorrente in ['ML', 'MELI', 'SHOPEE', 'MAGALU', 'ALI', 'KABUM']:
        if concorrente in c:
            return ""
    return c

def validar_oferta_para_publicacao(preco_antigo_raw, preco_novo_raw, cupom_raw):
    """Retorna (valida: bool, resumo: str, desconto_pct: float, cupom_limpo: str)."""
    p_antigo = preco_para_float(preco_antigo_raw)
    p_novo = preco_para_float(preco_novo_raw)
    cupom = limpar_cupom(cupom_raw)

    if p_novo <= 0:
        return False, "Preço promocional inválido", 0.0, ""

    desconto_pct = 0.0
    if p_antigo > p_novo:
        desconto_pct = ((p_antigo - p_novo) / p_antigo) * 100.0
        if desconto_pct >= 95.0:
            return False, f"Desconto irreal ({desconto_pct:.0f}%)", desconto_pct, cupom

    if desconto_pct >= DESCONTO_MINIMO_PCT:
        return True, f"🔥 {desconto_pct:.0f}% OFF", desconto_pct, cupom
    if cupom:
        return True, f"🎟️ Cupom {cupom}", desconto_pct, cupom

    return False, f"Desconto abaixo de 30% ({desconto_pct:.1f}%) e sem cupom válido", desconto_pct, ""

def formatar_preco(valor):
    """Garante R$, separador de milhar e centavos corretos."""
    val_float = preco_para_float(valor)
    if val_float <= 0:
        return ""
    inteiro = int(val_float)
    centavos = int(round((val_float - inteiro) * 100))
    inteiro_fmt = f"{inteiro:,}".replace(",", ".")
    return f"R$ {inteiro_fmt},{centavos:02d}"

def preco_para_fala(preco_formatado):
    """Converte 'R$ 1.749,90' em '1749 reais e 90 centavos' para pronúncia limpa no TTS."""
    val_float = preco_para_float(preco_formatado)
    if val_float <= 0:
        return preco_formatado
    reais = int(val_float)
    centavos = int(round((val_float - reais) * 100))
    if centavos > 0:
        return f"{reais} reais e {centavos} centavos"
    return f"{reais} reais"

def desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="FOTO", desconto_pct=0.0, caminho_saida="post_insta.jpg"):
    largura = 1080
    altura = 1080 if formato == "FOTO" else 1920

    img = Image.new('RGB', (largura, altura), color=(30, 70, 190))
    draw = ImageDraw.Draw(img)

    offset_y = 60 if formato == "FOTO" else 220

    # 1. Topo: Selo "BUG DAS PROMOS"
    draw.rounded_rectangle([(80, offset_y), (440, offset_y + 70)], radius=35, fill=(255, 255, 255))
    f_selo = carregar_fonte(28, True)
    draw.text((110, offset_y + 18), "BUG DAS PROMOS", fill=(30, 70, 190), font=f_selo)

    f_semana = carregar_fonte(32, True if desconto_pct >= DESCONTO_MINIMO_PCT else False)
    subtitulo_topo = f"🔥 {desconto_pct:.0f}% DE DESCONTO" if desconto_pct >= DESCONTO_MINIMO_PCT else "oferta exclusiva"
    draw.text((460, offset_y + 15), subtitulo_topo, fill=(255, 200, 50) if desconto_pct >= DESCONTO_MINIMO_PCT else (255, 255, 255), font=f_semana)

    # 2. Quadro Branco Central para a Imagem
    caixa_y1 = offset_y + 110
    altura_caixa = 480 if formato == "FOTO" else 650
    caixa_y2 = caixa_y1 + altura_caixa
    draw.rounded_rectangle([(140, caixa_y1), (940, caixa_y2)], radius=40, fill=(255, 255, 255))

    try:
        resp = requests.get(url_img, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            prod_img = Image.open(BytesIO(resp.content)).convert("RGBA")
            larg_img, alt_img = prod_img.size
            if "media-amazon.com" not in str(url_img) and "images-amazon.com" not in str(url_img):
                prod_img = prod_img.crop((0, int(alt_img * 0.15), larg_img, alt_img))

            fundo_branco = Image.new("RGBA", prod_img.size, "WHITE")
            fundo_branco.paste(prod_img, (0, 0), prod_img)
            prod_img = fundo_branco.convert("RGB")

            prod_img.thumbnail((760, altura_caixa - 40), Image.Resampling.LANCZOS)
            px = (largura - prod_img.width) // 2
            py = caixa_y1 + (altura_caixa - prod_img.height) // 2
            img.paste(prod_img, (px, py))
    except Exception as e:
        print(f"⚠️ Aviso imagem: {e}")

    # 3. Nome do Produto
    y_texto = caixa_y2 + 45
    f_prod = carregar_fonte(36, False)
    linhas = quebrar_texto(produto, f_prod, 900, draw)
    for linha in linhas:
        bbox = draw.textbbox((0, 0), linha, font=f_prod)
        draw.text(((largura - (bbox[2] - bbox[0])) / 2, y_texto), linha, fill=(255, 255, 255), font=f_prod)
        y_texto += 45

    y_texto += 25

    # 4. Preço Antigo
    if preco_antigo:
        f_antigo = carregar_fonte(45, False)
        txt_antigo = f"de {preco_antigo}"
        bbox = draw.textbbox((0, 0), txt_antigo, font=f_antigo)
        x_ant = (largura - (bbox[2] - bbox[0])) / 2
        draw.text((x_ant, y_texto), txt_antigo, fill=(200, 210, 255), font=f_antigo)
        meio_y = y_texto + (bbox[3] - bbox[1]) / 2 + 5
        draw.line([(x_ant - 10, meio_y), (x_ant + (bbox[2] - bbox[0]) + 10, meio_y)], fill=(255, 255, 255), width=4)
        y_texto += 60

    # 5. Preço Novo Gigante e Laranja
    f_novo_menor = carregar_fonte(50, True)
    f_novo_maior = carregar_fonte(95, True)

    txt_por = "por "
    bbox_por = draw.textbbox((0, 0), txt_por, font=f_novo_menor)
    bbox_preco = draw.textbbox((0, 0), preco_novo, font=f_novo_maior)

    largura_total_preco = (bbox_por[2] - bbox_por[0]) + (bbox_preco[2] - bbox_preco[0])
    x_inicial_preco = (largura - largura_total_preco) / 2

    draw.text((x_inicial_preco, y_texto + 35), txt_por, fill=(255, 170, 50), font=f_novo_menor)
    draw.text((x_inicial_preco + (bbox_por[2] - bbox_por[0]), y_texto), preco_novo, fill=(255, 170, 50), font=f_novo_maior)
    y_texto += 120

    # 6. Cupom
    if cupom:
        f_cupom = carregar_fonte(30, True)
        txt_cupom = f"CUPOM: {cupom.upper()}"
        bbox = draw.textbbox((0, 0), txt_cupom, font=f_cupom)
        draw.text(((largura - (bbox[2] - bbox[0])) / 2, y_texto), txt_cupom, fill=(255, 255, 255), font=f_cupom)

    # 7. Rodapé (Foco no Telegram)
    rodape_y = altura - (70 if formato == "FOTO" else 200)
    f_cta = carregar_fonte(26, True)
    txt_cta = "🔗 ACESSE O GRUPO DO TELEGRAM NA BIO!"
    bbox = draw.textbbox((0, 0), txt_cta, font=f_cta)
    draw.text(((largura - (bbox[2] - bbox[0])) / 2, rodape_y), txt_cta, fill=(200, 210, 255), font=f_cta)

    img.save(caminho_saida, quality=95)
    return caminho_saida

def gerar_reels_mp4(produto, preco_antigo, preco_novo, cupom, url_img, texto_narracao, desconto_pct=0.0):
    caminho_img = "frame_reels.jpg"
    caminho_audio = "locucao.mp3"
    caminho_video = "reels_final.mp4"

    desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="VIDEO", desconto_pct=desconto_pct, caminho_saida=caminho_img)

    texto_limpo = texto_narracao.replace('"', '').replace("'", "").replace("\n", " ")
    if preco_novo and preco_novo in texto_limpo:
        texto_limpo = texto_limpo.replace(preco_novo, preco_para_fala(preco_novo))

    try:
        subprocess.run([
            "edge-tts", "--voice", "pt-BR-FranciscaNeural",
            "--text", texto_limpo, "--write-media", caminho_audio
        ], check=True, timeout=30)
    except Exception as e:
        print(f"   ⚠️ Aviso edge-tts ({e}), gerando trilha de áudio silenciosa de fallback...")
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "7", "-q:a", "9", "-acodec", "libmp3lame", caminho_audio
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", caminho_img, "-i", caminho_audio,
        "-vf", "zoompan=z='min(zoom+0.0005,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=900:s=1080x1920:fps=30",
        "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p", "-shortest", caminho_video
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return caminho_video

# ==========================================
# 4. TEXTOS COM IA E ENVIO PARA APROVAÇÃO
# ==========================================
MODELOS_GEMINI = ['gemini-3.8-flash-lite', 'gemini-3.8-flash']
MODELOS_INDISPONIVEIS = set()

def criar_textos_insta(produto, preco_antigo, preco_novo, cupom, condicao, instrucao_extra="", desconto_pct=0.0):
    obs = f"\nATENÇÃO - O DONO PEDIU ESTA ALTERAÇÃO: {instrucao_extra}" if instrucao_extra else ""
    preco_fala = preco_para_fala(preco_novo)
    prompt = f"""
    Crie o conteúdo de um post de Instagram para a página @BugdasPromos:
    Produto: {produto}
    Preço Antigo: {preco_antigo}
    Preço Promoção: {preco_novo} ({desconto_pct:.0f}% OFF)
    Preço por extenso para locução: {preco_fala}
    Cupom: {cupom} | Condição: {condicao}{obs}

    Retorne APENAS um JSON válido com estas 2 chaves:
    {{
        "legenda": "Legenda engajadora, focada em guiar a pessoa para clicar no link da BIO e entrar no nosso grupo do TELEGRAM. NUNCA mencione envio de link por direct e NUNCA coloque o link direto no texto. Inclua 5 hashtags.",
        "narracao": "Texto muito natural e vibrante (máximo 22 palavras) para locução de vídeo, escrevendo o preço por extenso ({preco_fala}). Chame para o grupo do Telegram na Bio!"
    }}
    """
    for modelo in MODELOS_GEMINI:
        if modelo in MODELOS_INDISPONIVEIS:
            continue
        try:
            resp = cliente_gemini.models.generate_content(
                model=modelo,
                contents=prompt,
                config={'response_mime_type': 'application/json'}
            )
            return json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
        except Exception:
            MODELOS_INDISPONIVEIS.add(modelo)
            continue

    bloco_de = f"❌ De: {preco_antigo}\n" if preco_antigo else ""
    bloco_cupom = f"🎟️ Cupom: {cupom}\n" if cupom else ""
    tag_off = f" ({desconto_pct:.0f}% OFF)" if desconto_pct >= DESCONTO_MINIMO_PCT else ""
    return {
        "legenda": f"🚨 Oferta verificada na Amazon!{tag_off}\n\n📦 {produto}\n{bloco_de}✅ Por: {preco_novo} {condicao}\n{bloco_cupom}\n📲 Acesse o link da nossa Bio e entre no grupo do Telegram para garantir essa oferta antes que acabe!\n\n#ofertas #promocao #amazonbrasil #achadinhos #descontos",
        "narracao": f"Olha esse desconto na Amazon! {produto} saindo por apenas {preco_fala}! Corre pro nosso grupo do telegram no link da bio!"
    }

def enviar_para_aprovacao_admin(indice_linha, formato, caminho_midia, legenda, resumo_val):
    teclado = {
        "inline_keyboard": [
            [
                {"text": "✅ Aprovar (Pronto)", "callback_data": f"aprovar_{indice_linha}"},
                {"text": "❌ Descartar", "callback_data": f"rejeitar_{indice_linha}"}
            ]
        ]
    }
    texto_msg = f"🎨 PRÉVIA INSTAGRAM (Linha {indice_linha}) | {formato} | {resumo_val}\n\n📝 Legenda:\n{legenda}\n\n💡 Para editar, clique em RESPONDER (Reply) a esta mensagem!"
    endpoint = "sendPhoto" if formato == "FOTO" else "sendVideo"
    campo = "photo" if formato == "FOTO" else "video"

    with open(caminho_midia, "rb") as f:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}",
            data={"chat_id": ADMIN_CHAT_ID, "caption": texto_msg[:1024], "reply_markup": json.dumps(teclado)},
            files={campo: f}
        ).json()
        return resp.get("result", {}).get("message_id")

# ==========================================
# 5. LEITURA DE CLIQUES E PEDIDOS DE EDIÇÃO
# ==========================================
def responder_callback(cb_id, texto="Comando registrado!"):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": texto},
            timeout=5
        )
    except Exception:
        pass

def atualizar_botoes_mensagem(cb, texto_status):
    try:
        if "message" in cb:
            chat_id = cb["message"]["chat"]["id"]
            msg_id = cb["message"]["message_id"]
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "reply_markup": {
                        "inline_keyboard": [[
                            {"text": texto_status, "callback_data": "noop"}
                        ]]
                    }
                },
                timeout=5
            )
    except Exception:
        pass

def interpretar_e_aplicar_sugestao_admin(linha_idx, texto_sugestao):
    try:
        linha_atual = planilha.row_values(linha_idx)
        prod_atual = linha_atual[0] if len(linha_atual) > 0 else ""
        antigo_atual = linha_atual[1] if len(linha_atual) > 1 else ""
        novo_atual = linha_atual[2] if len(linha_atual) > 2 else ""
        cupom_atual = linha_atual[6] if len(linha_atual) > 6 else ""

        prompt = f"""
        O administrador do canal de ofertas pediu uma alteração nesta oferta (Linha {linha_idx}):
        Mensagem do Admin: "{texto_sugestao}"

        Dados atuais da oferta na planilha:
        - Produto: {prod_atual}
        - Preço Antigo: {antigo_atual}
        - Preço Novo: {novo_atual}
        - Cupom: {cupom_atual}

        Retorne APENAS um JSON válido com os campos que devem ser alterados (ou null se não for para alterar):
        {{
            "novo_produto": "Novo nome limpo do produto ou null",
            "novo_preco_antigo": "Novo preco antigo formatado ou null (apenas números e vírgula, ex: 149,90)",
            "novo_preco_novo": "Novo preco promocional formatado ou null (apenas números e vírgula, ex: 99,90)",
            "novo_cupom": "Novo cupom ou null",
            "descartar": false,
            "resumo_mudancas": "Uma frase curta e objetiva em português confirmando o que foi alterado"
        }}
        """
        dados = None
        for modelo in MODELOS_GEMINI:
            if modelo in MODELOS_INDISPONIVEIS:
                continue
            try:
                resp = cliente_gemini.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config={'response_mime_type': 'application/json'}
                )
                dados = json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
                break
            except Exception:
                MODELOS_INDISPONIVEIS.add(modelo)
                continue

        if not dados:
            txt_l = texto_sugestao.lower()
            dados = {
                "novo_produto": None,
                "novo_preco_antigo": None,
                "novo_preco_novo": None,
                "novo_cupom": None,
                "descartar": any(p in txt_l for p in ["descart", "rejeit", "apaga", "exclui", "remove"]),
                "resumo_mudancas": f"Ajuste aplicado via comando: {texto_sugestao}"
            }
            m_preco = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})', texto_sugestao)
            if m_preco:
                if "antigo" in txt_l:
                    dados["novo_preco_antigo"] = m_preco.group(1)
                else:
                    dados["novo_preco_novo"] = m_preco.group(1)
            m_cupom = re.search(r'cupom\s+([A-Z0-9]{3,20})', texto_sugestao, re.IGNORECASE)
            if m_cupom:
                dados["novo_cupom"] = m_cupom.group(1).upper()

        if dados.get("descartar"):
            planilha.update_cell(linha_idx, COL_STATUS_TG, "Descartado")
            planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Descartado")
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha_idx} descartada conforme seu pedido!"}
            )
            return

        mudou = False
        if dados.get("novo_produto"):
            planilha.update_cell(linha_idx, COL_PRODUTO, str(dados["novo_produto"]).strip())
            mudou = True
        if dados.get("novo_preco_antigo"):
            planilha.update_cell(linha_idx, COL_PRECO_ANTIGO, str(dados["novo_preco_antigo"]).strip())
            mudou = True
        if dados.get("novo_preco_novo"):
            planilha.update_cell(linha_idx, COL_PRECO_NOVO, str(dados["novo_preco_novo"]).strip())
            mudou = True
        if dados.get("novo_cupom") is not None:
            planilha.update_cell(linha_idx, COL_CUPOM, str(dados["novo_cupom"]).strip().upper())
            mudou = True

        planilha.update_cell(linha_idx, COL_STATUS_TG, "")
        planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Aguardando Geracao")

        msg_feedback = dados.get("resumo_mudancas") or "Valores atualizados na planilha."
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_CHAT_ID, "text": f"✏️ Linha {linha_idx} atualizada!\n{msg_feedback}\n\n🔄 As prévias serão recriadas automaticamente com os novos dados."}
        )
    except Exception as e:
        print(f"⚠️ Erro ao interpretar sugestão: {e}")
        planilha.update_cell(linha_idx, COL_STATUS_INSTA, f"Regerar|{texto_sugestao}")
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_CHAT_ID, "text": f"🔄 Pedido registrado para refazer a Linha {linha_idx}..."}
        )

def verificar_respostas_do_admin():
    try:
        updates = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=10).json().get("result", [])
        maior_id = 0
        for up in updates:
            maior_id = max(maior_id, up["update_id"])
            if "callback_query" in up:
                cb = up["callback_query"]
                data = str(cb.get("data", "")).strip()
                cb_id = cb.get("id")

                match_linha = re.search(r'(\d+)$', data)
                if not match_linha:
                    responder_callback(cb_id, "Comando concluído")
                    continue
                linha = int(match_linha.group(1))

                if "aprovar_tg" in data:
                    responder_callback(cb_id, "🚀 Aprovado para o canal!")
                    atualizar_botoes_mensagem(cb, "✅ PUBLICADO NO CANAL")
                    planilha.update_cell(linha, COL_STATUS_TG, "Aprovado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"✅ Linha {linha} APROVADA para o Canal do Telegram!"})
                elif "rejeitar_tg" in data:
                    responder_callback(cb_id, "❌ Descartado do canal!")
                    atualizar_botoes_mensagem(cb, "❌ DESCARTADO DO CANAL")
                    planilha.update_cell(linha, COL_STATUS_TG, "Descartado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha} DESCARTADA do Canal!"})
                elif "aprovar" in data:
                    responder_callback(cb_id, "✅ Aprovado para o Instagram!")
                    atualizar_botoes_mensagem(cb, "✅ APROVADO PARA INSTAGRAM")
                    planilha.update_cell(linha, COL_STATUS_INSTA, "Aprovado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"✅ Linha {linha} (Instagram) APROVADA!"})
                elif "rejeitar" in data:
                    responder_callback(cb_id, "❌ Descartado do Instagram!")
                    atualizar_botoes_mensagem(cb, "❌ DESCARTADO DO INSTAGRAM")
                    planilha.update_cell(linha, COL_STATUS_INSTA, "Descartado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha} (Instagram) DESCARTADA!"})

            elif "message" in up and "reply_to_message" in up["message"]:
                msg = up["message"]
                caption_original = msg["reply_to_message"].get("caption", "") or msg["reply_to_message"].get("text", "")
                match = re.search(r"Linha\s*(\d+)", caption_original)
                if match and msg.get("text"):
                    linha = int(match.group(1))
                    print(f"📩 Sugestão do Admin recebida para Linha {linha}: {msg['text']}")
                    interpretar_e_aplicar_sugestao_admin(linha, msg['text'])

        if maior_id > 0:
            requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={maior_id + 1}", timeout=5)
    except Exception as e:
        print(f"⚠️ Aviso ao verificar respostas do Admin: {e}")

# ==========================================
# 6. FLUXO PRINCIPAL
# ==========================================
def processar_fila_instagram():
    verificar_respostas_do_admin()
    print("\n🎨 Verificando fila de criação do Instagram...")
    registros = planilha.get_all_records()
    gerados = 0

    for indice, linha in enumerate(registros, start=2):
        postar_insta = str(linha.get('Postar_Insta', '')).upper()
        status = str(linha.get('Status_Insta', '')).strip()

        if (postar_insta == "SIM" and status in ["", "Aguardando Geracao"]) or status.startswith("Regerar"):
            produto = str(linha.get('Produto', '')).strip()
            if not produto:
                continue

            # Valida se a oferta cumpre a regra de >= 30% OFF ou Cupom válido
            valida, resumo_val, desconto_pct, cupom_limpo = validar_oferta_para_publicacao(
                linha.get('Preco_Antigo', ''),
                linha.get('Preco_Novo', ''),
                linha.get('Cupom', '')
            )

            if not valida:
                print(f"⏭️ Linha {indice} ({produto}) descartada do Instagram: {resumo_val}")
                planilha.update_cell(indice, COL_STATUS_INSTA, f"Descartado: {resumo_val}")
                continue

            preco_antigo = formatar_preco(str(linha.get('Preco_Antigo', '')))
            preco_novo = formatar_preco(str(linha.get('Preco_Novo', '')))
            condicao = str(linha.get('Condicao', ''))
            url_img = str(linha.get('Imagem_URL', ''))
            formato = str(linha.get('Formato_Insta', 'FOTO')).upper()

            # Trava anti-ticket baixo para Reels (< R$ 45 vira FOTO)
            if preco_para_float(linha.get('Preco_Novo', '')) < 45.0:
                formato = "FOTO"

            inst_extra = status.split("|")[1] if "|" in status else ""

            try:
                print(f"🎬 Gerando criativo ({formato}) da linha {indice}: {produto} ({resumo_val})")
                textos = criar_textos_insta(produto, preco_antigo, preco_novo, cupom_limpo, condicao, inst_extra, desconto_pct=desconto_pct)
                legenda = textos['legenda']

                if formato == "VIDEO":
                    caminho_midia = gerar_reels_mp4(produto, preco_antigo, preco_novo, cupom_limpo, url_img, textos['narracao'], desconto_pct=desconto_pct)
                else:
                    caminho_midia = desenhar_arte(produto, preco_antigo, preco_novo, cupom_limpo, url_img, formato="FOTO", desconto_pct=desconto_pct)

                msg_id = enviar_para_aprovacao_admin(indice, formato, caminho_midia, legenda, resumo_val)
                if msg_id:
                    planilha.update_cell(indice, COL_STATUS_INSTA, "Em Aprovacao")
                    planilha.update_cell(indice, COL_LEGENDA_INSTA, legenda)
                    gerados += 1
                    print(f"   ✅ Prévia enviada ao Admin (msg_id={msg_id})!")
                if gerados >= 2:
                    break
            except Exception as e:
                print(f"❌ Erro linha {indice}: {e}")

if __name__ == "__main__":
    processar_fila_instagram()
