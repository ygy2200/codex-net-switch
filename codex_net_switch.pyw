# -*- coding: utf-8 -*-
"""codex 网络开关 —— 控制 hosts 境外屏蔽（B+C 级合并块）

用法:
  双击/无参数        GUI 面板（普通权限，改 hosts 时弹 UAC）
  --apply            写入屏蔽块（需管理员）
  --remove           删除屏蔽块（需管理员）
  --statsig on|off   statsigapi.net 放行(off)/屏蔽(on)（需管理员）
  --status           输出状态 JSON（含各域名解析 IP，普通权限）
  --diagnose         输出诊断报告文本（排障用）
  --selftest         14 项逻辑自测（不碰真实 hosts）
  --minimize         启动后最小化（配合开机自启）
  --version / --help 版本 / 帮助
"""
import sys, os, socket, subprocess, json, ctypes, threading, winreg

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'

def autostart_get():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        try:
            winreg.QueryValueEx(k, 'CodexNetSwitch')
            return True
        finally:
            winreg.CloseKey(k)
    except OSError:
        return False

def autostart_set(on):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if on:
                target, params = elevated_params('--minimize')
                winreg.SetValueEx(k, 'CodexNetSwitch', 0, winreg.REG_SZ,
                                  f'"{target}" {params}')
            else:
                winreg.DeleteValue(k, 'CodexNetSwitch')
        finally:
            winreg.CloseKey(k)
        return True
    except OSError:
        return False

def self_cmd_args(cli_arg):
    """以子进程跑自身 CLI 的参数表"""
    if getattr(sys, 'frozen', False):
        return [sys.executable, cli_arg]
    return [sys.executable, os.path.abspath(__file__), cli_arg]

VERSION = '2.0.5'
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
    'auth.openai.com': 'OpenAI 的登录验证服务器（你用 deepseek 钥匙，用不着它）',
    'api.openai.com': 'OpenAI 官方接口（已换成 deepseek，不再走它）',
    'statsigapi.net': '⚠ 公用服务：决定开哪些实验功能，别的软件可能也用它',
    'statsigcdn.openai.com': '上一条的 OpenAI 专用资源库',
    'ab.chatgpt.com': '小白鼠分组服务器（A/B 测试用）',
    'codex-portals.api.openai.com': 'codex 连 OpenAI 云端的专用通道',
    'o33249.ingest.us.sentry.io': '报错回传服务（发不出去也不影响使用）',
    'o33249.ingest.sentry.io': '报错回传服务（发不出去也不影响使用）',
}

GROUPS = [
    ('登录与 API', ['auth.openai.com', 'api.openai.com']),
    ('特性开关', ['statsigapi.net', 'statsigcdn.openai.com', 'ab.chatgpt.com']),
    ('云通道', ['codex-portals.api.openai.com']),
    ('错误遥测', ['o33249.ingest.us.sentry.io', 'o33249.ingest.sentry.io']),
]

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
        with open(HOSTS, 'r', encoding='utf-8-sig', errors='replace') as f:
            # utf-8-sig 自动剥 BOM；再修历史损坏首行（BOM 曾被 ascii-replace 毁成 '?#'）
            lines = f.read().splitlines()
            if lines and lines[0].startswith('?#'):
                lines[0] = lines[0][1:]
            return lines
    except OSError:
        return None

def strip_managed(lines):
    """删除标准块与旧标记块，返回 (剩余行, 旧块是否存在, 块是否损坏)"""
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
    return out, old_found, in_block  # 循环完仍 in_block = 有 BEGIN 无 END，块损坏

def write_block(mode, statsig_on=True):
    """mode: apply / remove；需管理员。返回 (ok, msg)"""
    lines = read_hosts()
    if lines is None:
        return False, '无法读取 hosts（权限）'
    rest, old_found, broken = strip_managed(lines)
    if broken:
        return False, ('hosts 屏蔽块标记损坏（有 BEGIN 无 END）：'
                       '请手动删除损坏的标记块后再操作，工具拒绝在损坏文件上写入')
    if mode == 'apply':
        rest = rest + [''] + build_block(statsig_on) + ['']
    rotate_backup()
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
    got = read_hosts()  # 写后回读验证（一致性自检）
    if got is None or [l for l in got if l.strip()] != [l for l in rest if l.strip()]:
        return False, '警告：写入后回读不一致，请检查 hosts'
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

