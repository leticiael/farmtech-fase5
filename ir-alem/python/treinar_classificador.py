"""Treina o classificador binário de saúde do arroz e serializa o artefato.

LIMITAÇÃO CENTRAL — O RÓTULO É CIRCULAR
=======================================
A telemetria publicada pelo ESP32 não traz rótulo de saúde da planta. O rótulo
usado aqui é sintetizado por `rotular`, uma regra de limiares escrita neste
próprio arquivo. Treinar um modelo nesse rótulo faz o modelo reaprender a regra.

Consequência, que vale para todo número impresso por este script: a acurácia
NÃO mede saúde da plantação. Ela mede o quanto cada modelo aproxima uma função
que já é conhecida, porque foi escrita aqui. Nenhuma métrica deste arquivo é
evidência de que o sistema detecta estresse em arroz.

É o mesmo erro que a Entrega 1 evita ao comparar todo R² contra o piso de
média-por-cultura, com um agravante: lá o piso era alto por uma propriedade do
dado, aqui o teto é 1,0 por construção e nenhum dado do mundo pode contrariar
o rótulo.
"""

from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

from contrato import CAMINHO_ARTEFATO, FEATURES, VERSAO_CONTRATO

RANDOM_STATE = 42
ALVO = "saudavel"

# --------------------------- Faixas agronômicas ---------------------------
# Acima de ~33 °C a esterilidade de espiguetas do arroz cresce de forma
# acentuada. Fonte que traz o limiar:
#   "Temperature thresholds for spikelet sterility and associated warming
#   impacts for sub-tropical rice", Agricultural and Forest Meteorology (2016),
#   que reporta 0,26 ponto percentual de esterilidade por grau-hora acima de
#   33 °C. https://www.sciencedirect.com/science/article/abs/pii/S0168192316301587
# Corrobora, com ressalva de genótipo:
#   Jagadish et al. (2007), Journal of Experimental Botany 58(7):1627-1635,
#   https://academic.oup.com/jxb/article/58/7/1627/512931
#   onde exposição de até 1 h a >= 33,7 °C na antese já causa esterilidade. Note
#   que ali o limiar de 33 °C é específico do genótipo Azucena: no mesmo estudo
#   o IR64 perde fertilidade acima de 29,6 °C sem limiar definido. Ou seja, 33 °C
#   não é o valor mais restritivo da literatura, e sim o que a fonte sustenta.
TEMPERATURA_MAXIMA_C = 33.0

# NÃO EXISTE LIMITE INFERIOR DE TEMPERATURA NESTA REGRA.
# Arroz sofre dano por frio, mas a faixa "ótima 25-35 °C", comum na literatura
# secundária, NÃO está em nenhuma das duas fontes acima — ela rastreia a
# Yoshida (1981), "Fundamentals of Rice Crop Science", IRRI, cujo texto integral
# eu não consegui acessar para conferir o valor. Citar as fontes que eu tenho
# para sustentar um número que elas não contêm seria fonte decorativa, então o
# limite inferior foi removido da regra em vez de ficar sem lastro. O frio está
# fora do escopo do classificador, declaradamente.

# ATENÇÃO — ESTE NÚMERO NÃO É UMA FAIXA AGRONÔMICA.
# A referência de manejo de água para arroz é o "Safe AWD" do IRRI: reirrigar
# quando a lâmina d'água baixa a 15 cm abaixo da superfície do solo.
#   IRRI Rice Knowledge Bank, "Saving Water with Alternate Wetting Drying".
#   http://www.knowledgebank.irri.org/training/fact-sheets/water-management/saving-water-alternate-wetting-drying-awd
# Esse limiar é expresso em PROFUNDIDADE DE LÂMINA D'ÁGUA. Converter 15 cm em
# porcentagem de leitura de um sensor capacitivo exige calibração por tipo de
# solo, que este projeto não tem — e o sensor aqui é um potenciômetro fazendo
# as vezes do capacitivo. Transportar o número de uma grandeza para a outra
# seria fabricar faixa agronômica com aparência de fonte.
# O valor abaixo é, portanto, um PONTO DE OPERAÇÃO da simulação, escolhido por
# mim, sem lastro agronômico. O comportamento do classificador neste eixo não
# sustenta nenhuma afirmação sobre arroz.
LIMIAR_OPERACIONAL_SOLO_PCT = 40.0

# Não foi encontrada fonte com limiar de umidade relativa do ar para arroz que
# separasse planta saudável de planta sob estresse. Por isso `air_humidity_pct`
# entra como feature do modelo — é o que o sensor publica — mas NÃO participa da
# regra de rotulagem. Consequência declarada: é uma variável sem efeito nenhum
# sobre o rótulo, por construção, e a importância medida dela deve ficar em
# torno de zero. Se não ficar, o problema é do procedimento, não da planta.

