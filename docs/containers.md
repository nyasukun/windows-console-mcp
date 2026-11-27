# Chrome・LinuxコンテナとトポロジーDNS

Chromeコンテナでは、ホスト側のChromeを開き、タブ、アドレス欄、マウスとキーボードを管理画面から操作できます。LinuxコンテナにはWeb SSH端末があります。どちらもWindows VMと同じトポロジーに接続でき、起動、停止、接続先の変更、削除をWebまたはMCPから操作できます。

## イメージを用意する

[インストール手順](install.md)でWindows Consoleが使える状態にしたあと、Ubuntu x86-64ホストのリポジトリ直下で実行します。Chromeのビルド時はGoogleの配布先へアクセスします。ChromeとLinuxの起動自体にKVMは不要です。

```sh
docker build -t windows-console-mcp:local .
docker build -f containers/Dockerfile --target browser -t windows-console-browser:local .
docker build -f containers/Dockerfile --target linux -t windows-console-linux:local .
uv sync --locked --project console/mcp
systemctl --user restart windows-html-console.service
# LAN SSE を使っている場合
systemctl --user restart windows-console-mcp-sse.service
```

イメージ名を変える場合は、WebとMCPの両方に `WINDOWS_BROWSER_IMAGE` または `WINDOWS_LINUX_IMAGE` を指定します。Linuxを初めて起動する際は、ホストの `ssh-keygen` でコンテナ固有の鍵を作ります。

既存トポロジーで初めてコンテナやDNSを使う場合は、接続中のVM・コンテナを停止してから、更新したイメージでネットワークを起動します。

## 最初のコンテナを作る

1. VM管理の「ネットワークトポロジー」でトポロジーを作り、重複しないプライベートIPv4のセグメントを登録します。外部Webサイトにもアクセスさせる場合は、既存の透過プロキシ設定を有効にします。
2. 「Chrome と Linux コンテナ」の「コンテナを作成」で名前と種類を選び、使用するトポロジーとセグメントを選びます。Windows VMの接続も同じトポロジーで設定します。
3. 「起動して開く」を押します。Chromeならアドレス欄にHTTPまたはHTTPSのURLを入力し、＋でタブを追加できます。LinuxならSSH端末でシェルを操作できます。
4. 停止後も、Linuxのファイルとインストール済みパッケージ、Chromeのプロファイルは保持されます。接続先を変更するには先に停止します。「削除」でそのコンテナのデータも消えます。

未接続のコンテナでもChrome画面とSSH端末は開けます。他の機器やインターネットへ通信させる場合はトポロジーに接続します。Chromeは専用のユーザープロファイルを持ち、画面を転送します。実行時はChrome内蔵のサンドボックスを無効にし、Docker側で非root、権限の削減、他のコンテナから分離したネットワーク名前空間を使用します。LinuxのSSHとChromeの制御ポートはコンテナ内部のループバックにあり、ホストにTCPポートを公開しません。

## DNS名を設定する

1. つなぐ機器のIPv4を確認します。ChromeとLinuxの固定IPはVM管理とトポロジー図に表示されます。Windows VMのIPv4はゲスト内で確認できます。
2. トポロジーの「DNS 設定」でDNSを有効にし、たとえば `app.example.test` とLinuxコンテナのIPv4を指定します。変更する際は、そのトポロジーのVMとコンテナを先に停止します。
3. 「ネットワークを起動」または接続先のVM・コンテナの起動で、ルーターとDNSコンテナが起動します。WindowsはDHCPでルーターのDNSを受け取り、ChromeとLinuxは同じセグメントのルーターをDNSに使います。
4. Linuxで `python3 -m http.server 8080 --bind 0.0.0.0` を実行し、同じトポロジーのChromeやWindowsから `http://app.example.test:8080/` を開くと確認できます。Windows上のサーバーへ接続する場合も、Windowsがそのセグメントのアドレスで待ち受け、Windows側で該当ポートが許可されていれば同じ手順で接続できます。

DNSの変更対象はIPv4のAレコードです。ポートはURLで指定します。透過プロキシを有効にしたトポロジーでは、ローカルDNSレコードにない名前は上流DNSへ問い合わせます。DNSレコードで参照しているコンテナは、先にレコードを変更することで移動・削除できます。

## MCP で操作する

`vm_inventory` の `endpoints` から `endpoint_id`、`topologies` から `topology_id` を確認します。下記のIDは自分の環境のものに置き換えます。

| 操作 | ツール |
| --- | --- |
| Chrome・Linuxを作成 | `container_create(name, kind, topology_id, segment)`。種類は `browser` または `linux` |
| 電源・接続先・削除 | `container_start`、`container_stop`、`container_set_network`、`container_delete` |
| Chromeのタブ | `browser_tabs`、`browser_new_tab`、`browser_close_tab` |
| Chromeを操作 | `browser_navigate`、`browser_screen`、`browser_input`。操作するタブの `tab_id` を指定 |
| Linuxでコマンドを実行 | `linux_exec(endpoint_id, command, timeout)`。SSH経由で標準出力・標準エラー・終了コードを取得 |
| DNSと電源 | `vm_topology_dns_save(topology_id, records)`、`vm_topology_power(topology_id, running)` |

DNS保存の例です。`records: null` でDNSを無効にできます。

```json
{
  "topology_id": "TOPOLOGY_ID",
  "records": [{"name": "app.example.test", "address": "10.40.0.14"}]
}
```

## E2Eを再実行する

上記の3イメージと、ホスト側のChromeが必要です。テストは一時的な台帳と専用のトポロジーを作成します。

```sh
WINDOWS_CONTAINERS_E2E=1 uv run --locked --project console/mcp --with playwright \
  python console/tests/container_e2e.py -v
```

Windowsの検証も行う場合は追加でWindows 11 x64 ISOを用意し、テスト用のHTTPサーバーをビルドします。隔離したWindowsインストーラー環境のファイアウォールを検証時だけ無効にし、別セグメントのChromeでHTTP応答とWindows側のDNS解決を確認します。

```sh
fixture=$(mktemp -d)
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/console/tests/fixtures,target=/src,readonly" \
  --mount "type=bind,source=$fixture,target=/out" \
  -e CGO_ENABLED=0 -e GOOS=windows -e GOARCH=amd64 -e GOCACHE=/tmp/go-build \
  golang:1.25 go build -ldflags="-s -w" -o /out/WINHTTP.EXE /src/winhttp.go
WINDOWS_CONTAINERS_E2E=1 WINDOWS_E2E_WINHTTP="$fixture/WINHTTP.EXE" \
  WINDOWS_E2E_ISO=/path/to/windows-x64.iso \
  uv run --locked --project console/mcp --with playwright python console/tests/container_e2e.py -v
```
