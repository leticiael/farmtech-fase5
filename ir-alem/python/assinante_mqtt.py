"""Assina a telemetria do ESP32, classifica cada leitura e persiste em SQLite.

O modelo carregado aqui foi treinado sobre um rótulo sintético gerado por regra
de limiares (ver `treinar_classificador.py`). O veredito impresso reproduz essa
regra: ele NÃO é um diagnóstico agronômico da lavoura.

`soil_moisture_pct` também não é umidade de solo medida. No diagrama do Wokwi a
grandeza vem de um POTENCIÔMETRO fazendo as vezes do sensor capacitivo, e o
firmware converte a posição do cursor em 0-100% por regra de três, sem
calibração nenhuma.

O broker é público e o tópico é assinado com wildcard, então qualquer pessoa
pode publicar nele. Todo payload passa por validação de schema e de sanidade, e
o dispositivo de origem pode ser restringido por allowlist na linha de comando.
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import joblib
import pandas as pd
import paho.mqtt.client as mqtt

from contrato import (
    CAMINHO_ARTEFATO,
    CAMINHO_BANCO,
    CULTURA_ESPERADA,
    FEATURES,
    VERSAO_CONTRATO,
)

# Host, porta e prefixo de tópico espelham as constantes de ../firmware/sketch.ino.
# O firmware publica em "<prefixo>/<deviceId>/telemetry"; o "+" casa com um único
# nível, o que é mais estreito que "#" e não captura subtópicos inesperados.
MQTT_HOST = "broker.hivemq.com"
MQTT_PORT = 1883
PREFIXO_TOPICO = "fiap/farmtech/rice"
TOPICO_ASSINATURA = f"{PREFIXO_TOPICO}/+/telemetry"
MQTT_KEEPALIVE_S = 30

# O paho reconecta sozinho com espera exponencial entre estes limites; um laço
# de reconexão próprio ficaria mais apertado e martelaria o broker público.
RECONEXAO_MIN_S = 1
RECONEXAO_MAX_S = 30

# Filtro de sanidade contra payload corrompido ou publicado por terceiro no
# broker público. Não é faixa agronômica: descarta apenas o que não pode ser
# leitura de sensor. As duas porcentagens são 0-100 por construção do firmware
# (readSoilMoisturePercent em ../firmware/sketch.ino); a banda de temperatura é
# deliberadamente larga para não descartar leitura legítima do simulador.
PERCENTUAL_MIN = 0.0
PERCENTUAL_MAX = 100.0
TEMPERATURA_SANIDADE_MIN_C = -50.0
TEMPERATURA_SANIDADE_MAX_C = 100.0

# O firmware devolve 0 enquanto o SNTP não respondeu (syncedEpoch em
# ../firmware/sketch.ino). Gravar esse 0 como instante seria inventar um horário.
TS_NAO_SINCRONIZADO = 0

# O SQLite guarda inteiro de 64 bits; epoch fora desta faixa não é timestamp e
# faria o INSERT levantar OverflowError, derrubando o assinante.
TS_MAXIMO = 2**63 - 1

ROTULO_SAUDAVEL = "Saudável"
ROTULO_NAO_SAUDAVEL = "Não saudável"
CLASSE_SAUDAVEL = 1

ESQUEMA_BANCO = """
CREATE TABLE IF NOT EXISTS leituras (
    recebido_em        TEXT    NOT NULL,
    device             TEXT    NOT NULL,
    ts_dispositivo     INTEGER NOT NULL,
    ts_sincronizado    INTEGER NOT NULL,
    temperature_c      REAL    NOT NULL,
    air_humidity_pct   REAL    NOT NULL,
    soil_moisture_pct  REAL    NOT NULL,
    veredito           TEXT    NOT NULL
)
"""

# Deduplicação só vale onde o timestamp distingue amostras. Enquanto o SNTP não
# responde o ts é 0 para todas, e com os sensores parados no simulador as
# leituras saem idênticas: uma chave única cega descartaria amostra legítima
# como se fosse reentrega. O índice parcial protege contra duplicata real e
# deixa passar a janela não sincronizada.
INDICE_DEDUPLICACAO = """
CREATE UNIQUE INDEX IF NOT EXISTS ix_leitura_unica
    ON leituras (device, ts_dispositivo, temperature_c, air_humidity_pct, soil_moisture_pct)
    WHERE ts_dispositivo != 0
