# Windows Console MCP

Ubuntu 上の Windows x64 VM を、SSH ポートフォワードした Chrome と MCP クライアントから操作できます。ブラウザは SPICE で画面を描画し、マウスとキー入力はホストの QMP ソケット経由で QEMU に届きます。

- **ブラウザ操作**　クリックなしのポインター移動、キー操作、ホスト IME での日本語入力、コピーと貼り付け。
- **ウォークスルー**　MCP から任意の位置に説明とカーソルを表示。説明枠の下のバーが指定時間で縮み、表示が消えます。
- **VM 管理**　Web と MCP でテンプレート、VM の電源、複数セグメントのルーターと透過プロキシを管理。
- **MCP 操作**　VM の起動・正常終了、ISO の切り替え、画面の差分更新、GUI・CLI 入力、ゲスト専用 URL での双方向ファイル転送。

## ガイド

- [インストールと最初の Windows VM](docs/install.md)
- [Chrome・LinuxコンテナとトポロジーDNS](docs/containers.md)
- [アップグレード、バックアップ、復旧](docs/upgrade.md)
- [MCP 操作とブラウザコンソール](console/README.md)

## 既存の Windows VM を登録する

以前の構成で作成した VM は、ホスト上で次のコマンドを実行すると VM 一覧に追加されます。仮想ディスク、TPM、ネットワーク設定、転送データは現在の保存先を使います。登録時に VM の停止は不要です。

```sh
uv run --locked --project console/mcp python console/legacy_vm.py
```

登録済み VM のコンソール表示、起動・停止、削除は VM 一覧から操作できます。削除後も Web のポート設定と既存の転送データは保存されます。新規 VM のコンソールとファイル転送は既存 VM を必要としません。

コンテナ名と RFB のホスト側ポートは、既定で `windows11-vm-clean` と `5912` です。変更する場合は `WINDOWS_CONSOLE_VM` と `WINDOWS_CONSOLE_RFB_PORT` を VM 起動時、Web サービス、MCP サーバーの環境で揃えてください。

## 複数の VM を管理する

1. [VM 管理](http://127.0.0.1:6083/vms/) の「ISO を登録」からインストール ISO をアップロードします。既存の ISO も一覧に表示されます。保存先を変える場合は Web と MCP の両方に `WINDOWS_MANAGER_ISO_DIR` を設定します。
2. [VM 管理](http://127.0.0.1:6083/vms/) で ISO、vCPU、RAM、ディスク容量を選び、テンプレートを追加します。
3. テンプレートから VM を作成し、「起動してコンソールを開く」から Windows をセットアップします。初回起動時に専用のディスクが作られます。テンプレートを編集・削除しても、作成済み VM の構成は維持されます。各 VM が使う ISO は同じ場所に置いてください。
4. 「停止」で Windows に ACPI シャットダウンを要求します。セットアップ中など正常終了できない場合は「その他」の「強制停止」または「強制停止して削除」を選べます。削除すると VM のディスクと設定が消えます。

VM 作成画面または一覧の「AIエージェントで操作」から、MCP の接続コマンドと指示例をコピーできます。作成後の例文には対象の VM ID が入ります。

管理データと VM のディスク・転送データは既定で `state-managed/` に保存されます。Web のポート設定と管理ソケットは `state-console/` にあります。以前のポート設定と転送データは引き継ぎます。これらのほか、`state-clean/`、ISO、仮想ディスク、認証情報は Git の対象外です。保存先を変える場合は `WINDOWS_MANAGER_STATE` を Web と MCP で揃えてください。MCP の操作名は [操作ガイド](console/README.md#vm-管理) にあります。

### ルーターと透過プロキシ

Web の「ネットワークトポロジー」でルーター、セグメント、接続中の VM と稼働状態を確認できます。「構成を編集」でセグメントや透過プロキシを変更します。停止中の VM はセグメントや「未接続の VM」へドラッグするか、「接続先を変更」から移動できます。先に停止や移動が必要な場合は、編集画面に対象の VM が表示されます。

トポロジにプライベート IPv4 CIDR を1〜8個登録し、停止中の VM をいずれかのセグメントに接続します。最初の VM の起動時にルーターコンテナが起動し、各セグメントの DHCP とセグメント間のルーティングを提供します。同じトポロジ内の VM は Windows 側のファイアウォールで許可された通信ができます。トポロジーに接続していない VM は、ゲスト専用の転送経路を使います。外部接続が必要な場合は、Web の「インターネット設定」または MCP の `vm_set_internet` で個別に NAT を有効にできます。ルーターを使うにはホストに `/dev/net/tun` が必要です。

「透過プロキシでインターネットに接続」を選ぶと、別のプロキシコンテナが公開 IPv4 宛ての TCP 通信を中継し、ルーターが DNS を転送します。Windows でプロキシ設定や追加の証明書は不要です。インターネット向け UDP と IPv6 は対象外です。外部 DNS リゾルバへ通信できる Docker ホストを使ってください。設定を切り替えるときは、接続中の VM をすべて停止します。

### 日本語入力とクリップボード

VM の起動時に、COM1 ヘルパーを含む補助 CD が生成されます。Windows Setup が補助 CD の応答ファイルを適用した場合は、最初の管理者サインイン時にヘルパーがインストールされます。

すでにインストール済みの Windows でヘルパーの追加や更新が必要な場合は、補助 CD `CONSOLE_TOOLS` を開き、`install.cmd` を実行します。ヘルパーは以降のサインイン時に自動起動します。マウス・物理キー・デモ表示は Windows のインストール画面から利用できます。

## デモを表示する

Chrome でコンソールを開いている間に、MCP クライアントから `console_demo_show` を呼びます。

```json
{
  "text": "「保存」をクリックすると変更が反映されます。",
  "x": 18000, "y": 10000,
  "pointer_x": 15000, "pointer_y": 10000,
  "duration_seconds": 6
}
```

座標は両軸とも 0〜32767 です。表示中はマウス操作にカーソルが追従します。次の説明を送ると差し替えられ、`console_demo_clear` で即座に消せます。ツール一覧、ファイル転送、画面ストリームの使用例は[操作ガイド](console/README.md)にあります。

## テスト

VM を起動せずに、MCP プロトコル、画面更新、入力、ファイル転送、デモ配信をテストできます。

```sh
uv run --locked --project console/mcp python -m unittest discover -s console/tests
```

VM 管理の E2E テストは Chrome、Web API、stdio MCP を一時インベントリで接続します。Docker と Chrome が必要です。Chrome のパスを変更する場合は `WINDOWS_E2E_CHROME` を設定します。KVM とビルド済みの Docker イメージがある環境では、`WINDOWS_E2E_ISO` に Windows の ISO を指定すると、VM の起動・再起動、コンソール、削除まで確認できます。

```sh
export WINDOWS_E2E_ISO=/path/to/windows-x64.iso
uv run --locked --project console/mcp --with playwright python console/tests/vm_management_e2e.py -v
uv run --locked --project console/mcp --with playwright python console/tests/mutate_vm_management.py
```

ミューテーションテストは一時ディレクトリにコピーしたソースに変更を1件ずつ加え、対応するテストが失敗することを確認します。ISO を指定すると実 VM の再起動に関するミューテーションも実行します。

起動中の Web コンソールでデモを検証するには、Chrome があるホストで次を実行します。実際に短い説明枠が表示されます。`--move-guest` を末尾に追加するとゲストのマウスも1回移動します。

```sh
uv run --project console/mcp --with playwright python console/tests/walkthrough_browser.py
```
