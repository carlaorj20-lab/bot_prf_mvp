import os
import re
import sqlite3
from datetime import datetime, time, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import requests

app = Flask(__name__)

# Configurações de Grupos e API
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "https://sua-api.koyeb.app")
API_KEY = os.getenv("EVOLUTION_API_KEY", "SUA_CHAVE_API")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "instancia_prf")

GRUPO_CHAMADOS = "LIBERAÇÃO REMOTA - Alto da Serra - DEL0701"
GRUPO_GESTAO = "DEL 7/1 - GESTÃO DE LIBERAÇÕES"
DB_PATH = "bot_prf.db"

# ==========================================
# BANCO DE DADOS
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS policiais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            whatsapp_id TEXT UNIQUE NOT NULL,
            nome TEXT NOT NULL,
            inicio_expediente TEXT,
            fim_expediente TEXT,
            status TEXT DEFAULT 'INDISPONIVEL', -- 'DISPONIVEL', 'EM_ATENDIMENTO', 'INDISPONIVEL'
            atendimentos_liberacao INTEGER DEFAULT 0,
            atendimentos_outros INTEGER DEFAULT 0,
            ordem_prioridade INTEGER UNIQUE NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chamados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL, -- 'LIBERACAO' ou 'OUTROS'
            status TEXT DEFAULT 'AGUARDANDO_RESPOSTA', -- 'AGUARDANDO_RESPOSTA', 'EM_ATENDIMENTO', 'EXPIRED'
            policial_designado_id INTEGER,
            data_hora_alerta DATETIME,
            FOREIGN KEY(policial_designado_id) REFERENCES policiais(id)
        )
    """)
    conn.commit()
    conn.close()

# ==========================================
# AUXILIARES E REGRAS DE NEGÓCIO
# ==========================================
def enviar_mensagem(grupo_nome, texto, mencao_jid=None):
    url = f"{EVOLUTION_API_URL}/message/sendText/{INSTANCE_NAME}"
    payload = {
        "number": grupo_nome,
        "options": {"delay": 500},
        "textMessage": {"text": texto}
    }
    if mencao_jid:
        payload["textMessage"]["mentioned"] = [mencao_jid]
        
    headers = {"apikey": API_KEY, "Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"[ERRO ENVIO] {e}")

def dentro_do_horario_comercial():
    agora = datetime.now()
    if agora.weekday() >= 5: # Sábado/Domingo
        return False
    horario = agora.time()
    return (time(8, 0) <= horario < time(12, 0)) or (time(13, 0) <= horario < time(17, 0))

def buscar_proximo_policial(tipo_chamado):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    agora_str = datetime.now().strftime("%H:%M")
    
    # Busca o próximo respeitando: Disponibilidade, Expediente, Equalização do tipo e Prioridade da Fila
    cursor.execute("""
        SELECT id, whatsapp_id, nome 
        FROM policiais 
        WHERE status = 'DISPONIVEL'
          AND inicio_expediente IS NOT NULL 
          AND fim_expediente IS NOT NULL
          AND time(?) >= time(inicio_expediente) 
          AND time(?) < time(fim_expediente)
        ORDER BY 
            CASE WHEN ? = 'LIBERACAO' THEN atendimentos_liberacao ELSE atendimentos_outros END ASC,
            ordem_prioridade ASC
        LIMIT 1
    """, (agora_str, agora_str, tipo_chamado))
    
    policial = cursor.fetchone()
    conn.close()
    return policial

# ==========================================
# ROTINAS AGENDADAS (CRON JOBS)
# ==========================================
def verificar_timeout_15min():
    if not dentro_do_horario_comercial():
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    limite = datetime.now() - timedelta(minutes=15)
    
    cursor.execute("""
        SELECT c.id, c.tipo, p.id, p.whatsapp_id, p.nome 
        FROM chamados c
        JOIN policiais p ON c.policial_designado_id = p.id
        WHERE c.status = 'AGUARDANDO_RESPOSTA' AND c.data_hora_alerta <= ?
    """, (limite,))
    
    expirados = cursor.fetchall()
    for c_id, tipo, p_id, p_wpid, p_nome in expirados:
        # ÚNICA exceção que envia mensagem no Grupo de Chamados:
        enviar_mensagem(GRUPO_CHAMADOS, f"@{p_nome} Chamado não atendido/justificado em 15 minutos.", mencao_jid=p_wpid)
        
        cursor.execute("UPDATE chamados SET status = 'EXPIRED' WHERE id = ?", (c_id,))
        cursor.execute("UPDATE policiais SET status = 'INDISPONIVEL' WHERE id = ?", (p_id,))
        conn.commit()
        
        enviar_mensagem(GRUPO_GESTAO, f"⚠️ {p_nome} ultrapassou 15 min sem resposta. Passando chamado de {tipo} ao próximo.")
        
        # Passa chamado para o próximo da fila
        proximo = buscar_proximo_policial(tipo)
        if proximo:
            prox_id, prox_wpid, prox_nome = proximo
            cursor.execute("INSERT INTO chamados (tipo, policial_designado_id, data_hora_alerta) VALUES (?, ?, ?)",
                           (tipo, prox_id, datetime.now()))
            conn.commit()
            enviar_mensagem(GRUPO_GESTAO, f"@{prox_nome} Por favor, atender ao chamado no grupo das liberações", mencao_jid=prox_wpid)
            
    conn.close()

def reset_diario():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Zera o placar diário, mantendo os policiais e a ordem de prioridade intacta
    cursor.execute("""
        UPDATE policiais 
        SET atendimentos_liberacao = 0, atendimentos_outros = 0, status = 'INDISPONIVEL',
            inicio_expediente = NULL, fim_expediente = NULL
    """)
    conn.commit()
    conn.close()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(verificar_timeout_15min, 'interval', minutes=1)
scheduler.add_job(reset_diario, 'cron', hour=0, minute=0)
scheduler.start()

# ==========================================
# WEBHOOK PRINCIPAL
# ==========================================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    mensagem = data.get('data', {}).get('message', {}).get('conversation', '') or \
               data.get('data', {}).get('message', {}).get('extendedTextMessage', {}).get('text', '')
    grupo_jid = data.get('data', {}).get('key', {}).get('remoteJid', '')
    remetente_jid = data.get('data', {}).get('key', {}).get('participant', '')

    # 1. RECEBIMENTO DE CHAMADO NO GRUPO DE LIBERAÇÃO REMOTA
    if GRUPO_CHAMADOS in grupo_jid:
        if not dentro_do_horario_comercial():
            return jsonify({"status": "Fora do horário comercial"}), 200

        # Identifica tipo de chamado por palavra-chave
        texto_lc = mensagem.lower()
        tipo = 'LIBERACAO' if any(w in texto_lc for w in ['libera', 'veiculo', 'pátio', 'patio']) else 'OUTROS'
        
        policial = buscar_proximo_policial(tipo)
        if policial:
            p_id, p_wpid, p_nome = policial
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO chamados (tipo, policial_designado_id, data_hora_alerta) VALUES (?, ?, ?)",
                           (tipo, p_id, datetime.now()))
            conn.commit()
            conn.close()
            
            # Alerta exclusivamente no grupo de GESTÃO
            enviar_mensagem(GRUPO_GESTAO, f"@{p_nome} Por favor, atender ao chamado no grupo das liberações", mencao_jid=p_wpid)
        else:
            enviar_mensagem(GRUPO_GESTAO, f"⚠️ Chamado de {tipo} recebido, mas nenhum policial está disponível!")

    # 2. GESTÃO E RESPOSTAS NO GRUPO DE GESTÃO DE LIBERAÇÕES
    elif GRUPO_GESTAO in grupo_jid:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Registro ou alteração de expediente (!expediente 08:00 17:00 ou !expediente @nome 08:00 17:00)
        if mensagem.startswith('!expediente'):
            match = re.search(r'(?:@(\w+))?\s*(\d{2}:\d{2})\s*(\d{2}:\d{2})', mensagem)
            if match:
                nome_alvo, inicio, fim = match.groups()
                if nome_alvo:
                    cursor.execute("SELECT id, nome FROM policiais WHERE nome LIKE ?", (f"%{nome_alvo}%",))
                else:
                    cursor.execute("SELECT id, nome FROM policiais WHERE whatsapp_id = ?", (remetente_jid,))
                p = cursor.fetchone()
                if p:
                    cursor.execute("UPDATE policiais SET inicio_expediente = ?, fim_expediente = ?, status = 'DISPONIVEL' WHERE id = ?", (inicio, fim, p[0]))
                    conn.commit()
                    enviar_mensagem(GRUPO_GESTAO, f"✅ Expediente de {p[1]} configurado: {inicio} às {fim}.")

        # Policial finaliza atendimento e volta a ficar disponível
        elif mensagem.strip() == '!disponivel':
            cursor.execute("SELECT id, nome FROM policiais WHERE whatsapp_id = ?", (remetente_jid,))
            p = cursor.fetchone()
            if p:
                cursor.execute("UPDATE policiais SET status = 'DISPONIVEL' WHERE id = ?", (p[0],))
                conn.commit()
                enviar_mensagem(GRUPO_GESTAO, f"🟢 {p[1]} está novamente DISPONÍVEL na fila.")

        # Tratamento de Aceite do Chamado ou Justificativa de Impossibilidade
        else:
            cursor.execute("""
                SELECT c.id, c.tipo, p.id, p.nome 
                FROM chamados c
                JOIN policiais p ON c.policial_designado_id = p.id
                WHERE p.whatsapp_id = ? AND c.status = 'AGUARDANDO_RESPOSTA'
                ORDER BY c.id DESC LIMIT 1
            """, (remetente_jid,))
            chamado = cursor.fetchone()

            if chamado:
                c_id, tipo, p_id, p_nome = chamado
                # Se for justificativa (mínimo de 5 caracteres)
                if len(mensagem.strip()) >= 5:
                    enviar_mensagem(GRUPO_GESTAO, f"📋 Justificativa enviada por {p_nome}: \"{mensagem.strip()}\"")
                    cursor.execute("UPDATE chamados SET status = 'EXPIRED' WHERE id = ?", (c_id,))
                    cursor.execute("UPDATE policiais SET status = 'INDISPONIVEL' WHERE id = ?", (p_id,))
                    conn.commit()
                    
                    # Passa para o próximo policial
                    proximo = buscar_proximo_policial(tipo)
                    if proximo:
                        prox_id, prox_wpid, prox_nome = proximo
                        cursor.execute("INSERT INTO chamados (tipo, policial_designado_id, data_hora_alerta) VALUES (?, ?, ?)",
                                       (tipo, prox_id, datetime.now()))
                        conn.commit()
                        enviar_mensagem(GRUPO_GESTAO, f"@{prox_nome} Por favor, atender ao chamado no grupo das liberações", mencao_jid=prox_wpid)
                
                # Aceite por Emoji ou confirmação curta (< 5 caracteres)
                else:
                    cursor.execute("UPDATE chamados SET status = 'EM_ATENDIMENTO' WHERE id = ?", (c_id,))
                    cursor.execute("UPDATE policiais SET status = 'EM_ATENDIMENTO' WHERE id = ?", (p_id,))
                    if tipo == 'LIBERACAO':
                        cursor.execute("UPDATE policiais SET atendimentos_liberacao = atendimentos_liberacao + 1 WHERE id = ?", (p_id,))
                    else:
                        cursor.execute("UPDATE policiais SET atendimentos_outros = atendimentos_outros + 1 WHERE id = ?", (p_id,))
                    conn.commit()
                    enviar_mensagem(GRUPO_GESTAO, f"👍 {p_nome} assumiu o chamado de {tipo} e está temporariamente indisponível.")

        conn.close()
    return jsonify({"status": "success"}), 200

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))