from router import OptimizedRouter

# Inicializar el enrutador con un tamaño de lote de 3
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