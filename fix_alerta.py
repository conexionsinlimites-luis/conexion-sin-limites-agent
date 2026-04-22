content = open('agent/prompt_builder.py', encoding='utf-8').read()

old = '        return "Direcci\u00f3n recibida. Confirmar datos y disparar alerta al supervisor AHORA."'
new = '        return "Direcci\u00f3n recibida. Confirmar datos al cliente y en la MISMA respuesta incluir OBLIGATORIAMENTE este marcador exacto al final (invisible para el cliente):\\n[ALERTA_SUPERVISOR|nombre=NOMBRE|tel=TELEFONO|dir=DIRECCION]\\nReemplaza NOMBRE, TELEFONO y DIRECCION con los datos reales del lead."'

if old in content:
    content = content.replace(old, new)
    open('agent/prompt_builder.py', 'w', encoding='utf-8').write(content)
    print('OK - fix aplicado')
else:
    print('ERROR - texto no encontrado')
