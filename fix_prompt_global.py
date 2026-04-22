content = open('agent/prompt_builder.py', encoding='utf-8').read()

old = '    return "\n".join(lineas)\ndef _instruccion_por_estado'
new = '    lineas.append("\\nREGLA OBLIGATORIA: Cuando el cliente entregue una direccion completa (calle, numero y comuna), incluye AL FINAL de tu respuesta este marcador exacto (el cliente NO lo ve): [ALERTA_SUPERVISOR|nombre=NOMBRE_REAL|tel=TELEFONO_REAL|dir=DIRECCION_COMPLETA] Reemplaza con los datos reales del lead. OBLIGATORIO sin excepcion cada vez que captures una direccion.")\n    return "\n".join(lineas)\ndef _instruccion_por_estado'

if old in content:
    content = content.replace(old, new)
    open('agent/prompt_builder.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
