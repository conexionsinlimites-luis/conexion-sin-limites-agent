TEMPLATES_FOLLOWUP = {
    "suave": "Hola {nombre}, te escribo porque varios ya están aprovechando mejores planes en {comuna}. No quiero que sigas pagando de más si puedes mejorar. ¿Quieres que lo revisemos?",
    "medio": "Hola {nombre}, justo hoy varios hicieron el cambio y bajaron su costo mensual en {comuna}. Si quieres, revisamos rápido tu caso y ves si te conviene.",
    "cierre": "Si ahora no te sirve, ningún problema 👍 ¿Prefieres que lo veamos mañana en la mañana o en la tarde al llegar del trabajo?",
    "reactivacion": None
}

TEMPLATE_INICIAL = "Hola {nombre}, te escribo porque en {comuna} están habilitando mejores planes de internet con más velocidad y mejor precio. ¿Quieres que revise si puedes mejorar lo que tienes actualmente?"

def get_mensaje_followup(tipo: str, nombre: str, comuna: str = None) -> str:
    template = TEMPLATES_FOLLOWUP.get(tipo)
    if not template:
        return None
    if comuna:
        return template.format(nombre=nombre, comuna=comuna)
    else:
        msg = template.replace(" en {comuna}", "").replace(" en {comuna}.", ".")
        return msg.format(nombre=nombre)
