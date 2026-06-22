#!/usr/bin/env python3
"""
個人 Gmail を CLI で読む / 送る（ローカル完結）。

サブコマンド:
  gmcli read [件数] [from=...] [subject=...] [keyword=...] [minutes=N] [codes]
  gmcli send --to a@b.com [--cc ...] [--bcc ...] -s 件名 -b 本文 [...]

read を省略しても read として動く（gmcli 5 from=... など）。

準備:
  export GMAIL_USER='you@gmail.com'
  export GMAIL_APP_PASSWORD='アプリパスワード16桁（スペース無し）'

read のフィルター（複数指定は AND。カンマ区切りは OR・大文字小文字無視・日本語OK）:
  from=noreply,security      差出人にいずれかを含む
  subject=コード,認証         件名にいずれかを含む
  keyword=確認,verification   件名 or 本文 にいずれかを含む
  minutes=30                 直近30分以内のメールだけ
  codes                      4〜8桁の数字を含むメールだけ
  （先頭の数字）              表示件数（既定10）

send のオプション:
  --to/-t   宛先（カンマ区切り or 複数指定）        ※必須
  --cc/--bcc                                    CC / BCC
  --subject/-s 件名
  --body/-b 本文（省略時は --body-file か標準入力）
  --body-file/-F 本文をファイルから読む
  --html    本文を HTML として送る
  --attach/-a 添付ファイル（複数指定可）
  --from    差出人（既定 GMAIL_USER）
  --reply-to 返信先
  --yes/-y  確認プロンプトを出さずに送信
  --dry-run 送信せずプレビューだけ表示
"""
import os
import re
import sys
import ssl
import email
import shutil
import imaplib
import smtplib
import argparse
import mimetypes
from email.message import EmailMessage
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

IMAP_HOST, IMAP_PORT = 'imap.gmail.com', 993
SMTP_HOST, SMTP_PORT = 'smtp.gmail.com', 465
SCAN_CAP = 300
BODY_MAX_LINES = 10
BODY_MAX_CHARS = 500

# 色
DIM = '\033[90m'
CYAN = '\033[1;36m'
GREEN = '\033[1;32m'
CODEC = '\033[1;30;43m'   # 黒文字×黄背景
RESET = '\033[0m'

SEP_RE = re.compile(r'^[\s\-=_*~‐―ー─・･=＝—]{6,}$')
NUM_RE = re.compile(r'(?<!\d)\d{4,8}(?!\d)')
# 「コードらしい行」を判定するキーワード（年・日付・ID 等の誤検知を避けるため）
KW_RE = re.compile(
    r'(認証|確認コード|確認番号|認証番号|コード|ワンタイム|セキュリティコード|パスコード|'
    r'passcode|verification|security\s*code|one[\s-]?time|otp|your\s+code|\bcode\b)',
    re.I)
SEPCHARS_RE = re.compile(r'[\s\-‐―ー─・･:：=＝|｜()（）\[\]【】]+')


def need_credentials():
    user, pw = os.environ.get('GMAIL_USER'), os.environ.get('GMAIL_APP_PASSWORD')
    if not user or not pw:
        print('環境変数 GMAIL_USER と GMAIL_APP_PASSWORD を設定してください。', file=sys.stderr)
        sys.exit(1)
    return user, pw


# ---------------------------------------------------------------- read

def dh(s):
    if not s:
        return ''
    out = ''
    for text, enc in decode_header(s):
        out += text.decode(enc or 'utf-8', 'replace') if isinstance(text, bytes) else text
    return out.strip()


def get_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain' and 'attachment' not in str(part.get('Content-Disposition')):
                return (part.get_payload(decode=True) or b'').decode(part.get_content_charset() or 'utf-8', 'replace')
        return ''
    return (msg.get_payload(decode=True) or b'').decode(msg.get_content_charset() or 'utf-8', 'replace')


def wrap_codes(s):
    return NUM_RE.sub(lambda m: f'{CODEC} {m.group(0)} {RESET}', s)


def is_codeish_line(s):
    """記号・空白を除いた残りが 4〜8桁の数字グループだけ（最大16桁）= コードが並んだ行"""
    t = SEPCHARS_RE.sub('', s)
    return bool(t) and len(t) <= 16 and re.fullmatch(r'(?:\d{4,8})+', t) is not None


def hl_line(s, prev_kw):
    """行・直前行にコード系キーワードがある or コードらしい行のときだけ数字を強調"""
    cur_kw = bool(KW_RE.search(s))
    if cur_kw or prev_kw or is_codeish_line(s):
        return wrap_codes(s), cur_kw
    return s, cur_kw


