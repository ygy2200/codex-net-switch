# -*- coding: utf-8 -*-
"""codex 网络开关 —— 控制 hosts 境外屏蔽（B+C 级合并块）

用法:
  双击/无参数        GUI 面板（普通权限，改 hosts 时弹 UAC）
  --apply            写入屏蔽块（需管理员）
  --remove           删除屏蔽块（需管理员）
  --statsig on|off   statsigapi.net 放行(off)/屏蔽(on)（需管理员）
  --status           输出状态 JSON（普通权限）
  --selftest         逻辑自测（不动真 hosts）
"""
import sys, os, socket, subprocess, json, ctypes, threading

HOSTS = r'C:\Windows\System32\drivers\etc\hosts'
BEGIN = '# >>> CODEX-OFFSHORE-BLOCK (managed by codex_net_switch) >>>'
END   = '# <<< CODEX-OFFSHORE-BLOCK <<<'
# 域名清单；statsig=True 的行是公共域名，支持单独放行（行前加 # 即放行）
DOMAINS = [
    ('auth.openai.com', False), ('api.openai.com', False),
    ('statsigapi.net', True), ('statsigcdn.openai.com', False),
    ('ab.chatgpt.com', False), ('codex-portals.api.openai.com', False),
    ('o33249.ingest.us.sentry.io', False), ('o33249.ingest.sentry.io', False),
]
OLD_MARKERS = ('# codex sentry block 2026-10-25', '# codex offshore block 2026-10-25')
ICON = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'icon.ico')

USE = {
    'auth.openai.com': 'OpenAI 登录检查（API key 模式不需要）',
    'api.openai.com': 'OpenAI API / 插件目录（已用 deepseek 替代）',
    'statsigapi.net': '⚠ 公共域名：特性开关服务（codex 在用）',
    'statsigcdn.openai.com': 'OpenAI 专属特性开关 CDN',
    'ab.chatgpt.com': 'A/B 实验配置',
    'codex-portals.api.openai.com': '云通道（MASQUE）',
    'o33249.ingest.us.sentry.io': '错误遥测上报（Sentry）',
    'o33249.ingest.sentry.io': '错误遥测上报（Sentry）',
}

def build_block(statsig_on=True):
    lines = [BEGIN]
    for dom, is_statsig in DOMAINS:
        for ip in ('127.0.0.1', '::1'):
            line = f'{ip} {dom}'
            if is_statsig and not statsig_on:
                lines.append('# ' + line + '  # [statsig] 放行中')
            else:
                lines.append(line)
    lines.append(END)
    return lines

def read_hosts():
    try:
        with open(HOSTS, 'r', encoding='utf-8', errors='replace') as f:
            return f.read().splitlines()
    except OSError:
        return None

def strip_managed(lines):
    """删除标准块与旧标记块，返回 (剩余行, 旧块是否存在)"""
    out, in_block, old_found = [], False, False
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == BEGIN:
            in_block = True; i += 1; continue
        if ln.strip() == END:
            in_block = False; i += 1; continue
        if any(ln.startswith(m) for m in OLD_MARKERS):
            old_found = True
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if (nxt.startswith('127.0.0.1 ') or nxt.startswith('::1 ')
                        or nxt.strip() == '' or nxt.lstrip().startswith('# o33249')
                        or 'sentry.io' in nxt or 'openai.com' in nxt or 'statsig' in nxt or 'chatgpt' in nxt):
                    i += 1
                else:
                    break
            continue
        if not in_block:
            out.append(ln)
        i += 1
    while out and out[-1].strip() == '':
        out.pop()
    return out, old_found

def write_block(mode, statsig_on=True):
    """mode: apply / remove；需管理员。返回 (ok, msg)"""
    lines = read_hosts()
    if lines is None:
        return False, '无法读取 hosts（权限）'
    rest, old_found = strip_managed(lines)
    if mode == 'apply':
        rest = rest + [''] + build_block(statsig_on) + ['']
    tmp = HOSTS + '.codexnet.tmp'
    try:
        with open(tmp, 'w', encoding='ascii', errors='replace') as f:
            f.write('\n'.join(rest) + '\n')
        os.replace(tmp, HOSTS)  # 原子替换：中途崩溃不会留半截 hosts
    except PermissionError:
        return False, '写入失败：需要管理员权限'
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f'写入失败：{e}'
    subprocess.run(['ipconfig', '/flushdns'], capture_output=True)
    return True, ('已' + ('启用' if mode == 'apply' else '停用') + '屏蔽' + ('（含旧块迁移）' if old_found else ''))

