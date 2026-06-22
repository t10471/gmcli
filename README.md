# gmcli

個人 Gmail を CLI で読む / 送るツール。IMAP で受信箱を読み（既読にしない・ローカル完結）、SMTP で送信する。
旧 `gmread`（読むだけ）の後継。

## セットアップ

1. アプリパスワードを用意する（2段階認証を有効にして Google アカウントで発行）。
2. 認証情報を `.env` に置く:

   ```sh
   cp .env.example .env && chmod 600 .env
   # .env を編集して GMAIL_USER / GMAIL_APP_PASSWORD を入れる
   ```

3. python は [mise](https://mise.jdx.dev/) が `mise.toml` の指定（3.13）で解決する。
4. `~/bin/gmcli` のラッパー経由で実行する（このリポジトリに cd して `.env` を読み込み、python を呼ぶ）。

## 使い方

```sh
gmcli read [件数] [from=...] [subject=...] [keyword=...] [minutes=N] [codes]
gmcli send --to a@b.com -s 件名 -b 本文 [...]
```

`read` は省略できる（`gmcli 5 from=...` も read として動く）。

### 読む（read）

フィルターは複数指定すると AND、カンマ区切りは OR（大文字小文字を無視・日本語可）。

| 指定 | 意味 |
| --- | --- |
| `from=noreply,security` | 差出人にいずれかを含む |
| `subject=コード,認証` | 件名にいずれかを含む |
| `keyword=確認,verification` | 件名 or 本文にいずれかを含む |
| `minutes=30` | 直近30分以内のメールだけ |
| `codes` | 4〜8桁の数字を含むメールだけ |
| 先頭の数字 | 表示件数（既定10） |

例:

```sh
gmcli                       # 直近10件
gmcli 5 codes               # コードを含む直近のメール5件、数字をハイライト
gmcli from=github minutes=60
```

### 送る（send）

| オプション | 意味 |
| --- | --- |
| `--to/-t` | 宛先（カンマ区切り / 複数指定可）※必須 |
| `--cc` `--bcc` | CC / BCC |
| `--subject/-s` | 件名 |
| `--body/-b` | 本文（省略時は `--body-file` か標準入力） |
| `--body-file/-F` | 本文をファイルから読む |
| `--html` | 本文を HTML として送る |
| `--attach/-a` | 添付ファイル（複数可） |
| `--from` | 差出人（既定 `GMAIL_USER`） |
| `--reply-to` | 返信先 |
| `--yes/-y` | 確認なしで送信 |
| `--dry-run` | 送信せずプレビューだけ表示 |

送信前に内容のプレビューを出し、`y` で確認する（`--yes` で省略）。

例:

```sh
gmcli send -t a@example.com -s こんにちは -b "本文です"
echo "本文を標準入力から" | gmcli send -t a@example.com -s 件名
gmcli send -t a@example.com -s レポート -F body.txt -a report.pdf
gmcli send -t a@example.com -s 確認 -b test --dry-run
```

## 構成

```
gmcli.py        本体（read / send サブコマンド）
mise.toml       python のバージョン指定
.env            認証情報（gitignore・各自で用意）
.env.example    .env の雛形
```