def short_date(date_str):
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone().strftime('%-m/%d %H:%M') if dt else date_str
    except Exception:
        return date_str


def clean_body(body):
    out, blank = [], False
    for raw in body.splitlines():
        ln = raw.rstrip()
        s = ln.strip()
        if not s:
            if out and not blank:
                out.append('')
                blank = True
            continue
        if SEP_RE.match(s):
            continue
        out.append(ln)
        blank = False
    while out and out[-1] == '':
        out.pop()
    truncated = False
    if len(out) > BODY_MAX_LINES:
        out, truncated = out[:BODY_MAX_LINES], True
    text = '\n'.join(out)
    if len(text) > BODY_MAX_CHARS:
        text, truncated = text[:BODY_MAX_CHARS].rstrip(), True
    return (text.splitlines() if text else []), truncated


def contains_any(text, needles):
    t = text.lower()
    return any(n.lower() in t for n in needles)


def parse_read_args(argv):
    opt = {'count': 10, 'from': None, 'subject': None, 'keyword': None, 'minutes': None, 'codes': False}
    for a in argv:
        if a.isdigit():
            opt['count'] = int(a)
        elif a == 'codes':
            opt['codes'] = True
        elif '=' in a:
            k, v = a.split('=', 1)
            if k in ('from', 'subject', 'keyword'):
                opt[k] = [x for x in v.split(',') if x]
            elif k == 'minutes':
                opt['minutes'] = int(v)
    return opt


def age_ok(date_str, minutes):
    if minutes is None:
        return True
    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        return True
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(minutes=minutes)


def cmd_read(argv):
    user, pw = need_credentials()
    opt = parse_read_args(argv)

    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(user, pw)
    except imaplib.IMAP4.error as e:
        print('ログイン失敗:', e, file=sys.stderr)
        print('→ アプリパスワード/2段階認証を確認してください。', file=sys.stderr)
        sys.exit(2)

    imap.select('INBOX', readonly=True)
    ids = imap.search(None, 'ALL')[1][0].split()
    if not ids:
        print('メールがありません。')
        return

    width = max(40, min(shutil.get_terminal_size((90, 20)).columns, 90))
    shown = scanned = 0
    for i in reversed(ids):
        if shown >= opt['count'] or scanned >= SCAN_CAP:
            break
        scanned += 1

        hdr = email.message_from_bytes(imap.fetch(i, '(BODY.PEEK[HEADER])')[1][0][1])
        frm, subj, date = dh(hdr.get('From')), dh(hdr.get('Subject')), dh(hdr.get('Date'))

        if opt['minutes'] is not None and not age_ok(date, opt['minutes']):
            break
        if opt['from'] and not contains_any(frm, opt['from']):
            continue
        if opt['subject'] and not contains_any(subj, opt['subject']):
            continue

        body = get_body(email.message_from_bytes(imap.fetch(i, '(BODY.PEEK[])')[1][0][1]))
        if opt['keyword'] and not contains_any(subj + ' ' + body, opt['keyword']):
            continue
        if opt['codes'] and not re.search(r'(?<!\d)\d{4,8}(?!\d)', subj + ' ' + body):
            continue

        lines, truncated = clean_body(body)
        shown += 1

        subj_disp, prev_kw = hl_line(subj or '(件名なし)', False)
        print()
        print(f'{DIM}{"─" * width}{RESET}')
        print(f'{CYAN}[{shown}] {subj_disp}{RESET}')
        print(f'{DIM}     {frm}  ·  {short_date(date)}{RESET}')
        if lines:
            for bl in lines:
                if bl.strip() == '':
                    print(f'{DIM} │{RESET}')
                    continue
                disp, prev_kw = hl_line(bl, prev_kw)
                print(f'{DIM} │ {RESET}{disp}')
            if truncated:
                print(f'{DIM} │ …{RESET}')
        else:
            print(f'{DIM} │ (本文なし){RESET}')

    imap.logout()
    if shown == 0:
        print('（条件に合うメールはありませんでした）')


# ---------------------------------------------------------------- send

def split_addrs(values):
    """--to a@b,c@d --to e@f のような指定を 1 本のリストにまとめる"""
    out = []
    for v in values or []:
        out += [x.strip() for x in v.split(',') if x.strip()]
    return out


def confirm(prompt):
    """y/N を聞く。本文を標準入力から受けても確認できるよう /dev/tty を優先する。"""
    try:
        tty = open('/dev/tty')
    except OSError:
        return False
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        return tty.readline().strip().lower() in ('y', 'yes')
    finally:
        tty.close()