def block_state(lines):
    """返回 (块存在, statsig当前屏蔽?)；兼容识别未迁移的旧格式两块"""
    try:
        b, e = lines.index(BEGIN), lines.index(END)
        seg = lines[b:e]
    except ValueError:
        seg = [l for l in lines if any(l.startswith(m) for m in OLD_MARKERS)] + \
              [l for l in lines if l.startswith(('127.0.0.1 ', '::1 ')) and
               any(d in l for d, _ in DOMAINS)]
        if not seg:
            return False, None
    statsig_lines = [l for l in seg if 'statsigapi.net' in l]
    statsig_on = any(not l.lstrip().startswith('#') for l in statsig_lines)
    return True, statsig_on

def resolve_status():
    """逐域名实测解析；全回环=blocked，部分回环=partial，其余=pass"""
    res = {}
    for dom, _ in DOMAINS:
        try:
            ips = sorted(set(a[4][0] for a in socket.getaddrinfo(dom, 443, proto=socket.IPPROTO_TCP)))
            loop = [ip for ip in ips if ip in ('127.0.0.1', '::1')]
            if ips and len(loop) == len(ips):
                res[dom] = 'blocked'
            elif loop:
                res[dom] = 'partial'
            else:
                res[dom] = 'pass'
        except Exception:
            res[dom] = 'error'
    return res