def resolve_all():
    """逐域名实测解析；返回 {域名: (状态, IP清单)}"""
    out = {}
    for dom, _ in DOMAINS:
        try:
            ips = sorted(set(a[4][0] for a in socket.getaddrinfo(dom, 443, proto=socket.IPPROTO_TCP)))
            loop = [ip for ip in ips if ip in ('127.0.0.1', '::1')]
            if ips and len(loop) == len(ips):
                state = 'blocked'
            elif loop:
                state = 'partial'
            else:
                state = 'pass'
        except Exception:
            ips, state = [], 'error'
        out[dom] = (state, ips)
    return out

def resolve_status():
    return {d: s for d, (s, _) in resolve_all().items()}

def resolve_detail():
    return {d: ips for d, (_, ips) in resolve_all().items()}

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

def deepseek_status():
    """对话 API 连通性（DNS+TCP 实测）"""
    try:
        s = socket.create_connection(('api.deepseek.com', 443), timeout=2.5)
        s.close()
        return True
    except Exception:
        return False

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def elevated_params(args):
    """构造提权重入的 (target, params)；打包后即 exe 自身"""
    if getattr(sys, 'frozen', False):
        return sys.executable, args
    return sys.executable, '"{}" {}'.format(os.path.abspath(__file__), args)

def run_elevated(args):
    """弹 UAC 以管理员重入自身，返回是否成功发起"""
    target, params = elevated_params(args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', target, params, None, 0)
    return rc > 32

def rotate_backup():
    """写 hosts 前滚动备份：.2→.3、.1→.2、当前→.1（保留 3 份）"""
    d = os.path.dirname(HOSTS)
    n3 = os.path.join(d, 'hosts.bak-codexnet.3')
    n2 = os.path.join(d, 'hosts.bak-codexnet.2')
    n1 = os.path.join(d, 'hosts.bak-codexnet.1')
    try:
        if os.path.exists(n3):
            os.remove(n3)
        if os.path.exists(n2):
            os.replace(n2, n3)
        if os.path.exists(n1):
            os.replace(n1, n2)
        if os.path.exists(HOSTS):
            with open(HOSTS, 'rb') as s, open(n1, 'wb') as t:
                t.write(s.read())
    except OSError:
        pass  # 备份失败不阻断主流程（有历史 .bak 兜底）

def uac_log(msg):
    """提权实例的操作日志（无控制台，落 %TEMP% 供排查）"""
    import time
    try:
        with open(os.path.join(os.environ.get('TEMP', '.'), 'codexnet_uac.log'), 'a',
                  encoding='utf-8') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} [{VERSION}] {msg}\n')
    except OSError:
        pass