# ----------------------- Janela de amostragem sintética -------------------
# Não há dataset rotulado de saúde de arroz para este hardware, então as
# amostras são geradas. A janela abaixo é ESCOLHA MINHA: ela atravessa os
# limiares com folga para que as duas classes existam. Não é a distribuição de
# campo, e o balanceamento entre classes é consequência dela, não do mundo.
TEMPERATURA_AMOSTRAL_MIN_C = 18.0
TEMPERATURA_AMOSTRAL_MAX_C = 40.0

# O firmware converte a leitura do ADC em 0-100% (ver readSoilMoisturePercent
# em ../firmware/sketch.ino); a umidade do ar do DHT22 é reportada na mesma
# escala percentual.
PERCENTUAL_MIN = 0.0
PERCENTUAL_MAX = 100.0

N_AMOSTRAS = 4000
FRACAO_TESTE = 0.25

# A regra é uma conjunção de dois limiares, ambos paralelos aos eixos. Dois
# cortes bastam; a folga permite observar se a árvore acrescenta corte que a
# regra não tem — e `medir_fidelidade_a_regra` mede se ela acrescentou.
PROFUNDIDADE_MAXIMA_ARVORE = 4
N_REPETICOES_IMPORTANCIA = 20

# Amostras novas, fora do treino e do teste, só para comparar o modelo com a
# regra. Volume alto porque a divergência esperada mora em faixas estreitas
# em torno dos limiares.
N_AMOSTRAS_FIDELIDADE = 2_000_000

NOME_PISO = "Piso (classe majoritária)"
NOME_LOGISTICA = "Regressão Logística"
NOME_ARVORE = "Árvore de Decisão"
CASAS_DECIMAIS = 4


def rotular(temperatura_c: pd.Series, umidade_solo_pct: pd.Series) -> pd.Series:
    """Rótulo sintético: 1 = saudável. Esta é a regra que o modelo vai reaprender."""
    temperatura_adequada = temperatura_c <= TEMPERATURA_MAXIMA_C
    solo_adequado = umidade_solo_pct >= LIMIAR_OPERACIONAL_SOLO_PCT
    return (temperatura_adequada & solo_adequado).astype(int)


def gerar_amostras(n_amostras: int, gerador: np.random.Generator) -> pd.DataFrame:
    """Amostras independentes e uniformes dentro da janela declarada acima."""
    amostras = pd.DataFrame(
        {
            "temperature_c": gerador.uniform(
                TEMPERATURA_AMOSTRAL_MIN_C, TEMPERATURA_AMOSTRAL_MAX_C, n_amostras
            ),
            "air_humidity_pct": gerador.uniform(PERCENTUAL_MIN, PERCENTUAL_MAX, n_amostras),
            "soil_moisture_pct": gerador.uniform(PERCENTUAL_MIN, PERCENTUAL_MAX, n_amostras),
        }
    )
    amostras[ALVO] = rotular(amostras["temperature_c"], amostras["soil_moisture_pct"])
    return amostras


def montar_modelos() -> dict[str, Any]:
    """Piso de comparação, modelo linear e modelo de partição, nesta ordem de leitura.

    A escolha não é acidental e o que ela mede é a geometria da regra de
    rotulagem, não a plantação:

    - `DummyClassifier` é o piso. Acurácia abaixo dele é pior que chutar a
      classe majoritária, e nenhuma acurácia significa nada sem ele ao lado.
    - `LogisticRegression` traça UMA fronteira linear. A regra é um "E" de
      limiares, que uma reta não representa: a logística fica abaixo do teto
      por limitação do espaço de hipóteses dela.
    - `DecisionTreeClassifier` particiona por cortes paralelos aos eixos, que é
      exatamente a forma da regra.

    A diferença entre os dois últimos descreve a FORMA DA MINHA REGRA — uma
    conjunção de limiares —, e não uma propriedade do arroz. É a árvore que vai
    ao artefato, por ser a mais fiel à regra declarada.
    """
    return {
        NOME_PISO: DummyClassifier(strategy="most_frequent", random_state=RANDOM_STATE),
        NOME_LOGISTICA: Pipeline(
            [
                ("escala", StandardScaler()),
                ("modelo", LogisticRegression(random_state=RANDOM_STATE)),
            ]
        ),
        NOME_ARVORE: DecisionTreeClassifier(
            max_depth=PROFUNDIDADE_MAXIMA_ARVORE, random_state=RANDOM_STATE
        ),
    }