def build_message(args, sender):
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = ', '.join(args.to)
    if args.cc:
        msg['Cc'] = ', '.join(args.cc)
    if args.subject:
        msg['Subject'] = args.subject
    if args.reply_to:
        msg['Reply-To'] = args.reply_to

    if args.body is not None:
        body = args.body
    elif args.body_file:
        with open(args.body_file, encoding='utf-8') as f:
            body = f.read()
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        body = ''

    if args.html:
        msg.set_content('HTML メールです。テキスト版に対応したクライアントでご覧ください。')
        msg.add_alternative(body, subtype='html')
    else:
        msg.set_content(body)

    for path in args.attach or []:
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype.split('/', 1) if ctype else ('application', 'octet-stream'))
        with open(path, 'rb') as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))
    return msg


def cmd_send(argv):
    user, pw = need_credentials()
    p = argparse.ArgumentParser(prog='gmcli send', add_help=True,
                                description='個人 Gmail から SMTP で送信する')
    p.add_argument('--to', '-t', action='append', metavar='ADDR', help='宛先（カンマ区切り / 複数指定可）')
    p.add_argument('--cc', action='append', metavar='ADDR', help='CC')
    p.add_argument('--bcc', action='append', metavar='ADDR', help='BCC')
    p.add_argument('--subject', '-s', default='', help='件名')
    p.add_argument('--body', '-b', help='本文（省略時は --body-file か標準入力）')
    p.add_argument('--body-file', '-F', dest='body_file', metavar='FILE', help='本文をファイルから読む')
    p.add_argument('--html', action='store_true', help='本文を HTML として送る')
    p.add_argument('--attach', '-a', action='append', metavar='FILE', help='添付ファイル（複数可）')
    p.add_argument('--from', dest='sender', help='差出人（既定 GMAIL_USER）')
    p.add_argument('--reply-to', dest='reply_to', help='返信先')
    p.add_argument('--yes', '-y', action='store_true', help='確認なしで送信')
    p.add_argument('--dry-run', action='store_true', help='送信せずプレビューだけ表示')
    args = p.parse_args(argv)

    args.to = split_addrs(args.to)
    args.cc = split_addrs(args.cc)
    args.bcc = split_addrs(args.bcc)
    if not args.to and not args.cc and not args.bcc:
        p.error('宛先が必要です（--to / --cc / --bcc のいずれか）')

    sender = args.sender or user
    msg = build_message(args, sender)
    recipients = args.to + args.cc + args.bcc

    # プレビュー
    body_preview = msg.get_body(preferencelist=('plain', 'html'))
    text = body_preview.get_content() if body_preview else ''
    print(f'{DIM}{"─" * 50}{RESET}', file=sys.stderr)
    print(f'From   : {sender}', file=sys.stderr)
    print(f'To     : {", ".join(args.to) or "-"}', file=sys.stderr)
    if args.cc:
        print(f'Cc     : {", ".join(args.cc)}', file=sys.stderr)
    if args.bcc:
        print(f'Bcc    : {", ".join(args.bcc)}', file=sys.stderr)
    print(f'Subject: {args.subject or "(件名なし)"}', file=sys.stderr)
    if args.attach:
        print(f'Attach : {", ".join(os.path.basename(a) for a in args.attach)}', file=sys.stderr)
    print(f'{DIM}{"─" * 50}{RESET}', file=sys.stderr)
    preview = '\n'.join(text.splitlines()[:15])
    print(preview, file=sys.stderr)
    if len(text.splitlines()) > 15:
        print('…', file=sys.stderr)
    print(f'{DIM}{"─" * 50}{RESET}', file=sys.stderr)

    if args.dry_run:
        print('(dry-run: 送信しませんでした)', file=sys.stderr)
        return
    if not args.yes and not confirm('送信しますか? [y/N] '):
        print('中止しました。', file=sys.stderr)
        sys.exit(130)

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as smtp:
            smtp.login(user, pw)
            smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError as e:
        print('ログイン失敗:', e, file=sys.stderr)
        print('→ アプリパスワード/2段階認証を確認してください。', file=sys.stderr)
        sys.exit(2)
    except smtplib.SMTPException as e:
        print('送信失敗:', e, file=sys.stderr)
        sys.exit(3)
    print(f'{GREEN}✓ 送信しました{RESET} → {", ".join(recipients)}', file=sys.stderr)


# ---------------------------------------------------------------- entry

def main():
    argv = sys.argv[1:]
    if argv and argv[0] == 'send':
        return cmd_send(argv[1:])
    if argv and argv[0] == 'read':
        argv = argv[1:]
    return cmd_read(argv)


if __name__ == '__main__':
    main()
