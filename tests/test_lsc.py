# Copyright (c) 2026 Stephen Díaz
# SovNode - Local Desktop AI Application
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
from router import OptimizedRouter

# ES: Prueba manual: inicializa el enrutador con tamaño de lote 3 y envía 3 mensajes
# para verificar el comportamiento de agrupamiento por lotes (batching).
# EN: Manual test: initializes the router with batch size 3 and sends 3 messages
# to verify batch-grouping (batching) behavior.
opt = OptimizedRouter(batch_size=3)

mensajes = [
    "¿Cómo optimizar el consumo de memoria en la arquitectura del monolito?",
    "El sistema local de enrutamiento reduce drásticamente la latencia.",
    "Ejecuta el volcado final del búfer para comprobar el lote completo."
]

print("=== VERIFICACIÓN DEL BÚFER DE LOTES ===")
for i, msg in enumerate(mensajes, 1):
    resultado = opt.submit(msg)
    print(f"\nEnvío {i}: '{msg}'")
    if resultado is None:
        print("  └─ Status: Retenido en búfer (esperando completar lote)...")
    else:
        print("  └─ Status: ¡Lote completado! Resultados procesados:")
        for res in resultado:
            print(f"      • Ruta: {res.path.value} | Score: {res.score} | Tags: {[t.value for t in res.tags]}")