def avaliar_modelos(
    modelos: dict[str, Any],
    treino: pd.DataFrame,
    teste: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Ajusta cada modelo e devolve as métricas numa tabela única, mais os ajustados."""
    x_treino, y_treino = treino[list(FEATURES)], treino[ALVO]
    x_teste, y_teste = teste[list(FEATURES)], teste[ALVO]

    ajustados: dict[str, Any] = {}
    linhas = []
    for nome, modelo in modelos.items():
        ajustado = modelo.fit(x_treino, y_treino)
        predito = ajustado.predict(x_teste)
        ajustados[nome] = ajustado
        linhas.append(
            {
                "modelo": nome,
                "acurácia": accuracy_score(y_teste, predito),
                # zero_division=0 porque o piso nunca prevê a classe positiva:
                # a precisão dele é indefinida, e 0 é a leitura honesta disso.
                "precisão": precision_score(y_teste, predito, zero_division=0),
                "recall": recall_score(y_teste, predito, zero_division=0),
            }
        )
    return pd.DataFrame(linhas).set_index("modelo"), ajustados


def medir_importancia(modelo: Any, teste: pd.DataFrame) -> pd.Series:
    """Queda de acurácia ao embaralhar cada feature, para conferir a umidade do ar."""
    resultado = permutation_importance(
        modelo,
        teste[list(FEATURES)],
        teste[ALVO],
        n_repeats=N_REPETICOES_IMPORTANCIA,
        random_state=RANDOM_STATE,
    )
    return pd.Series(resultado.importances_mean, index=FEATURES).sort_values(ascending=False)


def medir_fidelidade_a_regra(modelo: Any, gerador: np.random.Generator) -> float:
    """Fração de amostras NOVAS em que o modelo concorda com a regra de rotulagem.

    O conjunto de teste é finito e pode dar 1,0 sem que o modelo seja idêntico à
    regra: a árvore aprende as fronteiras a partir dos pontos que viu e costuma
    acrescentar cortes estreitos em torno dos limiares. Este número mede isso em
    vez de deixar a matriz de confusão perfeita sugerir identidade.
    """
    amostras = gerar_amostras(N_AMOSTRAS_FIDELIDADE, gerador)
    predito = modelo.predict(amostras[list(FEATURES)])
    return float((predito == amostras[ALVO]).mean())


def formatar_matriz_confusao(matriz: np.ndarray) -> str:
    return pd.DataFrame(
        matriz,
        index=["real: não saudável", "real: saudável"],
        columns=["predito: não saudável", "predito: saudável"],
    ).to_string()


def salvar_artefato(modelo: Any, caminho) -> None:
    """Serializa o estimador junto do contrato de features que o assinante deve usar."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "versao_contrato": VERSAO_CONTRATO,
            "features": list(FEATURES),
            "modelo": modelo,
            "regra_rotulagem": {
                "temperatura_max_c": TEMPERATURA_MAXIMA_C,
                "solo_min_pct": LIMIAR_OPERACIONAL_SOLO_PCT,
            },
        },
        caminho,
    )


def main() -> None:
    gerador = np.random.default_rng(RANDOM_STATE)
    amostras = gerar_amostras(N_AMOSTRAS, gerador)

    # Split estratificado simples: as amostras são independentes por construção,
    # sem estrutura temporal. Telemetria capturada do broker NÃO é assim — o
    # firmware publica a cada 5 s e amostras vizinhas são quase idênticas, o que
    # exigiria split temporal para não inflar a métrica.
    treino, teste = train_test_split(
        amostras,
        test_size=FRACAO_TESTE,
        random_state=RANDOM_STATE,
        stratify=amostras[ALVO],
    )

    comparacao, ajustados = avaliar_modelos(montar_modelos(), treino, teste)
    escolhido = ajustados[NOME_ARVORE]

    print(f"Amostras: {len(amostras)} | treino {len(treino)} | teste {len(teste)}")
    print(
        f"Proporção da classe 'saudável': {amostras[ALVO].mean():.3f} "
        f"— consequência da janela de amostragem escolhida, não do campo.\n"
    )
    print(comparacao.round(CASAS_DECIMAIS).to_string())

    predito = escolhido.predict(teste[list(FEATURES)])
    print(f"\nMatriz de confusão — {NOME_ARVORE} (linhas: real, colunas: predito)")
    print(formatar_matriz_confusao(confusion_matrix(teste[ALVO], predito)))

    print("\nImportância por permutação (queda de acurácia ao embaralhar):")
    print(medir_importancia(escolhido, teste).round(CASAS_DECIMAIS).to_string())

    fidelidade = medir_fidelidade_a_regra(escolhido, gerador)
    print(
        f"\nFidelidade à regra em {N_AMOSTRAS_FIDELIDADE:,} amostras novas: "
        f"{fidelidade:.6f}\n"
        "Abaixo de 1,0 significa que a árvore acrescentou corte que a regra não "
        "tem — o 1,0 da matriz acima é do conjunto de teste, não identidade com a regra."
    )

    salvar_artefato(escolhido, CAMINHO_ARTEFATO)
    print(f"\nArtefato salvo em {CAMINHO_ARTEFATO}")
    print(
        "\nLeitura obrigatória destes números: o rótulo foi gerado pela regra de\n"
        "limiares deste arquivo, então o modelo reaprende a regra. A acurácia alta\n"
        "não é evidência de que o sistema detecta estresse em arroz."
    )


if __name__ == "__main__":
    main()
