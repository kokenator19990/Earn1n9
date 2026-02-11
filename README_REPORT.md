# Earn1n9 (Binance Futures Alerts) - Reporte Técnico y Guía de Desarrollo

## 1. Descripción General
Earn1n9 es una app de monitoreo y alertas para Binance Futures (USDT Perpetual), orientada a traders cuantitativos y desarrolladores. Permite detectar oportunidades de trading en tiempo real, evaluarlas con un sistema de rating avanzado y visualizarlas en un dashboard profesional.

## 2. Estructura del Proyecto

```
Earn1n9/
├── README.md
├── README_REPORT.md  # (este archivo)
├── requirements.txt
├── run.bat
├── setup.ps1.txt
├── config/
│   └── config.yaml
├── data/
├── logs/
│   └── app.jsonl
├── src/
│   ├── __init__.py
│   ├── binance_rest.py         # Cliente REST Binance (klines, tickers)
│   ├── config.py               # Configuración y modelos
│   ├── dashboard.py            # FastAPI, render HTML, endpoints
│   ├── explosion_monitor.py    # Lógica de explosiones/impulsos
│   ├── main.py                 # Entry point
│   ├── scanner.py              # Pipeline de candidatos, cache, lógica Top N
│   ├── storage.py              # Persistencia SQLite (alertas/eventos)
│   ├── telegram_notifier.py    # Notificaciones Telegram
│   ├── trade_rating.py         # Algoritmo de rating (0-10)
│   ├── utils_logging.py        # Logging estructurado
│   └── __pycache__/
├── tests/
│   ├── test_dashboard_table_render.py
│   ├── test_explosion_logic.py
│   ├── test_recent_candidates_cache.py
│   ├── test_trade_rating.py
│   └── __pycache__/
```

## 3. Principales Componentes y Flujo

### a) Ingesta y Escaneo
- **scanner.py**: Loop principal. Cada 5-10s obtiene tickers, filtra Top N por % cambio y volumen, y actualiza el cache de candidatos recientes (`RecentCandidatesCache`).
- **binance_rest.py**: Cliente asíncrono para obtener datos de Binance (tickers, klines, funding, etc).

### b) Evaluación y Rating
- **trade_rating.py**: Algoritmo de rating (0-10) basado en explosión, volumen, pullback, soporte y funding. Se usa para priorizar oportunidades.
- **explosion_monitor.py**: Detecta impulsos, pullbacks y estados de mercado.

### c) Persistencia y Alertas
- **storage.py**: Guarda alertas y eventos en SQLite. Permite consultar históricos y últimos eventos.
- **telegram_notifier.py**: Envía alertas a Telegram.

### d) Dashboard y API
- **dashboard.py**: FastAPI + HTML. Renderiza la UI principal, expone endpoints para status, setups, y tabla de oportunidades recientes (solo últimos 5 min, Rate >= 7.2). Incluye filtros rápidos, búsqueda y ordenamiento.

### e) Configuración
- **config.py / config.yaml**: Modelos y parámetros globales (umbral de Rate, límites, etc).

### f) Tests
- **tests/**: Unitarios para rating, cache, lógica de tabla y explosiones. Manuales para validación visual.

## 4. Lógica de Oportunidades Recientes
- **RecentCandidatesCache (scanner.py)**: Mantiene símbolos calificados con `first_seen_at`, `last_seen_at`, `age_minutes` y TTL (20 min). Solo muestra en la tabla los que:
  - Tienen `age_minutes <= 5`
  - Rate >= umbral (default 7.2, configurable)
- Si un símbolo deja de calificar, se oculta pero su estado expira tras TTL.

## 5. Columnas y Features de la Tabla
- **Rate**: Score 0-10 (explosión, volumen, pullback, soporte, funding)
- **Age (min)**: Minutos desde que calificó por primera vez
- **Ret_1m, Ret_5m, Ret_15m**: Retornos porcentuales (placeholder, TODO)
- **VolZ_1m, VolZ_5m**: Z-score de volumen (placeholder, TODO)
- **OI_Δ5m**: Cambio de Open Interest (placeholder, TODO)
- **Funding**: Tasa de funding (real si disponible)
- **LS Accounts/Positions**: Ratio long/short (placeholder, TODO)
- **Estado**: IMPULSE, PULLBACK, TOUCH, CHOP, INVALID (placeholder, TODO)
- **Side**: LONG, SHORT, WAIT (placeholder, TODO)

## 6. Filtros y Ordenamiento
- Orden default: Rate DESC, luego "impulse_strength" (placeholder)
- Filtros rápidos (UI):
  - A+ Setups: Rate>=8.0, touch_support==true, funding_ok==true
  - Impulso fresco: age<=3, ret_5m>=X, volZ_5m>=Y (X/Y en config)
  - Squeeze/contrarian: crowd_extreme==true o LS muy cargado
- Búsqueda por símbolo y filtro Top N (UI)

## 7. Estilo y UX
- Paleta moderna, tipografía Segoe UI, animaciones sutiles.
- Rate >=8.5 destacado en verde/fondo.
- Placeholders “—” donde falta dato.
- Filtros rápidos y búsqueda arriba de la tabla.

## 8. Extensión y Customización
- Para agregar features reales: implementar cálculo en scanner.py y exponer en DTO de candidatos recientes.
- Para nuevos endpoints: seguir patrón de dashboard.py (FastAPI, response dict/list).
- Para cambiar umbrales: editar config.yaml o parámetros en config.py.

## 9. Pruebas y Validación
- Ejecutar backend: `python -m src.main`
- Acceder a http://127.0.0.1:8000/
- Validar:
  - Solo aparecen símbolos nuevos (últimos 5 min, Rate >= 7.2)
  - Columnas y estilos correctos
  - Placeholders “—” donde falta dato
  - Filtros rápidos y búsqueda funcionan (UI)
  - Cambia umbral de Rate y recarga: tabla se actualiza
  - Espera 6+ min: símbolos viejos desaparecen
- Tests unitarios: `pytest tests/`

## 10. Notas para Desarrolladores
- Código limpio, modular, con nombres consistentes y comentarios clave.
- Si agregas features, deja TODOs claros y placeholders robustos.
- Mantén la estética y UX profesional.
- Si tienes dudas, revisa este README_REPORT.md y el código fuente.

---

**Contacto:**
- Autor original: [tu nombre/email]
- Colaboradores: [agrega aquí]

---

Este reporte sirve como guía rápida y referencia para extender, mantener o portar la app a otros entornos. Facilita onboarding y colaboración para cualquier programador o equipo.