# ---------------- CLI ----------------
if len(sys.argv) > 1:
    a = sys.argv[1]
    if a in ('--help', '-h', '/?'):
        print(__doc__.strip())
        print(f'Version {VERSION}')
        sys.exit(0)
    if a == '--version':
        print(f'CodexNetSwitch {VERSION}')
        sys.exit(0)
    if a == '--apply':
        ok, msg = write_block('apply'); uac_log(f'--apply: {ok} {msg}'); print(msg); sys.exit(0 if ok else 1)
    if a == '--remove':
        ok, msg = write_block('remove'); uac_log(f'--remove: {ok} {msg}'); print(msg); sys.exit(0 if ok else 1)
    if a == '--statsig':
        arg = sys.argv[2].lower() if len(sys.argv) > 2 else ''
        if arg not in ('on', 'off'):
            print('用法: --statsig on|off'); sys.exit(1)
        lines = read_hosts()
        present, _ = block_state(lines) if lines else (False, None)
        if not present:
            print('屏蔽块未启用，请先 --apply 再切 statsig'); sys.exit(1)
        ok, msg = write_block('apply', statsig_on=(arg == 'on'))
        uac_log(f'--statsig {arg}: {ok} {msg}')
        print(msg); sys.exit(0 if ok else 1)
    if a == '--status':
        lines = read_hosts()
        present, sg = block_state(lines) if lines else (None, None)
        res = resolve_status()
        print(json.dumps({'version': VERSION, 'block': present, 'statsig_blocked': sg,
                          'resolve': res, 'resolve_ips': resolve_detail(),
                          'flclash': dict(zip(('process', 'port7890'), flclash_status()))},
                         ensure_ascii=False, indent=1))
        sys.exit(0)
    if a == '--diagnose':
        import time as _t
        rep = ['CodexNetSwitch %s diagnose %s' % (VERSION, _t.strftime('%Y-%m-%d %H:%M:%S'))]
        lines = read_hosts()
        present, sg = block_state(lines) if lines else (None, None)
        rep.append('hosts_read=%s block=%s statsig_blocked=%s' % ('ok' if lines is not None else 'FAIL', present, sg))
        rep.append('flclash process=%s port7890=%s' % flclash_status())
        rep.append('deepseek_api=%s' % deepseek_status())
        for d, (s, ips) in resolve_all().items():
            rep.append('%-8s %-34s %s' % (s, d, ips))
        try:
            rep.append('backups: ' + ', '.join(sorted(
                f for f in os.listdir(os.path.dirname(HOSTS)) if 'codexnet' in f or 'bak-2026' in f)))
        except OSError:
            pass
        print('\n'.join(rep))
        sys.exit(0)
    if a == '--selftest':
        tests = []
        t_, p_ = elevated_params('--apply')
        if getattr(sys, 'frozen', False):
            ok_ep = (t_ == sys.executable and p_ == '--apply')
        else:
            ok_ep = p_.startswith('"') and p_.endswith('" --apply')
        tests.append(('elevated params mode-aware', ok_ep))
        b = build_block(True); tests.append(('block has markers', b[0] == BEGIN and b[-1] == END))
        b2 = build_block(False)
        sg = [l for l in b2 if 'statsigapi.net' in l]
        tests.append(('statsig off comments only statsig lines',
                      all(l.startswith('# ') for l in sg) and len(sg) == 2))
        host_like = ['# header', BEGIN, '127.0.0.1 auth.openai.com', '::1 auth.openai.com',
                     '# 127.0.0.1 statsigapi.net  # [statsig] 放行中', END, '1.2.3.4 other.com']
        rest, old, broken = strip_managed(host_like)
        tests.append(('strip keeps outside lines', rest == ['# header', '1.2.3.4 other.com']))
        tests.append(('strip no-old-marker', old is False))
        tests.append(('strip healthy not broken', broken is False))
        old_like = ['# x', '# codex sentry block 2026-10-25 - delete', '127.0.0.1 o33249.ingest.sentry.io',
                    '# codex offshore block 2026-10-25 (C-level)', '127.0.0.1 auth.openai.com', '::1 auth.openai.com',
                    '2.3.4.5 mine.com']
        rest2, old2, _ = strip_managed(old_like)
        tests.append(('strip old blocks keeps others', rest2 == ['# x', '2.3.4.5 mine.com'] and old2 is True))
        broken_like = ['# header', BEGIN, '127.0.0.1 auth.openai.com', '1.2.3.4 user-own-line.com']
        rest3, _, broken3 = strip_managed(broken_like)
        tests.append(('broken block detected (BEGIN no END)', broken3 is True))
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
            write_block('apply'); write_block('apply')
            t4 = ('fake-hosts duplicate blocks collapse to one',
                  open(fake, encoding='ascii').read().count(BEGIN) == 1)
            broken_content = '# header\n' + BEGIN + '\n127.0.0.1 x\n1.2.3.4 keep.me\n'
            open(fake, 'w', encoding='ascii').write(broken_content)
            okb, _msgb = write_block('apply')
            t5 = ('fake-hosts broken block refuses write & keeps file',
                  okb is False and open(fake, encoding='ascii').read() == broken_content)
            open(fake, 'w', encoding='utf-8', newline='').write('\ufeff# bomline\n1.2.3.4 keep.me\n')
            write_block('apply'); write_block('remove')
            t6 = ('fake-hosts BOM first-line repaired',
                  open(fake, 'rb').read().startswith(b'# bomline'))
            for n, ok in (t1, t2, t3, t4, t5, t6):
                tests.append((n, ok)); print(('PASS' if ok else 'FAIL'), n)
                if not ok: bad.append(n)
        finally:
            globals()['HOSTS'] = orig_hosts
            if fake:
                import glob as _g
                for junk in _g.glob(os.path.join(os.path.dirname(fake), 'hosts.bak-codexnet.*')):
                    try:
                        os.remove(junk)
                    except OSError:
                        pass
                try:
                    os.remove(fake)
                except OSError:
                    pass
        sys.exit(1 if bad else 0)
    sys.exit(0)

# ---------------- GUI ----------------
import tkinter as tk
import tkinter.messagebox

LIGHT = dict(bg='#f2f4f7', card='#ffffff', line='#e2e6ec',
             ink='#1c2333', sub='#71798a',
             blue='#2563eb', blue_d='#1e4fc4', blue_bg='#dbeafe',
             green='#15803d', green_bg='#dcfce7',
             red='#dc2626', red_bg='#fee2e2',
             yellow='#b45309', yellow_bg='#fef3c7', gray_bg='#e8eaef',
             row_hover='#f6f9ff', help_bg='#eef1f5')
