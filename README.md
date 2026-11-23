# Windows Console MCP

Ubuntu 上の Windows x64 VM を、SSH ポートフォワードした Chrome と MCP クライアントから操作できます。ブラウザは SPICE で画面を描画し、マウスとキー入力はホストの QMP ソケット経由で QEMU に届きます。

- **ブラウザ操作**　クリックなしのポインター移動、キー操作、ホスト IME での日本語入力、コピーと貼り付け。
- **ウォークスルー**　MCP から任意の位置に説明とカーソルを表示。説明枠の下のバーが指定時間で縮み、表示が消えます。
- **MCP 操作**　VM の起動・正常終了、ISO の切り替え、画面の差分更新、GUI・CLI 入力、ゲスト専用 URL での双方向ファイル転送。

## 起動する

必要なものは x86-64 Ubuntu、使用可能な `/dev/kvm`、Docker、Python 3.12 以上、uv、Windows x64 のインストール ISO です。VM には既定で 4 vCPU、8 GiB RAM、最大 80 GiB のスパースディスクを割り当てます。

```sh
git clone https://github.com/nyasukun/windows-console-mcp.git
cd windows-console-mcp
docker build -t windows-console-mcp:local .
./launch-vm.sh /path/to/windows-x64.iso
```

ホストの同じユーザーで、Web コンソールと MCP の起動コマンドを登録します。以下は初回セットアップの例です。

```sh
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
ln -s "$PWD/console/start.sh" "$HOME/.local/bin/windows-console-web"
ln -s "$PWD/console/mcp/start.sh" "$HOME/.local/bin/windows-console-mcp"
install -m 644 systemd/windows-html-console.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now windows-html-console.service
```

Mac の SSH 設定で Ubuntu ホストを `vm-host` として登録している場合、次のコマンドでブラウザ用のトンネルを開けます。`vm-host` は自分の SSH 接続先に置き換えてください。

```sh
ssh -N -o ExitOnForwardFailure=yes -L 6083:127.0.0.1:6083 vm-host
```

Chrome で [Web コンソール](http://127.0.0.1:6083/) を開き、Windows セットアップを進めます。Mac 上の Codex には別のターミナルで MCP を登録します。

```sh
codex mcp add windows-console -- ssh -T -o BatchMode=yes vm-host .local/bin/windows-console-mcp
```

コンテナ名と RFB のホスト側ポートは、既定で `windows11-vm-clean` と `5912` です。変更する場合は `WINDOWS_CONSOLE_VM` と `WINDOWS_CONSOLE_RFB_PORT` を VM 起動時、Web サービス、MCP サーバーの環境で揃えてください。

### 日本語入力とクリップボードを有効にする

Windows にサインインしたら、付属の COM1 ヘルパーをユーザーセッションにインストールします。Ubuntu 側で `genisoimage` を使ってツール ISO を作ります。

```sh
genisoimage -J -r -V WINDOWS_CONSOLE -o state-clean/console-setup.iso console/guest/
```

MCP の `select_image` に `{"image_id":"guest-tools/console-setup.iso"}` を渡し、Windows の CD ドライブから `install.cmd` を実行します。ヘルパーはサインイン時に自動起動します。マウス・物理キー・デモ表示は Windows のインストール画面から利用できます。

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

起動中の Web コンソールでデモを検証するには、Chrome があるホストで次を実行します。実際に短い説明枠が表示されます。`--move-guest` を末尾に追加するとゲストのマウスも1回移動します。

```sh
uv run --project console/mcp --with playwright python console/tests/walkthrough_browser.py
```
