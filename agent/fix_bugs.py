# fix_bugs.py
content = open('agent/main.py', encoding='utf-8').read()

# Bug 1 fix: agregar patron_ficha
old1 = "    patron_marcador = r'\\[[A-Z_]+\\|[^\\]]*\\]'"
new1 = "    patron_marcador = r'\\[[A-Z_]+\\|[^\\]]*\\]'\n    patron_ficha    = r'(?s)\\n?[\\u2501\\u2500]{5,}.*?[\\u2501\\u2500]{5,}\\n?'"
content = content.replace(old1, new1)

# Bug 1 fix: aplicar patron_ficha antes del colapso de lineas
old2 = "    limpia = re.sub(r'\\n{3,}', '\\n\\n', limpia).strip()"
new2 = "    limpia = re.sub(patron_ficha, '', limpia)\n    limpia = re.sub(r'\\n{3,}', '\\n\\n', limpia).strip()"
content = content.replace(old2, new2)

open('agent/main.py', 'w', encoding='utf-8').write(content)
print('Bugs aplicados OK')