DARK = dict(bg='#151922', card='#1e2430', line='#2c3442',
            ink='#e6e9f0', sub='#8a93a5',
            blue='#3b82f6', blue_d='#2563eb', blue_bg='#1e3a5f',
            green='#4ade80', green_bg='#14532d',
            red='#f87171', red_bg='#5f1e1e',
            yellow='#fbbf24', yellow_bg='#4a3812', gray_bg='#2c3442',
            row_hover='#273043', help_bg='#171c26')
C = dict(LIGHT)

CONF_DIR = os.path.join(os.environ.get('APPDATA', '.'), 'CodexNetSwitch')

def load_theme():
    try:
        with open(os.path.join(CONF_DIR, 'config.json'), encoding='utf-8') as f:
            return json.load(f).get('theme', 'light')
    except Exception:
        return 'light'

def save_theme(t):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(os.path.join(CONF_DIR, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'theme': t}, f)
    except OSError:
        pass

FONT, FONT_M, FONT_B = 'Microsoft YaHei UI', ('Microsoft YaHei UI', 9), ('Microsoft YaHei UI', 11, 'bold')

def apply_titlebar_dark(root, dark):
    """Windows 10/11 深色标题栏（随主题）"""
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass

class Tooltip:
    """悬停气泡注释：鼠标放上去 0.5 秒弹出说明，不占界面版面"""
    def __init__(self, widget, text, delay=500):
        self.w, self.text, self.delay = widget, text, delay
        self.id = None
        self.tip = None
        widget.bind('<Enter>', self._enter, add='+')
        widget.bind('<Leave>', self._leave, add='+')
        widget.bind('<ButtonPress>', self._leave, add='+')

    def _enter(self, _=None):
        self.id = self.w.after(self.delay, self._show)

    def _leave(self, _=None):
        if self.id:
            self.w.after_cancel(self.id)
            self.id = None
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.w.winfo_rootx() + 12
        y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f'+{x}+{y}')
        tk.Label(self.tip, text=self.text, font=FONT_M, justify='left',
                 bg='#1c2333', fg='#e6e9f0', padx=10, pady=6).pack()

