/*
 * FarmTech Solutions - FIAP Fase 5 / "Ir Alem"
 * Nó de telemetria para cultura de arroz: publica o vetor de features
 * (temperatura, umidade do ar, umidade de solo) que alimenta o
 * classificador de saude da plantacao rodando fora do dispositivo.
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <time.h>

// ----------------------------- Rede ---------------------------------
constexpr char     WIFI_SSID[]       = "Wokwi-GUEST";
constexpr char     WIFI_PASSWORD[]   = "";
constexpr uint32_t WIFI_RETRY_MS     = 5000;

// ----------------------------- MQTT ---------------------------------
constexpr char     MQTT_HOST[]       = "broker.hivemq.com";
constexpr uint16_t MQTT_PORT         = 1883;
constexpr char     TOPIC_PREFIX[]    = "fiap/farmtech/rice";
constexpr uint16_t MQTT_KEEPALIVE_S  = 30;
constexpr uint32_t MQTT_RETRY_MS     = 5000;

// --------------------------- Sensores -------------------------------
// GPIO15 e strapping pin (MTDO) e mexe no log de boot; o dado do
// DHT22 fica num GPIO comum, sem funcao especial na inicializacao.
constexpr uint8_t  DHT_PIN           = 27;

// ADC2 fica indisponivel enquanto o radio Wi-Fi esta ligado;
// GPIO34 pertence ao ADC1 e continua lendo com a conexao ativa.
constexpr uint8_t  SOIL_PIN          = 34;

// O DHT22 conclui uma conversao a cada ~2 s: amostrar mais rapido
// devolve o valor anterior e infla o dataset com duplicatas.
constexpr uint32_t SAMPLE_PERIOD_MS  = 5000;

// O cursor do potenciometro oscila ~1 LSB no SAR; a media derruba
// esse jitter sem introduzir atraso perceptivel.
constexpr uint8_t  SOIL_OVERSAMPLE   = 8;
constexpr uint16_t ADC_FULL_SCALE    = 4095;
constexpr uint8_t  ADC_BITS          = 12;

// ------------------------------ Tempo -------------------------------
constexpr char     NTP_SERVER[]      = "pool.ntp.org";

// Antes do primeiro pacote SNTP o relogio marca 1970; o limiar separa
// timestamp real de contador de boot.
constexpr time_t   EPOCH_SANE_MIN    = 1700000000;

constexpr size_t   PAYLOAD_CAPACITY  = 200;

DHT dht(DHT_PIN, DHT22);
WiFiClient net;
PubSubClient mqtt(net);

char deviceId[13];
char telemetryTopic[64];

// O broker publico e compartilhado: derivar identidade do eFuse evita
// que duas equipes colidam no mesmo client-id e no mesmo topico.
void buildIdentity() {
  const uint64_t mac = ESP.getEfuseMac();
  snprintf(deviceId, sizeof(deviceId), "%04X%08X",
           static_cast<uint16_t>(mac >> 32), static_cast<uint32_t>(mac));
  snprintf(telemetryTopic, sizeof(telemetryTopic), "%s/%s/telemetry",
           TOPIC_PREFIX, deviceId);
}

void keepWifiAlive() {
  if (WiFi.status() == WL_CONNECTED) return;

  static uint32_t lastAttempt = 0;
  const uint32_t now = millis();
  if (now - lastAttempt < WIFI_RETRY_MS) return;
  lastAttempt = now;

  // Sem o disconnect a pilha pode ficar presa em WL_CONNECT_FAILED.
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

bool keepBrokerAlive() {
  if (mqtt.connected()) return true;

  static uint32_t lastAttempt = 0;
  const uint32_t now = millis();
  if (lastAttempt != 0 && now - lastAttempt < MQTT_RETRY_MS) return false;
  lastAttempt = now;

  return mqtt.connect(deviceId);
}

float readSoilMoisturePercent() {
  uint32_t accumulator = 0;
  for (uint8_t i = 0; i < SOIL_OVERSAMPLE; ++i) {
    accumulator += analogRead(SOIL_PIN);
  }
  const float raw = static_cast<float>(accumulator) / SOIL_OVERSAMPLE;
  return (raw / ADC_FULL_SCALE) * 100.0f;
}

time_t syncedEpoch() {
  const time_t now = time(nullptr);
  return now < EPOCH_SANE_MIN ? 0 : now;
}

// configTime() inicializa o SNTP do lwIP, que precisa da interface de rede ja
// existente: chamado no setup(), logo apos WiFi.begin(), ele arma o cliente SNTP
// sobre uma pilha que ainda esta subindo e a placa aborta em assert quando a
// associacao completa. Adiar para depois de WL_CONNECTED elimina a corrida.
// UTC puro: as amostras sao comparadas entre maquinas de fusos diferentes.
void startClockOnce() {
  static bool started = false;
  if (started) return;
  started = true;
  configTime(0, 0, NTP_SERVER);
}

void buildPayload(char *out, size_t capacity,
                  float temperatureC, float airHumidity, float soilMoisture,
                  time_t epoch) {
  snprintf(out, capacity,
           "{\"device\":\"%s\",\"crop\":\"rice\","
           "\"temperature_c\":%.1f,\"air_humidity_pct\":%.1f,"
           "\"soil_moisture_pct\":%.1f,\"ts\":%lu}",
           deviceId, temperatureC, airHumidity, soilMoisture,
           static_cast<unsigned long>(epoch));
}

void publishTelemetry() {
  const float temperatureC = dht.readTemperature();
  const float airHumidity  = dht.readHumidity();

  // Checksum invalido vira NaN; publicar contaminaria o dataset de treino.
  if (isnan(temperatureC) || isnan(airHumidity)) {
    Serial.println(F("DHT22 sem leitura valida - amostra descartada"));
    return;
  }

  char payload[PAYLOAD_CAPACITY];
  buildPayload(payload, sizeof(payload), temperatureC, airHumidity,
               readSoilMoisturePercent(), syncedEpoch());

  if (!mqtt.publish(telemetryTopic, payload)) {
    Serial.println(F("Broker recusou o publish"));
    return;
  }
  Serial.println(payload);
}

void setup() {
  Serial.begin(115200);

  buildIdentity();
  dht.begin();

  // Fixa 12 bits para que a normalizacao 0-100% independa do default do core.
  analogReadResolution(ADC_BITS);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(MQTT_KEEPALIVE_S);

  Serial.printf("Topico de telemetria: %s\n", telemetryTopic);
}

void loop() {
  keepWifiAlive();
  if (WiFi.status() != WL_CONNECTED) return;

  startClockOnce();
  if (!keepBrokerAlive()) return;

  // Sem esta chamada o broker derruba a sessao ao expirar o keep-alive.
  mqtt.loop();

  static uint32_t lastSample = 0;
  const uint32_t now = millis();
  if (now - lastSample < SAMPLE_PERIOD_MS) return;
  lastSample = now;

  publishTelemetry();
}
