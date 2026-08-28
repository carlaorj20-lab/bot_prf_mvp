"""
MVP Bot de Gestão de Liberações - PRF (DEL0701 - Alto da Serra)
Compatível com Z-API, PostgreSQL (Supabase) via psycopg3 e SQLite.
"""

import os
import re
import sqlite3
from datetime import datetime, time, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import requests

# Suporte ao PostgreSQL via psycopg3
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    import psycopg
    # Corrige o prefixo para a sintaxe do psycopg3 se vier como postgres://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)

# Configurações de Credenciais da Z-API
ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")

# Nomes exatos dos grupos no WhatsApp
GRUPO_CHAMADOS = "LIBERAÇÃO REMOTA - Alto da Serra - DEL0701"
GRUPO_GESTAO = "DEL 7/1 - GESTÃO DE LIBERAÇÕES"

# ==========================================
# GERENCIAMENTO DO BANCO DE DADOS
# ==========================================

def get_db():
    """Conecta ao PostgreSQL usando psycopg3 ou faz fallback para SQLite."""
    if DATABASE_URL:
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect("bot_prf.db")

def execute_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Helper universal compatível com psycopg3 e SQLite."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Adapta os placeholders (%s para PostgreSQL/psycopg3 e ? para SQLite)
    if not DATABASE_URL:
        query = query.replace("%s", "?")
        
    cursor.execute(query, params)
    
    result = None
    if fetchone:
        result = cursor.fetchone()
    elif fetchall:
        result = cursor.fetchall()
        
    if commit:
        conn.commit()
        
    cursor.close()
    conn.close()
    return result

