# アップグレードと復旧

Ubuntu ホストの、普段このプロジェクトを動かしているユーザーで実行します。ここでは GitHub の `main` と既定の Docker イメージタグを使用します。既存 VM の状態を継続するため、同じディレクトリで更新します。

## 1. 更新内容を確認して停止する

```sh
git status --short
git fetch origin main
git log --oneline HEAD..origin/main
```

ローカル変更があれば内容を確認して、別コミットなどに保存します。VM 管理画面で実行中のファイル転送を完了させ、稼働している VM を通常停止します。VM 一覧ですべて停止したことを確認してから、Web と、LAN SSE を設定している場合はその MCP サービスも停止します。

```sh
systemctl --user stop windows-html-console.service
# LAN SSE サービスを利用している環境だけ
systemctl --user stop windows-console-mcp-sse.service
```

## 2. バックアップする

既定の保存先を使用している場合の例です。バックアップ先には VM ディスクと転送済みファイルを保存できる空き容量を確保します。環境変数で保存場所を変更している場合は、その実体をバックアップに含めます。

```sh
umask 077
backup=$(mktemp -d "$HOME/windows-console-upgrade.XXXXXXXX")
before=$(git rev-parse HEAD)
image="windows-console-mcp:rollback-$(git rev-parse --short=12 HEAD)"
printf '%s\n' "$before" > "$backup/revision"
printf '%s\n' "$image" > "$backup/image"
docker image tag windows-console-mcp:local "$image"

for dir in state-managed state-console state-clean; do
  if [ -d "$dir" ]; then
    tar --sparse -cpf "$backup/$dir.tar" -- "$dir"
  fi
done
printf 'バックアップ先 %s\n' "$backup"
```

`state-managed` は VM 一覧と VM ごとのディスク・TPM・転送データ、`state-console` は Web のポート設定、`state-clean` は移行した旧 VM の状態です。ISO と systemd の個別設定、LAN SSE の証明書・認証ファイルを別の場所に置いている場合は、その保存場所も控えます。バックアップにはゲストデータや認証情報が含まれることがあるため、ユーザーが管理する保存先を使います。

## 3. コードとイメージを更新する

```sh
git merge --ff-only origin/main
docker build --pull -t windows-console-mcp:local .
uv sync --locked --project console/mcp
```

標準の systemd ユニットをリポジトリからインストールしている環境では、ユニットに変更がある場合に再インストールし、`systemctl --user daemon-reload` を実行します。独自ユニットやドロップインは更新差分を確認します。

```sh
systemctl --user start windows-html-console.service
# LAN SSE サービスを利用している環境だけ
systemctl --user start windows-console-mcp-sse.service
systemctl --user status windows-html-console.service
```

1. いつもの Web URL を開き、テンプレート、VM、トポロジーが表示されることを確認します。
2. 停止した VM を1台起動してコンソールを開き、以前の仮想ディスクから起動することを確認します。トポロジーを使う場合はルーターとプロキシの状態も確認します。
3. MCP クライアントを再接続し、`vm_inventory` を呼び出します。stdio は再接続時に更新済みプロセスを起動します。

管理下の VM コンテナは次回の「起動」で新しいイメージから作られます。ルーターと透過プロキシは、接続中の VM がすべて停止した状態で次の VM を起動する際、イメージの違いを検出して再作成します。旧構成から登録した VM は既存の Docker コンテナを使うため、同じイメージタグを付け替えてもその VM コンテナの実体は変わりません。

## 4. 更新前に戻す

更新後に VM を起動した場合は再度通常停止し、Web と LAN SSE を停止します。`backup` が別端末に引き継がれていない場合は、最初に手順2で表示されたバックアップの絶対パスを設定してください。

```sh
uv run --locked --project console/mcp python console/maintenance.py stop-network-containers
git switch --detach "$(cat "$backup/revision")"
docker image tag "$(cat "$backup/image")" windows-console-mcp:local
uv sync --locked --project console/mcp
systemctl --user start windows-html-console.service
# LAN SSE サービスを利用している環境だけ
systemctl --user start windows-console-mcp-sse.service
```

停止したルーターとプロキシは次回の VM 起動時に戻したイメージから作られます。VM 一覧とコンソールを再確認します。コードとイメージを戻しただけなら、更新後にゲスト内で行った作業もディスクに残ります。復旧後に追跡ブランチへ戻るときは `git switch main` を使用します。

### 状態ファイルも復元する場合

バックアップ後のゲスト内変更が巻き戻ります。VM とサービスを停止してから、既定の保存先で次を実行します。現在の状態は `retired` に移すので、復元をやり直せます。

```sh
retired=$(mktemp -d "$HOME/windows-console-before-restore.XXXXXXXX")
for dir in state-managed state-console state-clean; do
  if [ -f "$backup/$dir.tar" ]; then
    if [ -e "$dir" ]; then mv -- "$dir" "$retired/"; fi
    tar -xpf "$backup/$dir.tar"
  fi
done
printf '復元前の退避先 %s\n' "$retired"
systemctl --user start windows-html-console.service
```

カスタムの保存先を使う環境では、退避と復元のパスをその保存先に合わせます。Unix ソケットはサービスや VM が起動時に作り直します。
