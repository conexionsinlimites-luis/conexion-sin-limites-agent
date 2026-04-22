content = open('agent/dashboard.py', encoding='utf-8').read()

idx_start = content.find('  .ncpn-label')
idx_end = content.find('  /* Modal deta')

css_nuevo = '''  .ncpn-label {
    font-size:.6rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
    color:var(--txt3);margin-bottom:.3rem;
  }
  .ncpn-input {
    width:100%;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);
    border-radius:8px;padding:.55rem .75rem;color:var(--txt);font-size:.82rem;
    font-family:'Space Grotesk',sans-serif;transition:border-color .2s;outline:none;
  }
  .ncpn-input:focus { border-color:rgba(0,212,255,.5);box-shadow:0 0 0 3px rgba(0,212,255,.08); }
  .ncpn-input::placeholder { color:#444; }
  .ncpn-select option { background:#111; }
  .ncpn-preview-box {
    background:rgba(0,212,255,.04);border:1px solid rgba(0,212,255,.15);
    border-radius:12px;padding:1rem;
  }
  .ncpn-preview-num {
    font-family:'Orbitron',sans-serif;font-size:2rem;font-weight:700;
    color:var(--neon);line-height:1;text-align:center;
  }
  .ncpn-preview-sub { font-size:.7rem;color:var(--txt3);margin-top:.25rem;text-align:center; }
  .ncpn-preview-btn {
    width:100%;padding:.65rem;background:rgba(0,212,255,.08);
    border:1px solid rgba(0,212,255,.3);border-radius:10px;
    color:var(--neon);font-size:.8rem;font-weight:600;cursor:pointer;
    font-family:'Space Grotesk',sans-serif;letter-spacing:.04em;
    transition:all .2s;
  }
  .ncpn-preview-btn:hover { background:rgba(0,212,255,.18);box-shadow:0 0 12px rgba(0,212,255,.2); }
  .ncpn-cancel-btn {
    padding:.55rem 1.25rem;background:transparent;border:1px solid #333;
    border-radius:8px;color:#888;font-size:.8rem;cursor:pointer;
    font-family:'Space Grotesk',sans-serif;transition:all .2s;
  }
  .ncpn-cancel-btn:hover { border-color:#555;color:#aaa; }
  .ncpn-create-btn {
    padding:.55rem 1.5rem;background:var(--neon);border:none;
    border-radius:8px;color:#000;font-size:.82rem;font-weight:700;cursor:pointer;
    font-family:'Space Grotesk',sans-serif;transition:all .2s;
  }
  .ncpn-create-btn:hover { background:#fff;transform:translateY(-1px); }
  .ncpn-create-btn:disabled { opacity:.5;cursor:not-allowed;transform:none; }
  @media(max-width:600px) {
    .ncpn-input { font-size:.85rem;padding:.6rem .8rem; }
    .ncpn-preview-num { font-size:1.6rem; }
  }
  '''

content = content[:idx_start] + css_nuevo + content[idx_end:]
open('agent/dashboard.py', 'w', encoding='utf-8').write(content)
print('CSS premium OK, longitud:', len(css_nuevo))