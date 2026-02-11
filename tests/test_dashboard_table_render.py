# Test manual para tabla de alertas recientes (pro)
# 1. Levanta el backend: python -m src.main
# 2. Abre http://127.0.0.1:8000/
# 3. Valida visualmente:
#   - Solo aparecen símbolos nuevos de los últimos 5 minutos y Rate >= 7.2
#   - Columnas: Status, Symbol, Rate, Age, Ret_1m, Ret_5m, Ret_15m, VolZ_1m, VolZ_5m, OI_Δ5m, Funding, LS Acc, LS Pos, Estado, Side
#   - Rate >= 8.5 se ve destacado (verde/fondo)
#   - Filtros rápidos y búsqueda presentes arriba de la tabla
#   - Placeholders “—” donde no hay datos
# 4. Cambia el umbral de Rate en el backend y recarga: la tabla debe actualizarse
# 5. Espera 6+ minutos: los símbolos viejos desaparecen
# 6. (Opcional) Haz click en los filtros: debe mostrar alerta “TODO”
#
# Si todo lo anterior funciona, la integración es correcta.