def flclash_status():
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq FlClash.exe'],
                             capture_output=True, timeout=5,
                             encoding='gbk', errors='replace').stdout
        running = 'FlClash.exe' in out
    except Exception:
        running = False
    alive = False
    try:
        s = socket.create_connection(('127.0.0.1', 7890), timeout=1.5); s.close(); alive = True
    except Exception:
        pass
    return running, alive

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def run_elevated(args):
    """弹 UAC 以管理员重入自身（打包后即 exe 自身），返回是否成功发起"""
    if getattr(sys, 'frozen', False):
        target, params = sys.executable, args
    else:
        target, params = sys.executable, '"{}" {}'.format(os.path.abspath(__file__), args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', target, params, None, 0)
    return rc > 32

# ---------------- CLI ----------------
if len(sys.argv) > 1:
    a = sys.argv[1]
    if a == '--apply':
        ok, msg = write_block('apply'); print(msg); sys.exit(0 if ok else 1)
    if a == '--remove':
        ok, msg = write_block('remove'); print(msg); sys.exit(0 if ok else 1)
    if a == '--statsig':
        lines = read_hosts()
        present, _ = block_state(lines) if lines else (False, None)
        if not present:
            print('屏蔽块未启用，请先 --apply 再切 statsig'); sys.exit(1)
        ok, msg = write_block('apply', statsig_on=(sys.argv[2] == 'on'))
        print(msg); sys.exit(0 if ok else 1)
    if a == '--status':
        lines = read_hosts()
        present, sg = block_state(lines) if lines else (None, None)
        print(json.dumps({'block': present, 'statsig_blocked': sg,
                          'resolve': resolve_status(),
                          'flclash': dict(zip(('process', 'port7890'), flclash_status()))},
                         ensure_ascii=False, indent=1))
        sys.exit(0)
    if a == '--selftest':
        tests = []
        b = build_block(True); tests.append(('block has markers', b[0] == BEGIN and b[-1] == END))
        b2 = build_block(False)
        sg = [l for l in b2 if 'statsigapi.net' in l]
        tests.append(('statsig off comments only statsig lines',
                      all(l.startswith('# ') for l in sg) and len(sg) == 2))
        host_like = ['# header', BEGIN, '127.0.0.1 auth.openai.com', '::1 auth.openai.com',
                     '# 127.0.0.1 statsigapi.net  # [statsig] 放行中', END, '1.2.3.4 other.com']
        rest, old = strip_managed(host_like)
        tests.append(('strip keeps outside lines', rest == ['# header', '1.2.3.4 other.com']))
        tests.append(('strip no-old-marker', old is False))
        old_like = ['# x', '# codex sentry block 2026-10-25 - delete', '127.0.0.1 o33249.ingest.sentry.io',
                    '# codex offshore block 2026-10-25 (C-level)', '127.0.0.1 auth.openai.com', '::1 auth.openai.com',
                    '2.3.4.5 mine.com']
        rest2, old2 = strip_managed(old_like)
        tests.append(('strip old blocks keeps others', rest2 == ['# x', '2.3.4.5 mine.com'] and old2 is True))
        cur = read_hosts()
        if cur is not None:
            present, sgx = block_state(cur)
            if present:
                doms_in = {d for d, _ in DOMAINS}
                got = {l.split()[1] for l in cur if l.startswith(('127.0.0.1 ', '::1 ')) and len(l.split()) > 1 and l.split()[1] in doms_in}
                tests.append(('live hosts covers all domains', got == doms_in))
        bad = [n for n, ok in tests if not ok]
        for n, ok in tests: print(('PASS' if ok else 'FAIL'), n)
        # 假 hosts 全流程回归（不碰真 hosts）
        import tempfile
        orig_hosts, fake = HOSTS, None
        try:
            fd, fake = tempfile.mkstemp(suffix='.hosts'); os.close(fd)
            with open(fake, 'w', encoding='ascii') as f:
                f.write('# original\n1.2.3.4 keep.me\n')
            globals()['HOSTS'] = fake
            ok1, _ = write_block('apply')
            lines = read_hosts()
            p1, s1 = block_state(lines)
            t1 = ('fake-hosts apply (block+statsig on+keeps others)',
                  ok1 and p1 is True and s1 is True and any('keep.me' in l for l in lines))
            write_block('apply', statsig_on=False)
            lines = read_hosts()
            _, s2 = block_state(lines)
            leaked = [l for l in lines if 'statsigapi.net' in l and not l.lstrip().startswith('#')]
            t2 = ('fake-hosts statsig off (no active statsig line)', s2 is False and not leaked)
            write_block('remove')
            with open(fake, encoding='ascii') as f:
                final = f.read()
            t3 = ('fake-hosts remove restores original exactly', final == '# original\n1.2.3.4 keep.me\n')
            for n, ok in (t1, t2, t3):
                tests.append((n, ok)); print(('PASS' if ok else 'FAIL'), n)
                if not ok: bad.append(n)
        finally:
            globals()['HOSTS'] = orig_hosts
            if fake:
                try: os.remove(fake)
                except OSError: pass
        sys.exit(1 if bad else 0)
    sys.exit(0)

# ---------------- GUI ----------------
import tkinter as tk
import tkinter.messagebox

C = dict(bg='#f2f4f7', card='#ffffff', line='#e2e6ec',
         ink='#1c2333', sub='#71798a',
         blue='#2563eb', blue_d='#1e4fc4', blue_bg='#dbeafe',
         green='#15803d', green_bg='#dcfce7',
         red='#dc2626', red_bg='#fee2e2',
         yellow='#b45309', yellow_bg='#fef3c7', gray_bg='#e8eaef')

FONT, FONT_M, FONT_B = 'Microsoft YaHei UI', ('Microsoft YaHei UI', 9), ('Microsoft YaHei UI', 11, 'bold')

class App:
    def __init__(self, root):
        self.root = root
        root.title('Codex 网络开关')
        root.configure(bg=C['bg'])
        try:
            root.iconbitmap(ICON)
        except Exception:
            pass
        root.resizable(False, False)
        self.rows, self._closed = {}, False
        self._build()
        self.refresh_async()

    # ---- 构件 ----
    def _resize_center(self):
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), 560)
        h = max(self.root.winfo_reqheight(), 420)
        x = (self.root.winfo_screenwidth() - w) // 2
        y = max((self.root.winfo_screenheight() - h) // 3, 0)
        self.root.geometry(f'{w}x{h}+{x}+{y}')

    def card(self, master, **pack):
        f = tk.Frame(master, bg=C['card'], highlightbackground=C['line'], highlightthickness=1)
        f.pack(**pack)
        return f

    def badge(self, master, text, fg, bg):
        return tk.Label(master, text=text, fg=fg, bg=bg, font=(FONT, 9, 'bold'),
                        padx=9, pady=2)

    def _build(self):
        # 标题行
        head = tk.Frame(self.root, bg=C['bg']); head.pack(fill='x', padx=16, pady=(14, 8))
        tk.Label(head, text='Codex 网络开关', font=(FONT, 15, 'bold'),
                 fg=C['ink'], bg=C['bg']).pack(side='left')
        tk.Button(head, text='刷新', font=FONT_M, relief='flat', cursor='hand2',
                  bg=C['gray_bg'], fg=C['ink'], activebackground=C['line'],
                  padx=12, pady=2, command=lambda: self.refresh_async(manual=True)).pack(side='right')

        # 三张状态卡
        cards = tk.Frame(self.root, bg=C['bg']); cards.pack(fill='x', padx=16, pady=(0, 10))
        self.card_stats = []
        for key, title in (('block', '屏蔽状态'), ('fl', 'FlClash'), ('port', '代理端口 7890')):
            f = self.card(cards, side='left', padx=(0, 10), fill='both', expand=True)
            tk.Label(f, text=title, font=FONT_M, fg=C['sub'], bg=C['card']).pack(anchor='w', padx=14, pady=(10, 0))
            v = tk.Label(f, text='…', font=(FONT, 13, 'bold'), fg=C['sub'], bg=C['card'])
            v.pack(anchor='w', padx=14, pady=(2, 12))
            self.card_stats.append(v)

        # 域名列表卡
        lst_wrap = self.card(self.root, fill='both', expand=True, padx=16, pady=(0, 10))
        lh = tk.Frame(lst_wrap, bg=C['card']); lh.pack(fill='x', padx=14, pady=(10, 2))
        for t, cw, a in (('域名', 250, 'w'), ('状态', 80, 'center'), ('用途', 300, 'w')):
            tk.Label(lh, text=t, font=FONT_M, fg=C['sub'], bg=C['card'], width=cw // 8, anchor=a).pack(side='left')
        tk.Frame(lst_wrap, bg=C['line'], height=1).pack(fill='x', padx=14)
        for dom, is_sg in DOMAINS:
            row = tk.Frame(lst_wrap, bg=C['card']); row.pack(fill='x', padx=6)
            tk.Label(row, text=dom, font=('Consolas', 9), fg=C['ink'], bg=C['card'],
                     width=31, anchor='w').pack(side='left')
            st = self.badge(row, '…', C['sub'], C['gray_bg'])
            st.pack(side='left', padx=(0, 4))
            tk.Label(row, text=USE[dom], font=FONT_M, fg=C['sub'], bg=C['card'],
                     width=40, anchor='w').pack(side='left')
            row.bind('<Enter>', lambda e, r=row: r.config(bg='#f6f9ff'))
            row.bind('<Leave>', lambda e, r=row: r.config(bg=C['card']))
            self.rows[dom] = st

        # 操作区
        ops = tk.Frame(self.root, bg=C['bg']); ops.pack(fill='x', padx=16, pady=(0, 6))
        self.btn_on = tk.Button(ops, text='启用屏蔽', font=(FONT, 10, 'bold'), relief='flat',
                                cursor='hand2', padx=18, pady=7,
                                command=lambda: self.act('--apply'))
        self.btn_on.pack(side='left')
        self.btn_off = tk.Button(ops, text='停用屏蔽', font=(FONT, 10, 'bold'), relief='flat',
                                 cursor='hand2', padx=18, pady=7,
                                 command=lambda: self.act('--remove'))
        self.btn_off.pack(side='left', padx=(10, 0))
        self.btn_sg = tk.Button(ops, text='statsig 放行', font=(FONT, 10, 'bold'), relief='flat',
                                cursor='hand2', padx=14, pady=7,
                                bg=C['yellow_bg'], fg=C['yellow'],
                                activebackground=C['yellow_bg'], command=self.toggle_statsig)
        self.btn_sg.pack(side='left', padx=(10, 0))
        self.btn_sg._normal_bg = C['yellow_bg']
        for b in (self.btn_on, self.btn_off, self.btn_sg):
            self._bind_hover(b)

        # 底部状态条 + 可折叠说明
        self.tip = tk.Label(self.root, text='', font=FONT_M, fg=C['sub'], bg=C['bg'], anchor='w')
        self.tip.pack(fill='x', padx=16)
        self.fold_lbl = tk.Label(self.root, text='▸  影响说明', font=FONT_M, fg=C['blue'],
                                 bg=C['bg'], cursor='hand2', anchor='w')
        self.fold_lbl.pack(fill='x', padx=16, pady=(6, 0))
        self.fold_lbl.bind('<Button-1>', lambda e: self.toggle_help())
        self.help = tk.Text(self.root, height=10, font=FONT_M, wrap='word', relief='flat',
                            bg='#eef1f5', fg=C['ink'], padx=12, pady=8, spacing2=3)
        self.help.insert('end',
            '· 屏蔽开启：codex 对上述域名的请求毫秒级失败，不再傻等超时——FlClash 关着启动也快；\n'
            '  对话流量走 api.deepseek.com（国内直连），完全不受影响。\n'
            '· 屏蔽停用：恢复原样。FlClash 开着时 codex 走系统代理一切正常；关着时境外请求会\n'
            '  超时变慢（回到屏蔽前的状态）。想用 OpenAI 官方账号 / 云端功能时请停用屏蔽。\n'
            '· statsigapi.net 是清单里唯一的公共域名：若其他软件联网异常，点「statsig 放行」\n'
            '  单独放行它（其余屏蔽保持生效）。误伤判别：ping statsigapi.net 返回 127.0.0.1\n'
            '  即屏蔽在生效。\n'
            '· 操作会弹 UAC 管理员确认（改 hosts 需要）；旧格式屏蔽已于 2026-10-25 迁移为\n'
            '  标准块（>>> CODEX-OFFSHORE-BLOCK >>> 标记），工具按标记精准增删。\n'
            '· codex 版本大更新后若启动又变慢，可能是遥测域名换了——找 agent 重新取证更新清单。\n'
            '· 回滚兜底：etc 目录下有 hosts.bak-20261025-sentry / -cblock 两份历史备份。')
        self.help.config(state='disabled')

    def toggle_help(self):
        if self.help.winfo_ismapped():
            self.help.pack_forget(); self.fold_lbl.config(text='▸  影响说明')
        else:
            self.help.pack(fill='x', padx=16, pady=(4, 10))
            self.fold_lbl.config(text='▼  影响说明')

    # ---- hover ----
    @staticmethod
    def _lighten(color):
        try:
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            f = lambda v: min(255, int(v + (255 - v) * 0.18))
            return f'#{f(r):02x}{f(g):02x}{f(b):02x}'
        except Exception:
            return color

    def _bind_hover(self, btn):
        btn.bind('<Enter>', lambda e: btn.config(
            bg=self._lighten(getattr(btn, '_normal_bg', btn.cget('bg')))))
        btn.bind('<Leave>', lambda e: btn.config(
            bg=getattr(btn, '_normal_bg', btn.cget('bg'))))

    # ---- 数据刷新（后台线程）----
    def refresh_async(self, manual=False):
        if manual:
            self._tip_sticky = False
            self.tip.config(text='刷新中…', fg=C['sub'])
        threading.Thread(target=self._collect, daemon=True).start()

    def _collect(self):
        lines = read_hosts()
        present, sg = block_state(lines) if lines else (None, None)
        run_, alive = flclash_status()
        data = (present, sg, run_, alive, resolve_status())
        try:
            self.root.after(0, lambda: self._apply(data))
        except tk.TclError:
            pass

    def _apply(self, data):
        if self._closed:
            return
        present, sg, run_, alive, res = data
        b, fl, pt = self.card_stats
        if present is None:
            b.config(text='无法读取', fg=C['red'])
        else:
            b.config(text='● 开启' if present else '○ 停用', fg=C['green'] if present else C['sub'])
        fl.config(text='运行中' if run_ else '未运行', fg=C['green'] if run_ else C['sub'])
        pt.config(text='可用' if alive else '不通', fg=C['green'] if alive else C['red'])
        # 主按钮状态高亮：开启→停用醒目；停用→启用醒目
        on = present is True
        off_hot = present is False
        self.btn_on.config(bg=C['blue'] if on else C['gray_bg'],
                           fg='#ffffff' if on else C['sub'],
                           activebackground=C['blue_d'] if on else C['line'],
                           activeforeground='#ffffff' if on else C['ink'])
        self.btn_on._normal_bg = C['blue'] if on else C['gray_bg']
        self.btn_off.config(bg=C['red'] if off_hot else C['gray_bg'],
                            fg='#ffffff' if off_hot else C['sub'],
                            activebackground='#b91c1c' if off_hot else C['line'],
                            activeforeground='#ffffff' if off_hot else C['ink'])
        self.btn_off._normal_bg = C['red'] if off_hot else C['gray_bg']
        sg_ok = present is True
        self.btn_sg.config(
            state='normal' if sg_ok else 'disabled',
            text=('statsig 恢复屏蔽' if sg is False else 'statsig 放行') if sg_ok
                 else 'statsig（需先启用屏蔽）')
        for dom, _ in DOMAINS:
            st = res.get(dom, 'error')
            w = self.rows[dom]
            if st == 'blocked':
                w.config(text='已屏蔽', fg=C['green'], bg=C['green_bg'])
            elif st == 'partial':
                w.config(text='部分', fg=C['yellow'], bg=C['yellow_bg'])
            elif st == 'pass':
                w.config(text='放行', fg=C['sub'], bg=C['gray_bg'])
            else:
                w.config(text='解析失败', fg=C['red'], bg=C['red_bg'])
        if not getattr(self, '_tip_sticky', False):
            self.tip.config(text='')
        if not self._closed:
            self.root.after(15000, self.refresh_async)

    # ---- 操作 ----
    def act(self, arg):
        if not run_elevated(arg):
            self.tip.config(text='✕ 未能启动管理员进程（UAC 被取消）', fg=C['red'])
            return
        self.tip.config(text='⏳ 已发起，等待 UAC 确认后自动刷新…', fg=C['blue'])
        threading.Thread(target=self._watch_hosts, daemon=True).start()

    def _watch_hosts(self):
        import time
        before = self._hosts_sig()
        changed = False
        for _ in range(60):  # 最多等 30 秒
            time.sleep(0.5)
            if self._hosts_sig() != before:
                changed = True
                break
        def done():
            if self._closed:
                return
            if not changed:
                self._tip_sticky = True
                self.tip.config(text='✕ 30 秒内未检测到变化：UAC 可能被取消或写入失败', fg=C['red'])
            self.refresh_async()
        try:
            self.root.after(0, done)
        except tk.TclError:
            pass

    @staticmethod
    def _hosts_sig():
        try:
            with open(HOSTS, 'rb') as f:
                return hash(f.read())
        except OSError:
            return None

    def toggle_statsig(self):
        lines = read_hosts()
        _, sg = block_state(lines) if lines else (None, None)
        self.act('--statsig ' + ('off' if sg else 'on'))

if __name__ == '__main__':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高分屏清晰
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('codex.netswitch.app')
    except Exception:
        pass
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'codex_net_switch_singleton')
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _r = tk.Tk(); _r.withdraw()
        tk.messagebox.showinfo('Codex 网络开关', '已在运行，请使用已有窗口。')
        sys.exit(0)
    root = tk.Tk()
    app = App(root)
    app._resize_center()
    root.protocol('WM_DELETE_WINDOW', lambda: (setattr(app, '_closed', True), root.destroy()))
    root.mainloop()