def init_db():
    """Cria a estrutura inicial das tabelas no banco de dados."""
    execute_query("""
        CREATE TABLE IF NOT EXISTS policiais (
            id SERIAL PRIMARY KEY,
            whatsapp_id VARCHAR(100) UNIQUE NOT NULL,
            nome VARCHAR(100) NOT NULL,
            inicio_expediente VARCHAR(10),
            fim_expediente VARCHAR(10),
            status VARCHAR(30) DEFAULT 'INDISPONIVEL',
            atendimentos_liberacao INTEGER DEFAULT 0,
            atendimentos_outros INTEGER DEFAULT 0,
            ordem_prioridade INTEGER UNIQUE NOT NULL
        )
    """, commit=True)
    
    execute_query("""
        CREATE TABLE IF NOT EXISTS chamados (
            id SERIAL PRIMARY KEY,
            tipo VARCHAR(20) NOT NULL,
            status VARCHAR(30) DEFAULT 'AGUARDANDO_RESPOSTA',
            policial_designado_id INTEGER REFERENCES policiais(id),
            data_hora_alerta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, commit=True)
    
    execute_query("""
        CREATE TABLE IF NOT EXISTS grupos_zapi (
            nome_grupo VARCHAR(150) PRIMARY KEY,
            zapi_phone_id VARCHAR(100) NOT NULL
        )
    """, commit=True)

# ==========================================
# COMUNICAÇÃO COM Z-API
# ==========================================

def enviar_mensagem_zapi(grupo_phone_id, texto):
    """Dispara mensagens via REST API da Z-API."""
    if not ZAPI_INSTANCE or not ZAPI_TOKEN:
        print("[ERRO Z-API] Instância ou Token não configurados nas variáveis de ambiente.")
        return

    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
    headers = {
        "Content-Type": "application/json",
        "Client-Token": ZAPI_CLIENT_TOKEN
    }
    payload = {
        "phone": grupo_phone_id,
        "message": texto
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[Z-API STATUS]: {r.status_code}")
    except Exception as e:
        print(f"[ERRO ENVIO Z-API]: {e}")

def salvar_id_grupo(nome_grupo, phone_id):
    """Mapeia o ID interno do grupo no WhatsApp para envios posteriores."""
    query_check = "SELECT nome_grupo FROM grupos_zapi WHERE nome_grupo = %s"
    if execute_query(query_check, (nome_grupo,), fetchone=True):
        execute_query("UPDATE grupos_zapi SET zapi_phone_id = %s WHERE nome_grupo = %s", (phone_id, nome_grupo), commit=True)
    else:
        execute_query("INSERT INTO grupos_zapi (nome_grupo, zapi_phone_id) VALUES (%s, %s)", (nome_grupo, phone_id), commit=True)

def obter_id_grupo(nome_grupo):
    """Recupera o ID salvo do grupo."""
    res = execute_query("SELECT zapi_phone_id FROM grupos_zapi WHERE nome_grupo = %s", (nome_grupo,), fetchone=True)
    return res[0] if res else None

# ==========================================
# REGRAS E ESCALA DA PRF
# ==========================================

def dentro_do_horario_comercial():
    agora = datetime.now()
    if agora.weekday() >= 5:
        return False
    horario = agora.time()
    return (time(8, 0) <= horario < time(12, 0)) or (time(13, 0) <= horario < time(17, 0))

def buscar_proximo_policial(tipo_chamado):
    agora_str = datetime.now().strftime("%H:%M")
    query = """
        SELECT id, whatsapp_id, nome 
        FROM policiais 
        WHERE status = 'DISPONIVEL'
          AND inicio_expediente IS NOT NULL 
          AND fim_expediente IS NOT NULL
          AND %s >= inicio_expediente 
          AND %s < fim_expediente
        ORDER BY 
            CASE WHEN %s = 'LIBERACAO' THEN atendimentos_liberacao ELSE atendimentos_outros END ASC,
            ordem_prioridade ASC
        LIMIT 1
    """
    return execute_query(query, (agora_str, agora_str, tipo_chamado), fetchone=True)

# ==========================================
# ROTINAS AGENDADAS (CRON JOBS)
# ==========================================

def verificar_timeout_15min():
    """Verifica chamados sem resposta a mais de 15 minutos."""
    if not dentro_do_horario_comercial():
        return

    limite = datetime.now() - timedelta(minutes=15)
    query = """
        SELECT c.id, c.tipo, p.id, p.whatsapp_id, p.nome 
        FROM chamados c
        JOIN policiais p ON c.policial_designado_id = p.id
        WHERE c.status = 'AGUARDANDO_RESPOSTA' AND c.data_hora_alerta <= %s
    """
    expirados = execute_query(query, (limite,), fetchall=True) or []
    
    id_grupo_chamados = obter_id_grupo(GRUPO_CHAMADOS)
    id_grupo_gestao = obter_id_grupo(GRUPO_GESTAO)

    for c_id, tipo, p_id, p_wpid, p_nome in expirados:
        if id_grupo_chamados:
            enviar_mensagem_zapi(id_grupo_chamados, f"@{p_nome} Chamado não atendido/justificado em 15 minutos.")
            
        execute_query("UPDATE chamados SET status = 'EXPIRED' WHERE id = %s", (c_id,), commit=True)
        execute_query("UPDATE policiais SET status = 'INDISPONIVEL' WHERE id = %s", (p_id,), commit=True)
        
        if id_grupo_gestao:
            enviar_mensagem_zapi(id_grupo_gestao, f"⚠️ {p_nome} ultrapassou 15 min sem resposta. Repassando chamado de {tipo}.")
            
            proximo = buscar_proximo_policial(tipo)
            if proximo:
                prox_id, prox_wpid, prox_nome = proximo
                execute_query(
                    "INSERT INTO chamados (tipo, status, policial_designado_id, data_hora_alerta) VALUES (%s, 'AGUARDANDO_RESPOSTA', %s, %s)",
                    (tipo, prox_id, datetime.now()), commit=True
                )
                enviar_mensagem_zapi(id_grupo_gestao, f"@{prox_nome} Por favor, atender ao chamado no grupo das liberações")

def reset_diario():
    """Zera o placar diário às 00:00."""
    execute_query("""
        UPDATE policiais 
        SET atendimentos_liberacao = 0, atendimentos_outros = 0, status = 'INDISPONIVEL',
            inicio_expediente = NULL, fim_expediente = NULL
    """, commit=True)

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(verificar_timeout_15min, 'interval', minutes=1)
scheduler.add_job(reset_diario, 'cron', hour=0, minute=0)
scheduler.start()

# ==========================================
# RECEPTOR DO WEBHOOK
# ==========================================

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    print(f"[PAYLOAD Z-API]: {data}")

    is_group = data.get('isGroup', False)
    nome_chat = data.get('chatName', '')
    phone_chat = data.get('phone', '')
    remetente_fone = data.get('participantPhone', '') or data.get('authorPhone', '')
    
    mensagem = ""
    if isinstance(data.get('text'), dict):
        mensagem = data.get('text', {}).get('message', '')
    elif isinstance(data.get('body'), str):
        mensagem = data.get('body', '')

    if not is_group or not mensagem:
        return jsonify({"status": "Ignorado"}), 200

    # 1. GRUPO DE CHAMADOS DA REMOTA
    if GRUPO_CHAMADOS.lower() in nome_chat.lower():
        salvar_id_grupo(GRUPO_CHAMADOS, phone_chat)
        
        if not dentro_do_horario_comercial():
            return jsonify({"status": "Fora do horário comercial"}), 200

        texto_lc = mensagem.lower()
        tipo = 'LIBERACAO' if any(w in texto_lc for w in ['libera', 'veiculo', 'pátio', 'patio']) else 'OUTROS'
        
        policial = buscar_proximo_policial(tipo)
        id_gestao = obter_id_grupo(GRUPO_GESTAO)
        
        if policial:
            p_id, p_wpid, p_nome = policial
            execute_query(
                "INSERT INTO chamados (tipo, status, policial_designado_id, data_hora_alerta) VALUES (%s, 'AGUARDANDO_RESPOSTA', %s, %s)",
                (tipo, p_id, datetime.now()), commit=True
            )
            if id_gestao:
                enviar_mensagem_zapi(id_gestao, f"@{p_nome} Por favor, atender ao chamado no grupo das liberações")
        else:
            if id_gestao:
                enviar_mensagem_zapi(id_gestao, f"⚠️ Novo chamado de {tipo} recebido, mas nenhum policial está disponível!")

    # 2. GRUPO DE GESTÃO E COMANDOS
    elif GRUPO_GESTAO.lower() in nome_chat.lower():
        salvar_id_grupo(GRUPO_GESTAO, phone_chat)

        if mensagem.startswith('!expediente'):
            match = re.search(r'(?:@(\w+))?\s*(\d{2}:\d{2})\s*(\d{2}:\d{2})', mensagem)
            if match:
                nome_alvo, inicio, fim = match.groups()
                if nome_alvo:
                    p = execute_query("SELECT id, nome FROM policiais WHERE nome ILIKE %s", (f"%{nome_alvo}%",), fetchone=True)
                else:
                    p = execute_query("SELECT id, nome FROM policiais WHERE whatsapp_id LIKE %s", (f"%{remetente_fone}%",), fetchone=True)
                
                if p:
                    execute_query(
                        "UPDATE policiais SET inicio_expediente = %s, fim_expediente = %s, status = 'DISPONIVEL' WHERE id = %s",
                        (inicio, fim, p[0]), commit=True
                    )
                    enviar_mensagem_zapi(phone_chat, f"✅ Expediente de {p[1]} definido para {inicio} às {fim}. Status: DISPONÍVEL.")
                else:
                    enviar_mensagem_zapi(phone_chat, "❌ Policial não cadastrado no banco de dados.")

        elif mensagem.strip() == '!disponivel':
            p = execute_query("SELECT id, nome FROM policiais WHERE whatsapp_id LIKE %s", (f"%{remetente_fone}%",), fetchone=True)
            if p:
                execute_query("UPDATE policiais SET status = 'DISPONIVEL' WHERE id = %s", (p[0],), commit=True)
                enviar_mensagem_zapi(phone_chat, f"🟢 {p[1]} está novamente DISPONÍVEL na fila.")

        else:
            chamado = execute_query("""
                SELECT c.id, c.tipo, p.id, p.nome 
                FROM chamados c
                JOIN policiais p ON c.policial_designado_id = p.id
                WHERE p.whatsapp_id LIKE %s AND c.status = 'AGUARDANDO_RESPOSTA'
                ORDER BY c.id DESC LIMIT 1
            """, (f"%{remetente_fone}%",), fetchone=True)

            if chamado:
                c_id, tipo, p_id, p_nome = chamado
                
                if len(mensagem.strip()) >= 5:
                    enviar_mensagem_zapi(phone_chat, f"📋 Justificativa recebida de {p_nome}: \"{mensagem.strip()}\"")
                    execute_query("UPDATE chamados SET status = 'EXPIRED' WHERE id = %s", (c_id,), commit=True)
                    execute_query("UPDATE policiais SET status = 'INDISPONIVEL' WHERE id = %s", (p_id,), commit=True)
                    
                    proximo = buscar_proximo_policial(tipo)
                    if proximo:
                        prox_id, prox_wpid, prox_nome = proximo
                        execute_query(
                            "INSERT INTO chamados (tipo, status, policial_designado_id, data_hora_alerta) VALUES (%s, 'AGUARDANDO_RESPOSTA', %s, %s)",
                            (tipo, prox_id, datetime.now()), commit=True
                        )
                        enviar_mensagem_zapi(phone_chat, f"@{prox_nome} Por favor, atender ao chamado no grupo das liberações")
                else:
                    execute_query("UPDATE chamados SET status = 'EM_ATENDIMENTO' WHERE id = %s", (c_id,), commit=True)
                    execute_query("UPDATE policiais SET status = 'EM_ATENDIMENTO' WHERE id = %s", (p_id,), commit=True)
                    
                    coluna = "atendimentos_liberacao" if tipo == 'LIBERACAO' else "atendimentos_outros"
                    execute_query(f"UPDATE policiais SET {coluna} = {coluna} + 1 WHERE id = %s", (p_id,), commit=True)
                    enviar_mensagem_zapi(phone_chat, f"👍 {p_nome} assumiu o chamado de {tipo}.")

    return jsonify({"status": "success"}), 200

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))