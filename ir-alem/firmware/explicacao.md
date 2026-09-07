# Firmware — nó ESP32 de telemetria (Wokwi + MQTT)

Documentação técnica de **como montar e rodar o nó no simulador**. A
arquitetura, a justificativa das escolhas e as limitações do trabalho estão na
seção "Ir Além" do `README.md` da raiz; aqui ficam só os detalhes de operação
do Wokwi, que não cabem num README de entrega.

**Projeto pronto e rodando:** https://wokwi.com/projects/474459640677178369
(privacidade *Unlisted* — só abre com o link)

## Arquivos
| Arquivo | Onde vai |
|---|---|
| `diagram.json` | aba **diagram.json** |
| `sketch.ino` | aba **sketch.ino** |
| `libraries.txt` | gerado pela aba **Library Manager** |

## Pinagem
| Componente | Pino do componente | Pino do ESP32 |
|---|---|---|
| Potenciômetro | VCC | 3V3 |
| Potenciômetro | SIG | **GPIO34** |
| Potenciômetro | GND | GND (em série com o DHT22) |
| DHT22 | VCC | 3V3 (em série com o potenciômetro) |
| DHT22 | SDA (data) | **GPIO27** |
| DHT22 | GND | GND.1 |

Três decisões de pinagem que valem defender na entrega:

- **GPIO34 para o solo.** O ADC2 fica inutilizável enquanto o rádio Wi-Fi está
  ligado. GPIO34 é ADC1 e continua lendo com a conexão ativa. Usar GPIO4/GPIO2
  aqui faria `analogRead()` devolver lixo assim que o Wi-Fi subisse.
- **GPIO27 para o DHT22.** GPIO15 é *strapping pin* (MTDO) e altera o log de
  boot da placa. GPIO27 é um GPIO comum, sem função na inicialização.
- **Alimentação encadeada.** VCC e GND dos dois sensores são ligados em cadeia
  antes de irem à placa. Isso evita dois fios chegando no mesmo pino 3V3 (que
  ficariam sobrepostos no desenho) e deixa cada trilha do diagrama legível.
  O encadeamento é de **fiação**: eletricamente os dois sensores continuam em
  **paralelo** sobre 3V3 e GND, cada um recebendo os mesmos 3,3 V.

### Cuidado com os nomes dos pinos no `diagram.json`
No `board-esp32-devkit-c-v4` do Wokwi os GPIOs se chamam **`27`, `34`, `15`** —
sem o prefixo `D`. E a serial é **`TX`/`RX`**, não `TX0`/`RX0`. Escrever
`esp:D27` ou `esp:TX0` **não dá erro**: o Wokwi só ignora a conexão em silêncio,
o sensor fica solto e o Serial Monitor nem aparece. Cuidado também com `D0`–`D3`,
`CMD` e `CLK`: são os pinos da flash, não os GPIOs 0–3.

## Bibliotecas (Library Manager)
Nomes exatos a digitar na busca:

1. `DHT sensor library` — Adafruit
2. `Adafruit Unified Sensor` — Adafruit (dependência da anterior)
3. `PubSubClient` — Nick O'Leary

`WiFi.h` e `time.h` já vêm no core ESP32.

## Passo a passo no site

1. Abra `wokwi.com/projects/new/esp32`.
2. Aba **diagram.json**: Ctrl+A e cole o conteúdo de `diagram.json`. O canvas
   monta ESP32 + DHT22 + potenciômetro já ligados.
3. Aba **sketch.ino**: Ctrl+A e cole o conteúdo de `sketch.ino`.
4. Aba **Library Manager** → `+` → adicione as três bibliotecas, uma por vez.
5. **Play (▶)**. A compilação leva ~30 s na primeira vez.
6. O **Serial Monitor** abre embaixo do diagrama e imprime:
   - `Topico de telemetria: fiap/farmtech/rice/XXXXXXXXXXXX/telemetry`
   - depois de ~5 s, uma linha JSON a cada 5 s.
7. **Anote o tópico** da primeira linha — o sufixo vem do MAC do chip simulado
   e é único do seu projeto.

### Como confirmar que o MQTT está publicando de verdade
O Serial Monitor só prova que o `publish()` foi aceito localmente. Para provar
que o dado chegou ao broker, assine o tópico por fora:

**Opção A — navegador**
- `hivemq.com/demos/websocket-client/`
- Host `broker.hivemq.com`, Port `8000` (websocket, não 1883) → **Connect**
- *Add New Topic Subscription* → `fiap/farmtech/rice/#` → **Subscribe**
- As mensagens aparecem a cada 5 s.

**Opção B — terminal**
```
mosquitto_sub -h broker.hivemq.com -p 1883 -t 'fiap/farmtech/rice/#' -v
```

### Detalhe que engana
O Chrome congela a simulação do Wokwi quando a aba fica em segundo plano (o
indicador de performance vai a 0%). Se as mensagens pararem, é isso — deixe a
aba visível.

### Teste de sanidade
Com o assinante aberto, gire o potenciômetro no canvas e clique no DHT22 para
mudar temperatura/umidade. Os valores publicados mudam no ciclo seguinte —
prova ponta a ponta: sensor → firmware → broker.

## Payload publicado
```json
{"device":"100100C40A24","crop":"rice","temperature_c":26.5,
 "air_humidity_pct":72.0,"soil_moisture_pct":50.1,"ts":1788739866}
```
`ts` é epoch UTC vindo do NTP; vale `0` nos primeiros ~30 s, antes do primeiro
pacote SNTP responder — o assinante marca essas linhas como não sincronizadas
em vez de tratar o zero como instante válido.

**O vetor de features do classificador são os três campos de sensor** —
`temperature_c`, `air_humidity_pct` e `soil_moisture_pct`, nessa ordem.
`device`, `crop` e `ts` são metadados e **não** entram no modelo. A ordem
canônica é a gravada dentro do artefato `.joblib` pelo treino; o assinante
monta o vetor a partir dela, nunca de uma ordem repetida à mão.
