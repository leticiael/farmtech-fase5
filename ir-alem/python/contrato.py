"""Contrato compartilhado entre o treino e o assinante.

Os nomes e a ordem de `FEATURES` são a única fonte de verdade do projeto: o
treino grava esta lista dentro do artefato e o assinante monta o vetor a partir
dela. Vetor fora de ordem não levanta exceção quando passa como lista
posicional — classifica errado em silêncio —, então a lista não pode existir
em duas cópias que possam divergir.

Os nomes vêm do `snprintf` de `buildPayload` em ../firmware/sketch.ino.
"""

from pathlib import Path

FEATURES = ("temperature_c", "air_humidity_pct", "soil_moisture_pct")
CAMPOS_METADADOS = ("device", "crop", "ts")

# O firmware publica sempre com esta cultura; o classificador foi treinado com
# faixas de arroz e não se aplica a outra.
CULTURA_ESPERADA = "rice"

# Versão do formato do artefato. O assinante recusa artefato de versão diferente
# em vez de carregar um contrato que ele não sabe ler.
VERSAO_CONTRATO = 2

_RAIZ = Path(__file__).resolve().parent.parent
CAMINHO_ARTEFATO = _RAIZ / "modelo" / "classificador_saude_arroz.joblib"
CAMINHO_BANCO = _RAIZ / "dados" / "telemetria.db"