class App:
    CARD_TIP = {
        'block': '总开关。\n开：codex 连下面那些境外地址时会立刻失败，不再干等几十秒——\n启动变快，聊天功能不受影响。\n关：恢复 Windows 原样（想用 OpenAI 官方账号前要先关）。',
        'fl': 'FlClash = 你的翻墙代理软件。\n开着时 codex 的流量全走它分流，一切正常；\n没开时才需要靠本工具的屏蔽来避免卡等。',
        'port': 'FlClash 的代理端口号（7890）。\n"可用" = 代理软件正常工作中。',
        'ds': '你的 AI 对话接口（deepseek，国内服务器）。\n它显示"正常"就代表聊天/写代码功能没问题。',
    }
    DOM_TIP = {
        'auth.openai.com': 'OpenAI 检查"你登录了没有"的服务器。\n你用 deepseek 钥匙，不需要登录 OpenAI，\n所以连不上它毫发无损。',
        'api.openai.com': 'OpenAI 官方接口地址。\n你的对话已改走 deepseek，这个用不上；\n但 codex 启动时还是会去问它要插件列表，白等。',
        'statsigapi.net': '公用开关服务：告诉应用"哪些实验功能要打开"。\n⚠ 它不是 OpenAI 独有——你电脑上其他软件也可能用。\n哪个软件联网出问题，就点上方黄色按钮单独放行它。',
        'statsigcdn.openai.com': '上一条对应的 OpenAI 专用资源下载地址。',
        'ab.chatgpt.com': 'A/B 测试服务器：把用户分组试不同界面。\n对正常使用毫无影响。',
        'codex-portals.api.openai.com': 'codex 连 OpenAI 云端功能的专用通道。\n你不用官方云端功能，连不上无所谓。',
        'o33249.ingest.us.sentry.io': '报错回传服务：程序出错时把错误发回给 OpenAI。\n发不出去也完全不影响你使用，只是官方收不到日志。',
        'o33249.ingest.sentry.io': '报错回传服务（备用地址），同上。',
    }

    def __init__(self, root):
        self.root = root
        root.title(f'Codex 网络开关 v{VERSION}')
        root.configure(bg=C['bg'])
        try:
            root.iconbitmap(ICON)
        except Exception:
            pass
        root.resizable(False, False)
        self.rows, self._closed = {}, False
        self._build()
        self.refresh_async()
        self._watch_files()

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
                        padx=9, pady=2, width=8)

    def _build(self):
        self.body = tk.Frame(self.root, bg=C['bg'])
        self.body.pack(fill='both', expand=True)
        apply_titlebar_dark(self.root, load_theme() == 'dark')
        # 标题行
        head = tk.Frame(self.body, bg=C['bg']); head.pack(fill='x', padx=16, pady=(14, 8))
        tk.Label(head, text='Codex 网络开关', font=(FONT, 15, 'bold'),
                 fg=C['ink'], bg=C['bg']).pack(side='left')
        tk.Button(head, text='主题', font=FONT_M, relief='flat', cursor='hand2',
                  bg=C['gray_bg'], fg=C['ink'], activebackground=C['line'],
                  padx=10, pady=2, command=self.toggle_theme).pack(side='right', padx=(6, 0))
        tk.Button(head, text='刷新', font=FONT_M, relief='flat', cursor='hand2',
                  bg=C['gray_bg'], fg=C['ink'], activebackground=C['line'],
                  padx=12, pady=2, command=lambda: self.refresh_async(manual=True)).pack(side='right')

        # ── 总评区（红绿灯）：一句话告诉你现在能不能正常用 ──
        self.hero = self.card(self.body, fill='x', padx=16, pady=(0, 8))
        self.hero_state = tk.Label(self.hero, text='正在检查…', font=(FONT, 17, 'bold'),
                                   fg=C['sub'], bg=C['card'], anchor='w', justify='left')
        self.hero_state.pack(fill='x', padx=16, pady=(12, 2))
        self.hero_sub = tk.Label(self.hero, text='', font=(FONT, 11),
                                 fg=C['ink'], bg=C['card'], anchor='w', justify='left',
                                 wraplength=640)
        self.hero_sub.pack(fill='x', padx=16, pady=(0, 4))
        self.hero_why = tk.Label(self.hero, text='', font=FONT_M,
                                 fg=C['sub'], bg=C['card'], anchor='w')
        self.hero_why.pack(fill='x', padx=16, pady=(0, 12))

        # ── 详情折叠区（默认收起）：状态卡 + 域名清单 ──
        self.fold_detail = tk.Label(self.body, text='▸  技术详情（状态卡 / 域名屏蔽清单）',
                                    font=FONT_M, fg=C['blue'], bg=C['bg'],
                                    cursor='hand2', anchor='w')
        self.fold_detail.pack(fill='x', padx=16, pady=(6, 2))
        self.fold_detail.bind('<Button-1>', lambda e: self.toggle_detail())
        self.detail = tk.Frame(self.body, bg=C['bg'])

        # 四张状态卡（在详情区内）
        cards = tk.Frame(self.detail, bg=C['bg']); cards.pack(fill='x', padx=16, pady=(0, 10))
        self.card_stats = []
        for key, title in (('block', '屏蔽状态'), ('fl', 'FlClash'), ('port', '代理端口 7890'),
                           ('ds', '对话 API (deepseek)')):
            f = self.card(self.detail, side='left', padx=(0, 10), fill='both', expand=True)
            tk.Label(f, text=title, font=FONT_M, fg=C['sub'], bg=C['card']).pack(anchor='w', padx=14, pady=(10, 0))
            v = tk.Label(f, text='…', font=(FONT, 13, 'bold'), fg=C['sub'], bg=C['card'])
            v.pack(anchor='w', padx=14, pady=(2, 0))
            sub = tk.Label(f, text='', font=FONT_M, fg=C['sub'], bg=C['card'],
                           wraplength=145, justify='left')
            sub.pack(anchor='w', padx=14, pady=(1, 10))
            Tooltip(f, self.CARD_TIP[key])
            self.card_stats.append((v, sub))

        # 域名列表卡（在详情区内）
        lst_wrap = self.card(self.detail, fill='both', expand=True, padx=16, pady=(0, 10))
        lh = tk.Frame(lst_wrap, bg=C['card']); lh.pack(fill='x', padx=14, pady=(10, 2))
        for t, cw, a in (('域名', 250, 'w'), ('状态', 80, 'center'), ('用途', 300, 'w')):
            tk.Label(lh, text=t, font=FONT_M, fg=C['sub'], bg=C['card'], width=cw // 8, anchor=a).pack(side='left')
        tk.Frame(lst_wrap, bg=C['line'], height=1).pack(fill='x', padx=14)
        for gname, gdoms in GROUPS:
            glabel = tk.Label(lst_wrap, text='  ' + gname, font=(FONT, 9, 'bold'),
                              fg=C['sub'], bg=C['card'], anchor='w')
            glabel.pack(fill='x', padx=8, pady=(6, 1))
            for dom in gdoms:
                row = tk.Frame(lst_wrap, bg=C['card']); row.pack(fill='x', padx=6)
                tk.Label(row, text=dom, font=('Consolas', 9), fg=C['ink'], bg=C['card'],
                         width=31, anchor='w').pack(side='left')
                st = self.badge(row, '…', C['sub'], C['gray_bg'])
                st.pack(side='left', padx=(0, 4))
                tk.Label(row, text=USE[dom], font=FONT_M, fg=C['sub'], bg=C['card'],
                         width=40, anchor='w').pack(side='left')
                row.bind('<Enter>', lambda e, r=row: r.config(bg=C['row_hover']))
                row.bind('<Leave>', lambda e, r=row: r.config(bg=C['card']))
                Tooltip(row, self.DOM_TIP[dom])
                self.rows[dom] = st

        # 操作区
        ops = tk.Frame(self.body, bg=C['bg']); ops.pack(fill='x', padx=16, pady=(0, 6))
        self.ops_anchor = ops
        self.btn_on = tk.Button(ops, text='启用屏蔽（日常用这个）', font=(FONT, 10, 'bold'), relief='flat',
                                cursor='hand2', padx=18, pady=7,
                                command=lambda: self.act('--apply'))
        self.btn_on.pack(side='left')
        Tooltip(self.btn_on, '打开总开关：让 codex 的境外请求"立刻失败"而不是干等\n几十秒超时。适合平时用 deepseek 干活的状态。')
        self.btn_off = tk.Button(ops, text='停用屏蔽（用官方账号前点这个）', font=(FONT, 10, 'bold'), relief='flat',
                                 cursor='hand2', padx=18, pady=7,
                                 command=lambda: self.act('--remove'))
        self.btn_off.pack(side='left', padx=(10, 0))
        Tooltip(self.btn_off, '关掉总开关，恢复 Windows 原样。\n想登录 OpenAI 官方账号 / 用云端功能前，先点这里停用。')
        self.btn_sg = tk.Button(ops, text='防误伤：放行 statsig', font=(FONT, 10, 'bold'), relief='flat',
                                cursor='hand2', padx=14, pady=7,
                                bg=C['yellow_bg'], fg=C['yellow'],
                                activebackground=C['yellow_bg'], command=self.toggle_statsig)
        self.btn_sg.pack(side='left', padx=(10, 0))
        self.btn_sg._normal_bg = C['yellow_bg']
        Tooltip(self.btn_sg, 'statsigapi.net 是公用地址（见列表第二组），\n别的软件可能也在用。哪个软件联网异常就点这里\n把它单独放行，其余屏蔽保持不变。')
        for b in (self.btn_on, self.btn_off, self.btn_sg):
            self._bind_hover(b)

        # 工具行：开机自启 + 导出诊断
        tools = tk.Frame(self.body, bg=C['bg']); tools.pack(fill='x', padx=16, pady=(2, 0))
        self.auto_var = tk.BooleanVar(value=autostart_get())
        _cb = tk.Checkbutton(tools, text='开机自启（最小化）', variable=self.auto_var,
                             font=FONT_M, fg=C['ink'], bg=C['bg'], activebackground=C['bg'],
                             selectcolor=C['card'], cursor='hand2',
                             command=self.toggle_autostart)
        _cb.pack(side='left')
        Tooltip(_cb, '开机时自动启动本工具，最小化待在任务栏，\n不弹窗打扰。')
        _dg = tk.Button(tools, text='导出诊断', font=FONT_M, relief='flat', cursor='hand2',
                        bg=C['gray_bg'], fg=C['ink'], activebackground=C['line'],
                        padx=10, pady=2, command=self.copy_diagnose)
        _dg.pack(side='right')
        Tooltip(_dg, '把当前各项状态整理成一段文字并复制到剪贴板，\n方便发给别人（或发给 AI）帮你排查问题。')

        # 底部状态条 + 可折叠说明
        self.tip = tk.Label(self.body, text='', font=FONT_M, fg=C['sub'], bg=C['bg'], anchor='w')
        self.tip.pack(fill='x', padx=16)
        self.fold_lbl = tk.Label(self.body, text='▸  影响说明', font=FONT_M, fg=C['blue'],
                                 bg=C['bg'], cursor='hand2', anchor='w')
        self.fold_lbl.pack(fill='x', padx=16, pady=(6, 0))
        self.fold_lbl.bind('<Button-1>', lambda e: self.toggle_help())
        self.help = tk.Text(self.body, height=10, font=FONT_M, wrap='word', relief='flat',
                            bg=C['help_bg'], fg=C['ink'], padx=12, pady=8, spacing2=3)
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
            '· 回滚兜底：etc 目录下有 hosts.bak-20261025-sentry / -cblock 两份历史备份。\n'
            '\n'
            '【名词速查】\n'
            '· hosts：Windows 里"域名 → IP 地址"的本地对照表。本工具就是往里写条目，\n'
            '  让某些域名指向你自己电脑（127.0.0.1），从而"连不上"。\n'
            '· 屏蔽 = 让请求立刻被拒绝（快），代替傻等几十秒超时（慢）。反正这些请求\n'
            '  本来就是失败的，屏蔽只是让失败来得快一点。\n'
            '· UAC：弹出"是否允许更改"的管理员确认框——修改 hosts 属于系统文件，必须经它。\n'
            '· 劫持到 127.0.0.1：127.0.0.1 永远指你自己这台电脑，连它=自己连自己=秒失败。')
        self.help.config(state='disabled')
        self.foot = tk.Label(self.body, text=f'v{VERSION} · github.com/ygy2200/codex-net-switch · 点击复制链接',
                             font=FONT_M, fg=C['blue'], bg=C['bg'], cursor='hand2')
        self.foot.pack(side='bottom', fill='x', padx=16, pady=(0, 6))
        self.foot.bind('<Button-1>', lambda e: self._copy_repo())

    def toggle_theme(self):
        new = 'dark' if load_theme() == 'light' else 'light'
        save_theme(new)
        C.clear()
        C.update(DARK if new == 'dark' else LIGHT)
        self.rows.clear()
        self.body.destroy()
        self._build()
        self._resize_center()
        self.refresh_async()

    def _copy_repo(self):
        self.root.clipboard_clear()
        self.root.clipboard_append('https://github.com/ygy2200/codex-net-switch')
        self.tip.config(text='✓ 仓库链接已复制', fg=C['green'])

    def toggle_detail(self):
        if self.detail.winfo_ismapped():
            self.detail.pack_forget(); self.fold_detail.config(text='▸  技术详情（状态卡 / 域名屏蔽清单）')
            self._resize_center()
        else:
            self.detail.pack(fill='both', expand=True, before=self.ops_anchor)
            self.fold_detail.config(text='▼  技术详情（状态卡 / 域名屏蔽清单）')
            self._resize_center()

    def toggle_help(self):
        if self.help.winfo_ismapped():
            self.help.pack_forget(); self.fold_lbl.config(text='▸  影响说明')
        else:
            self.help.pack(fill='x', padx=16, pady=(4, 10))
            self.fold_lbl.config(text='▼  影响说明')
        self._resize_center()  # 窗口定高，展开/收起必须重算尺寸，否则 help 挤不进/收不回

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
        try:
            lines = read_hosts()
            present, sg = block_state(lines) if lines else (None, None)
            run_, alive = flclash_status()
            data = (present, sg, run_, alive, deepseek_status(), resolve_status())
        except Exception as e:
            err = str(e)[:80]
            try:
                self.root.after(0, lambda: self.tip.config(
                    text=f'刷新出错：{err}', fg=C['red']))
            except (tk.TclError, RuntimeError):
                pass
            return
        try:
            self.root.after(0, lambda: self._apply(data))
        except (tk.TclError, RuntimeError):
            pass  # 窗口销毁瞬间 / 非 mainloop 状态：丢弃本次回调

    VERDICT_FG = {'green': 'green', 'yellow': 'yellow', 'red': 'red', 'gray': 'sub'}

    def _set_verdict(self, level, title, sub):
        self.hero_state.config(text=title, fg=C[self.VERDICT_FG[level]])
        self.hero_sub.config(text=sub)

    def _apply(self, data):
        if self._closed:
            return
        present, sg, run_, alive, ds_ok, res = data
        # ── 红绿灯总评（优先级：对话接口 > hosts 可读 > 屏蔽状态）──
        if ds_ok is False:
            self._set_verdict('red', '⚠ 对话接口连不上',
                              '聊天会失败。先检查网络或 deepseek 服务状态，然后点右上角「刷新」。')
        elif present is None:
            self._set_verdict('red', '⚠ 无法读取 hosts',
                              '权限异常。右键本工具，选"以管理员身份运行"再试。')
        elif present is True:
            self._set_verdict('green', '✅ 一切正常', 'codex 可以正常、快速地使用——这就是日常该有的样子。')
        elif run_:
            self._set_verdict('green', '✅ 正常（走代理）',
                              '屏蔽关着，但 FlClash 在跑，codex 走代理不受影响。注意别关 FlClash。')
        else:
            self._set_verdict('yellow', '⚠ codex 启动会慢',
                              '屏蔽和 FlClash 都没开。平时建议点下方蓝色按钮「启用屏蔽」。')
        self.hero_why.config(text='明细：屏蔽 {} · FlClash {} · 代理端口 {} · 对话接口 {}'.format(
            '开' if present else ('?' if present is None else '关'),
            '运行中' if run_ else '没开',
            '通' if alive else '不通',
            '正常' if ds_ok else '不通'))
        b, fl, pt, ds = self.card_stats  # 每项 = (状态大字, 结论小字)
        if present is True:
            b[0].config(text='● 已开启', fg=C['green'])
            b[1].config(text='codex 启动不卡等\n（推荐的日常状态）')
        elif present is False:
            b[0].config(text='○ 已停用', fg=C['sub'])
            b[1].config(text='恢复原样；FlClash 没开时\n启动会变慢')
        else:
            b[0].config(text='无法读取', fg=C['red'])
            b[1].config(text='hosts 读不到，权限异常')
        warn_fl = (not run_) and (present is False)
        if run_:
            fl[0].config(text='运行中', fg=C['green'])
            fl[1].config(text='代理软件正常，\ncodex 走它分流')
        elif warn_fl:
            fl[0].config(text='未运行', fg=C['yellow'])
            fl[1].config(text='⚠ 屏蔽也停着：\ncodex 启动会慢')
        else:
            fl[0].config(text='未运行', fg=C['sub'])
            fl[1].config(text='没关系，屏蔽已兜底')
        if alive:
            pt[0].config(text='正常', fg=C['green'])
            pt[1].config(text='FlClash 工作中')
        else:
            pt[0].config(text='不通', fg=C['red'])
            pt[1].config(text='FlClash 没开或异常\n（不影响屏蔽功能）')
        if ds_ok:
            ds[0].config(text='正常', fg=C['green'])
            ds[1].config(text='可以正常对话干活')
        else:
            ds[0].config(text='不可达', fg=C['red'])
            ds[1].config(text='⚠ 聊天会失败，\n先查网络再干活')
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
            text=('恢复 statsig 屏蔽' if sg is False else '防误伤：放行 statsig') if sg_ok
                 else '防误伤（需先启用屏蔽）')
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
        if arg == '--remove' and not tk.messagebox.askyesno(
                '确认停用', '停用后 codex 将恢复直连境外：\n\n· FlClash 开着 → 走代理，一切正常\n· FlClash 关着 → 启动会变慢（回到屏蔽前状态）\n\n确定停用屏蔽吗？'):
            return
        if not run_elevated(arg):
            self.tip.config(text='✕ 未能启动管理员进程（UAC 被取消）', fg=C['red'])
            return
        self.tip.config(text='⏳ 已发起，等待 UAC 确认后自动刷新…', fg=C['blue'])
        threading.Thread(target=self._watch_hosts, daemon=True).start()

    def _watch_files(self):
        """常态监听 hosts 内容变化（外部手改也即时刷新）"""
        if self._closed:
            return
        sig = self._hosts_sig()
        if getattr(self, '_last_sig', None) is not None and sig != self._last_sig:
            self.refresh_async()
        self._last_sig = sig
        self.root.after(2000, self._watch_files)

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
        except (tk.TclError, RuntimeError):
            pass

    @staticmethod
    def _hosts_sig():
        try:
            with open(HOSTS, 'rb') as f:
                return hash(f.read())
        except OSError:
            return None

    def toggle_autostart(self):
        on = self.auto_var.get()
        if autostart_set(on):
            self.tip.config(text=f'✓ 开机自启已{"开启" if on else "关闭"}', fg=C['green'])
        else:
            self.auto_var.set(not on)
            self.tip.config(text='✕ 自启设置失败（注册表写入被拒）', fg=C['red'])

    def copy_diagnose(self):
        try:
            r = subprocess.run(self_cmd_args('--diagnose'), capture_output=True,
                               timeout=30, encoding='utf-8', errors='replace')
            self.root.clipboard_clear()
            self.root.clipboard_append(r.stdout)
            self.tip.config(text='✓ 诊断报告已复制到剪贴板', fg=C['green'])
        except Exception as e:
            self.tip.config(text=f'✕ 诊断失败：{str(e)[:60]}', fg=C['red'])

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
    if load_theme() == 'dark':
        C.update(DARK)
    app = App(root)
    app._resize_center()
    if '--minimize' in sys.argv:
        root.iconify()
    root.protocol('WM_DELETE_WINDOW', lambda: (setattr(app, '_closed', True), root.destroy()))
    root.mainloop()
