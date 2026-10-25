import importlib.util, ctypes, sys
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass
spec = importlib.util.spec_from_file_location('cns', r'D:\codex\codex-net-switch\codex_net_switch.pyw')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import tkinter as tk

initial_theme = m.load_theme()
root = tk.Tk()
app = m.App(root)
app._resize_center()
results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)

def finish():
    try:
        m.save_theme(initial_theme)
    except Exception:
        pass
    ok = sum(1 for _, c in results if c)
    print(f'GUI-TESTS {ok}/{len(results)}')
    root.destroy()
    sys.exit(0 if ok == len(results) else 1)

def stage_late():  # 在 mainloop 中跑的完整检查链
    check('hero verdict rendered', ('正常' in app.hero_state.cget('text')) or ('⚠' in app.hero_state.cget('text')))
    lines = m.read_hosts()
    present, sg = m.block_state(lines) if lines else (None, None)
    hero_txt = app.hero_state.cget('text')
    if present is True:
        check('consistency: block-on shows green verdict', '✅' in hero_txt)
    app.toggle_help()
    root.update()
    check('help expands', app.help.winfo_ismapped())
    app.copy_diagnose()
    root.after(1600, finish)  # 等子进程诊断+剪贴板

def stage_mid():  # 折叠/主题压力
    check('8 domain rows built', len(app.rows) == 8)
    check('detail folded by default', not app.detail.winfo_ismapped())
    app.toggle_detail()
    check('detail expands', app.detail.winfo_ismapped())
    app.toggle_detail()
    check('detail refolds', not app.detail.winfo_ismapped())
    for _ in range(6):
        app.toggle_theme()
    check('theme toggle x6 without crash', True)
    check('rows rebuilt correctly after toggles', len(app.rows) == 8)
    check('hero survived rebuild', app.hero_state.winfo_exists())
    check('statsig button text reflects state',
          app.btn_sg.cget('text') in ('防误伤：放行 statsig', '恢复 statsig 屏蔽', '防误伤（需先启用屏蔽）'))
    root.after(2200, stage_late)  # 等 _collect 线程把 hero 刷新出来

root.after(600, stage_mid)  # 首轮 _collect 完成后开始
root.mainloop()