"""

INSERCAO = """
INSERT OR IGNORE INTO leituras
    (recebido_em, device, ts_dispositivo, ts_sincronizado,
     temperature_c, air_humidity_pct, soil_moisture_pct, veredito)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def device_do_topico(topico: str) -> str | None:
    """Extrai o device do nível curinga de '<prefixo>/<device>/telemetry'."""
    partes = topico.split("/")
    esperado = PREFIXO_TOPICO.split("/")
    if len(partes) != len(esperado) + 2 or partes[: len(esperado)] != esperado:
        return None
    return partes[len(esperado)]


def interpretar_payload(bruto: bytes, topico: str) -> dict[str, Any] | None:
    """Converte o payload em dicionário validado, ou None se não for utilizável."""
    try:
        # json.loads sobre bytes detecta o encoding: payload não-UTF levanta
        # UnicodeDecodeError, que NÃO é subclasse de JSONDecodeError.
        conteudo = json.loads(bruto)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(conteudo, dict):
        return None
    if any(campo not in conteudo for campo in (*FEATURES, "device", "crop", "ts")):
        return None
    if conteudo["crop"] != CULTURA_ESPERADA:
        return None

    try:
        leituras = {campo: float(conteudo[campo]) for campo in FEATURES}
        ts = int(conteudo["ts"])
    except (TypeError, ValueError):
        return None

    if not dentro_da_sanidade(leituras) or not TS_NAO_SINCRONIZADO <= ts <= TS_MAXIMO:
        return None

    # O nível curinga do tópico é a identidade que o firmware derivou do MAC;
    # divergir do corpo indica publicação forjada por terceiro.
    device = str(conteudo["device"])
    if device_do_topico(topico) != device:
        return None

    return {"device": device, "ts": ts, **leituras}


def dentro_da_sanidade(leituras: dict[str, float]) -> bool:
    """Rejeita valor que não pode ter saído de um dos dois sensores simulados."""
    percentuais_validos = all(
        PERCENTUAL_MIN <= leituras[campo] <= PERCENTUAL_MAX
        for campo in ("air_humidity_pct", "soil_moisture_pct")
    )
    temperatura_valida = (
        TEMPERATURA_SANIDADE_MIN_C <= leituras["temperature_c"] <= TEMPERATURA_SANIDADE_MAX_C
    )
    return percentuais_validos and temperatura_valida


def carregar_artefato(caminho) -> dict[str, Any]:
    """Carrega o modelo e recusa artefato cujo contrato este assinante não sabe ler."""
    artefato = joblib.load(caminho)
    versao = artefato.get("versao_contrato")
    if versao != VERSAO_CONTRATO:
        raise ValueError(
            f"Artefato em {caminho} tem contrato versão {versao}; "
            f"este assinante espera {VERSAO_CONTRATO}. Rode treinar_classificador.py."
        )
    return artefato


def classificar(artefato: dict[str, Any], leitura: dict[str, Any]) -> str:
    """Monta o vetor na ordem gravada no artefato — nunca numa ordem repetida aqui."""
    features = artefato["features"]
    # DataFrame com os nomes, e não lista posicional: assim o sklearn compara os
    # nomes e levanta erro se o contrato divergir, em vez de classificar errado.
    entrada = pd.DataFrame([[leitura[nome] for nome in features]], columns=features)
    predito = artefato["modelo"].predict(entrada)
    return ROTULO_SAUDAVEL if int(predito[0]) == CLASSE_SAUDAVEL else ROTULO_NAO_SAUDAVEL


