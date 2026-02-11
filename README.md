# 19MoneyScanner

Escaner de Binance USD-M Futures (USDT Perpetual) que calcula Top N por cambio 24h y envia alertas a Telegram. Usa solo data publica (sin claves de trading). No ejecuta ordenes.

## FASE 2 y FASE 3
- EXPLOSION: detecta velas 1m con potencia (retorno + volumen + trades).
- SHORT SETUP: tras una EXPLOSION, espera retest al high y rechazo, con funding OK.

## Deteccion en tiempo real

### Sistema de Auto-Refresh
- **Frecuencia**: El dashboard se actualiza automáticamente cada 10 segundos sin recargar la página.
- **Indicador LIVE**: Un punto verde parpadeante indica que el dashboard está activo y recibiendo datos.
- **Actualización selectiva**: Solo las secciones dinámicas se actualizan (Entradas Recientes y Symbol Status).

### ¿Qué es una "Entrada Reciente"?
Una entrada reciente es un símbolo que:
1. **No estaba** en el Top N hace 60-120 segundos
2. **Ahora sí está** en el Top N actual
3. Indica un movimiento explosivo de precio y volumen reciente

### Cómo usar el Dashboard para detectar explosiones

**Sección 1: 🔥 Entradas Recientes al Top**
- **Fondo amarillo dorado**: Símbolos que acaban de entrar al Top N
- **Icono de fuego**: Indica actividad explosiva reciente
- **Ordenamiento**: Por cambio % descendente
- **Acción recomendada**: Revisar inmediatamente estos símbolos en Binance para confirmar la explosión

**Sección 2: 📊 Top N Performers**
- **Badge "↑ NEW" (verde)**: Símbolo con entrada reciente al Top
- **Badge "STABLE" (gris)**: Símbolo que ya estaba en el Top
- **Fondo verde claro**: Resalta filas con status NEW
- **Hover effect**: Las filas se desplazan sutilmente al pasar el mouse

**Indicadores visuales de trading:**
- **Status NEW**: Posible oportunidad para entrar temprano
- **Status STABLE**: Movimiento ya consolidado, evaluar con cautela
- **Volume alto + NEW**: Mayor probabilidad de explosión real
- **Volume bajo + NEW**: Posible "pump" artificial, precaución

### Flujo de trabajo recomendado
1. Monitorear la sección "Entradas Recientes" cada 1-2 minutos
2. Cuando aparece un símbolo nuevo con 🔥:
   - Verificar el % de cambio (>15% es significativo)
   - Revisar el volumen (>5M USDT indica liquidez real)
   - Abrir el gráfico en Binance Futures
   - Confirmar la explosión con análisis de velas 1m
3. Esperar señal de SHORT SETUP si aplica tu estrategia

## Requisitos
- Windows 11
- Python 3.11+
- VS Code

## Configuracion
1) Crear un bot de Telegram con BotFather y obtener el token.
2) Obtener el chat_id (por ejemplo, enviando un mensaje al bot y consultando el update con un bot auxiliar o usando el metodo getUpdates).
3) Crear un archivo .env en la raiz del proyecto:

```
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_CHAT_ID=tu_chat_id
```

## Instalacion (manual)
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m src.main
```

## Instalacion (run.bat)
```
run.bat
```

## Endpoints del dashboard
- GET /health
- GET /top
- GET /top/recent-entries
- GET /alerts/latest?limit=50
- GET /events/latest?limit=50
- GET /short_setups/latest?limit=50
- GET /symbols/status
- GET /

## Guía visual del Dashboard

### Códigos de color
- **Verde claro**: Símbolo con entrada reciente (oportunidad)
- **Blanco**: Símbolo estable en el Top N
- **Amarillo dorado**: Sección de alertas y entradas recientes
- **Degradado púrpura**: Headers y títulos principales

### Badges de estado
- **↑ NEW (verde brillante)**: Entrada reciente confirmada
- **STABLE (gris)**: Sin cambios significativos en posición
- **OK (gris)**: Funding rate normal
- **RARO (verde destacado)**: Funding rate anormal

### Animaciones y transiciones
- **Pulse en indicador LIVE**: Confirma que el sistema está activo
- **Slide-in en filas NEW**: Animación al detectar entrada reciente
- **Hover effect**: Hover sobre filas las desplaza 4px a la derecha
- **Flicker en 🔥**: Parpadeo sutil para llamar la atención

### Interpretación de datos
- **Volume > 10M USDT**: Alta liquidez, movimiento confiable
- **24h% > 20%**: Movimiento muy fuerte, revisar inmediatamente
- **Funding abs > 0.002**: Posible sobrecalentamiento del mercado

## Ajustes
Editar [config/config.yaml](config/config.yaml) para cambiar filtros, topN, cooldown, refresh y umbrales de EXPLOSION/SHORT SETUP.

### Como funciona SHORT SETUP
1) EXPLOSION detectada con velas 1m.
2) WAIT_RETEST: el precio entra en zona de retest alrededor del event_high.
3) RETEST_SEEN: si no rompe por encima y luego cae bajo el umbral de rechazo, se confirma.
4) Funding OK: abs(fundingRate) <= funding_abs_max.
5) Se emite SHORT SETUP y entra en cooldown.

Controla alert_mode para enviar solo SHORT SETUP o ambos.