def formatar_linha(leitura: dict[str, Any], veredito: str, recebido_em: str) -> str:
    """Uma linha por amostra, larga o bastante para ser lida em vídeo."""
    return (
        f"[{recebido_em}] {leitura['device']}  "
        f"T={leitura['temperature_c']:5.1f}C  "
        f"UR={leitura['air_humidity_pct']:5.1f}%  "
        f"Solo={leitura['soil_moisture_pct']:5.1f}%  ->  {veredito}"
    )


def abrir_banco(caminho) -> sqlite3.Connection:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho)
    conexao.execute(ESQUEMA_BANCO)
    conexao.execute(INDICE_DEDUPLICACAO)
    conexao.commit()
    return conexao


def persistir(
    conexao: sqlite3.Connection, leitura: dict[str, Any], veredito: str, recebido_em: str
) -> None:
    conexao.execute(
        INSERCAO,
        (
            recebido_em,
            leitura["device"],
            leitura["ts"],
            int(leitura["ts"] != TS_NAO_SINCRONIZADO),
            leitura["temperature_c"],
            leitura["air_humidity_pct"],
            leitura["soil_moisture_pct"],
            veredito,
        ),
    )
    conexao.commit()


def ao_conectar(
    cliente: mqtt.Client, dados: Any, flags: Any, codigo: Any, propriedades: Any
) -> None:
    """Reassina a cada conexão: sessão limpa perde a assinatura no reconnect."""
    if codigo.is_failure:
        print(f"Falha ao conectar ao broker: {codigo}")
        return
    cliente.subscribe(TOPICO_ASSINATURA)
    print(f"Conectado a {MQTT_HOST}. Assinando {TOPICO_ASSINATURA}")


def ao_receber(cliente: mqtt.Client, dados: dict[str, Any], mensagem: mqtt.MQTTMessage) -> None:
    leitura = interpretar_payload(mensagem.payload, mensagem.topic)
    if leitura is None:
        print(f"Payload descartado em {mensagem.topic}: {mensagem.payload!r}")
        return

    permitidos = dados["devices_permitidos"]
    if permitidos and leitura["device"] not in permitidos:
        print(f"Device fora da allowlist, ignorado: {leitura['device']}")
        return

    veredito = classificar(dados["artefato"], leitura)
    recebido_em = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    persistir(dados["conexao"], leitura, veredito, recebido_em)
    print(formatar_linha(leitura, veredito, recebido_em))


def ler_argumentos() -> argparse.Namespace:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument(
        "--device",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "aceita apenas este dispositivo; repita para vários. O ID sai da "
            "primeira linha do Serial Monitor do Wokwi. Sem a opção, aceita "
            "qualquer publicação do tópico — inclusive de terceiros."
        ),
    )
    return analisador.parse_args()


def main() -> None:
    argumentos = ler_argumentos()
    artefato = carregar_artefato(CAMINHO_ARTEFATO)
    conexao = abrir_banco(CAMINHO_BANCO)

    print(f"Modelo carregado de {CAMINHO_ARTEFATO}")
    print(f"Contrato de features (versão {VERSAO_CONTRATO}): {artefato['features']}")
    print(f"Banco: {CAMINHO_BANCO}")
    if argumentos.device:
        print(f"Allowlist de dispositivos: {argumentos.device}")
    else:
        print("Sem allowlist: o broker é público e qualquer publicação será aceita.")
    print("Veredito reproduz a regra de limiares do treino, não um diagnóstico agronômico.")
    print("Solo vem de um potenciômetro simulando o sensor capacitivo, sem calibração.\n")

    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    cliente.user_data_set(
        {
            "artefato": artefato,
            "conexao": conexao,
            "devices_permitidos": set(argumentos.device),
        }
    )
    cliente.on_connect = ao_conectar
    cliente.on_message = ao_receber
    cliente.reconnect_delay_set(min_delay=RECONEXAO_MIN_S, max_delay=RECONEXAO_MAX_S)
    cliente.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE_S)

    try:
        cliente.loop_forever()
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuário.")
    finally:
        cliente.disconnect()
        conexao.close()


if __name__ == "__main__":
    main()
