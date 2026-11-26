# Windows Console

Mac で SSH を起動し、Chrome で http://127.0.0.1:6083/vms/ を開き、利用する VM の「コンソール」を選びます。

```sh
ssh -N -o ExitOnForwardFailure=yes -L 6083:127.0.0.1:6083 vm-host
```

- 通常のマウス移動は、表示倍率を考慮して USB Tablet の絶対座標へ変換します。「マウスをキャプチャ」は Chrome の Pointer Lock を使い、移動量を絶対座標に積算します。Esc で解除します。右上のセッションメニュー（Ctrl+Alt+Home）にはブラウザの移動回数と、QEMU が処理した最新の移動番号を表示します。描画時に移動をまとめるので、2つの数値は途中で飛びます。
- 画面をクリックしてキー入力します。画面上の入力先をクリックすると、Mac の IME で直接変換できます。候補と変換中の文字は最後にクリックした位置付近に表示され、確定した文字が Windows に Unicode 入力されます。「日本語入力」で Mac 側の IME を使って文章を作り、⌘+Enter で Windows のアクティブな入力欄に送ります。キーの押下状態はフォーカス喪失時や接続終了時に解放します。
- Windows で文字を選択し「選択をコピー」、またはブラウザで「貼り付け」を押します。対象は UTF-8 換算で一度に 64 KiB までのプレーンテキストです。「日本語入力」パネルからの送信と貼り付けには Windows のクリップボードを使用します。画面への直接 IME 入力はクリップボードを使いません。Chrome が求めた場合はこの localhost ページへのクリップボード権限を許可します。
- 黒い画面から Windows のサインインや UEFI を操作する場合も物理キーとマウスは使えます。クリップボードと確定済みテキストの送信にはログイン中のユーザーセッションが必要です。

## VM 管理

Web の `/vms/` と MCP は同じインベントリを使用します。次のツールで作成した VM は Web の一覧に表示され、個別の画面を開けます。

| MCP ツール | 用途 |
| --- | --- |
| `vm_inventory` | 管理対象 VM と電源状態、テンプレート、利用可能な ISO ID、トポロジの一覧 |
| `vm_iso_register` / `vm_iso_rename` / `vm_iso_delete` | ホスト上の絶対パスから ISO を登録、ID を保ったまま表示名を変更、未使用の ISO ファイルを削除 |
| `vm_template_save` | ISO と vCPU・RAM・ディスク容量の組み合わせを追加・編集 |
| `vm_template_delete` | テンプレートを削除 |
| `vm_create` | `template_id` から停止状態の VM を作成 |
| `vm_start` / `vm_stop` / `vm_delete` | 管理対象 VM の起動・停止・削除。稼働中の VM を強制停止して削除する場合は `vm_delete` に `force: true` を明示 |
| `vm_topology_save` / `vm_topology_delete` | IPv4 セグメントと透過プロキシ設定を持つルーターを管理 |
| `vm_set_network` | 停止中の VM の接続トポロジとセグメントを変更 |

`vm_create` や `vm_set_network` でネットワークに接続する場合は、`topology_id` と `segment`（例 `10.40.0.0/24`）を指定します。

`vm_inventory` で得た `vm_id` は、`vm_status`、`vm_power`、`vm_set_internet`、ISO、`console_*`、`file_*` に渡せます。ライブ配信は `console_stream_open` で VM を指定し、以降は返された `stream_id` を使います。ファイル転送のチャンク・完了操作には同じ `vm_id` を渡してください。ローカルのファイル CLI では `--vm-id` が使えます。

ID を省略した既存の呼び出しは登録済みの旧 VM を優先します。その VM を削除したあと、管理対象が1台ならその VM を選びます。新規 VM が複数台の場合は ID を指定してください。トポロジーに接続した VM の外部接続は `vm_topology_save` で変更します。トポロジーに接続していない VM では `vm_set_internet` が個別 NAT を設定します。

## 構成

`server.py` はループバックで待ち受け、同一オリジンの WebSocket を受けます。最初のタブは QEMU の SPICE Unix ソケットと spice-html5 を使用します。追加タブは QEMU の読み取り専用 RFB 接続を通じて同じ画面を JPEG で受信します。どのタブでも QMP 経由で入力できます。あるタブでキーやボタンを押している間は、そのタブが入力を保持し、解放または切断で他のタブから操作できるようになります。

`guest/bridge.ps1` は Windows の COM1 でテキスト操作を受けます。Windows の新規インストール時に、補助 CD の応答ファイルが最初の管理者サインインで `install.cmd` を実行します。ヘルパーはユーザープロファイルに配置され、以降もサインイン時に起動します。ホストで補助 CD を再生成するコマンドは次のとおりです。

```sh
uv run --locked --project console/mcp python console/setup_media.py
```

`run-vm-clean.sh` が VM ごとの COM1、SPICE、QMP の Unix ソケットを用意します。Web サービスは `systemctl --user restart windows-html-console` で再起動できます。単独起動には `console/start.sh` を使います。

操作パネルとテキスト入力は [Apache Guacamole](https://guacamole.apache.org/doc/gug/using-guacamole.html)、物理キーとフォーカス管理は [noVNC](https://github.com/novnc/noVNC/blob/master/core/input/keyboard.js) の設計を参考にしています。

## MCP から操作する

管理対象の VM とテンプレートは `vm_inventory` で確認できます。VM 管理の上部にある「AIエージェントで操作」から、MCP の接続方法と、最初の VM を作る指示をコピーできます。作成済み VM の行から開くと、指示に `vm_id` が入ります。

VM ホスト上の OpenCode は、リポジトリ直下で起動すると同梱の [opencode.json](../opencode.json) から stdio サーバーを起動します。`opencode mcp list` で `windows-console` の接続を確認し、新しいセッションで `vm_inventory` を呼び出します。別端末の OpenCode では、設定の `command` に `["ssh", "-T", "-o", "BatchMode=yes", "vm-host", ".local/bin/windows-console-mcp"]` を指定します。ホストの起動リンクは [インストールガイド](../docs/install.md) の手順で用意します。

ホストで Codex を使う場合は、次のコマンドで登録し、新しいセッションを開きます。

```sh
codex mcp add windows-console -- "$HOME/.local/bin/windows-console-mcp"
```

Mac 上の Codex に登録する場合は、既存の SSH 接続先を使ってホスト上でサーバーを起動できます。SSH 鍵でログインできる状態で実行します。

```sh
codex mcp add windows-console -- ssh -T -o BatchMode=yes vm-host .local/bin/windows-console-mcp
```

他の stdio 対応クライアントでも同じコマンドと引数を指定できます。ホストでは `uv` が `console/mcp/uv.lock` の依存関係を初回にインストールします。MCP を起動するユーザーには対象の Docker コンテナと `windows-html-console.service`（systemd ユザーサービス）の操作権限が必要です。ブラウザで同じ Windows 画面を見るときは、上記の SSH ポートフォワードを併用できます。

| 操作 | MCP ツールと使い方 |
| --- | --- |
| VM の状態と電源 | `vm_status` で Docker と QEMU の状態を取得。`vm_power` に `start`、`shutdown`、`restart` を指定。終了は ACPI の電源ボタン経由で、再起動は終了を確認してから起動します。Windows の電源ポリシーや保存確認で終了が保留された場合はタイムアウトを返します。 |
| インターネット接続 | `vm_internet_status` で現在と次回起動時の設定を確認。`vm_set_internet` に `{"enabled":true}` または `{"enabled":false}` を渡すと、外向きの IPv4 NAT を次回起動から切り替えます。稼働中の VM にすぐ反映する場合は `"apply_now":true` も指定します。正常終了がタイムアウトした場合は設定が次回起動用に残ります。ファイル転送用 URL はどちらの設定でも利用できます。 |
| イメージ選択 | `list_images` の `images[].id` を `select_image` に渡します。対象は ISO カタログと対象 VM の状態ディレクトリ直下の ISO です。実行中は CD ドライブに交換し、停止中は次回起動用に選択します。`current` と `next_boot` は QEMU 側のパスです。起動順は OS ディスクが先です。 |
| 画面とマウス | `console_screen` はゲストの PNG を返します。`console_pointer` は移動、クリック、ダブルクリック、ホイールに対応。`console_drag` は左ボタンでドラッグします。PNG の幅 W、高さ H のピクセル (x, y) は `round(x*32767/(W-1))`、`round(y*32767/(H-1))` に換算します。 |
| キー・日本語・コピー | `console_keys` は例として `["ctrl","a"]`、`["ret"]`。`console_text` は日本語を Unicode 入力し、`method="paste"` で貼り付けます。`console_copy_selection` は選択した文字列を返します。テキスト操作にはサインイン中の COM1 ヘルパーが必要です。 |
| CLI | `console_open_terminal` の `shell` に `powershell` または `cmd` を指定。画面でプロンプトを確認してから `console_cli` にコマンドを1行渡します。応答は Enter の送信確認です。結果は同じ Windows 画面で読み取るか、出力を選択してコピーできます。 |
| コンソール設定 | `console_settings` で待受ポート、サービス状態、URL、SSH 転送引数を取得。`configure_console` に 1024〜65535 の `port` を指定すると、Web サービスを再起動して疎通を確認します。ブラウザは返された URL に再接続します。 |

管理用 Unix ソケットは `state-console/console-admin.sock`（0600）です。MCP 入力とブラウザ入力は同じコンソールサーバーで順序を管理します。ブラウザでキーやマウスボタンを押している間に MCP が操作を要求した場合は、離してから再試行します。スクリーンショットの一時ファイルは応答生成時に削除します。

### Windows のクリップボードを直接読み書きする

`console_clipboard_read()` は、サインイン中の Windows ユーザーのクリップボードからプレーンテキストを取得し、`{"text":"…"}` を返します。`console_clipboard_write(text)` は、文字を入力欄へ貼り付けずにクリップボードを更新します。空文字列を渡すとクリップボードを消去します。どちらも日本語を含む UTF-8 換算で最大 64 KiB です。

stdio と LAN の SSE の両方で使えます。既にインストール済みの Windows でヘルパーを更新するときは、最新版の補助 CD を接続した状態で Windows から `install.cmd` を実行します。

### LAN から SSE で接続する

VM 管理の「AIエージェントで操作」→「SSE 接続」に、稼働中のサーバーの URL が表示されます。「トークンを表示」からトークンを個別にコピーできます。SSE 対応の OpenCode 向け設定もコピーでき、トークンはクライアントの `WINDOWS_CONSOLE_MCP_TOKEN` 環境変数で渡します。

MCP クライアントでトランスポートに **SSE**、URL に `https://<ホストのLAN IP>:6084/sse` を指定します。SSE 接続とメッセージ送信の両方に `Authorization: Bearer <トークン>` ヘッダーを付け、ホストのサーバー証明書をクライアントで信頼します。

OpenCode 1.18.34 では、コピーした設定をクライアントのプロジェクトに追加し、サーバーの公開証明書をクライアント端末に置いたうえで、次のように接続を確認できます。

```sh
read -rs WINDOWS_CONSOLE_MCP_TOKEN
export WINDOWS_CONSOLE_MCP_TOKEN
export NODE_EXTRA_CA_CERTS=/path/to/server.pem
opencode mcp list
```

ホスト側では LAN IP とサブネット、IP を SAN に含む TLS 証明書と秘密鍵、ランダムなトークンを指定して起動します。次の IP は例です。

```sh
console/mcp/start.sh --sse --bind 192.168.1.10/24 \
  --token-file /path/to/token --cert /path/to/server.pem --key /path/to/server-key.pem
```

トークンには `openssl rand -hex 32` で生成した値を使えます。トークンファイルと秘密鍵はホストユーザーだけが読める場所に置き、クライアントにはトークンと公開証明書を渡します。

### ブラウザでウォークスルーデモを見せる

`console_demo_show` は、Web コンソールを見ている人に説明枠と強調カーソルを表示します。例えば、次の引数で「保存」を指しながら、その少し右に説明を6秒間表示できます。

```json
{
  "text": "「保存」を選ぶと、変更が反映されます。",
  "x": 18000, "y": 10000,
  "pointer_x": 15000, "pointer_y": 10000,
  "duration_seconds": 6
}
```

- 座標はゲスト画面の左上が `(0, 0)`、右下が `(32767, 32767)` です。`x` と `y` は説明枠の左上を指定します。右端・下端では枠全体が見える位置に自動調整します。
- `pointer_x` と `pointer_y` を省くと、最後にコンソールで操作したマウス位置を指します。最初の操作前は説明枠の位置を指します。表示中に `console_pointer` などでマウスを動かすと、強調カーソルが追従します。デモ表示だけで Windows のマウス位置や入力フォーカスが変わることはありません。
- 表示時間は秒単位で、小数も指定できます。既定は5秒です。説明枠の下の細いバーが時間とともに短くなり、時間が来ると枠とカーソルが消えます。途中でブラウザを開き直すと残り時間から表示します。
- 次の `console_demo_show` で表示を差し替えられます。`text` を空文字列にするとカーソルだけを表示します。`console_demo_clear` は表示を即座に終了します。録画にはブラウザの画面か、MCP の `demo` 映像を使えます。

## MCP でファイルを転送する

**クライアント → Windows** は、`file_begin_download(filename, size)` で転送 ID を取得し、`file_write_chunk(transfer_id, offset, data_base64)` でファイルを順番に送ります。`file_publish(transfer_id)` が返す `url` を Windows のブラウザで開くと、元のファイル名でダウンロードできます。公開後は HTTP Range でのダウンロード再開にも対応します。

**Windows → クライアント** は、`file_request_upload()` が返す `url` を Windows のブラウザで開きます。ファイルを選んで「アップロード」を押し、`file_status(transfer_id)` が `ready: true` になったら、`file_read_chunk(transfer_id, offset, length)` の `data_base64` を順番にデコードして保存します。次の位置は `next_offset`、末尾は `eof` で判定できます。送信が途中で切れた場合、同じフォームでファイルを再選択できます。完了後の URL は上書きを受け付けません。

ファイル全体のサイズにアプリ上限はありません。MCP メッセージは元データ最大 512 KiB に分割し、HTTP 側もストリームとして読み書きします。実容量はホスト・VM・受信側の空き容量とファイルシステムに依存します。VM の停止中にも URL を発行でき、起動後に使えます。転送 ID は MCP の再接続後も有効です。書き込みは同じオフセット・同じデータで再試行でき、現在のバイト数は `file_status` で取得できます。受け渡しが終わったら `file_discard(transfer_id)` で URL を無効化してホストの保存領域を解放します。

### ローカルファイルを扱う MCP クライアント

付属の `console/mcp/files.py` はファイルを分割して stdio MCP で転送します。バイナリをチャットに貼り付ける必要はありません。以下はプロジェクトのコピーと `uv` がある Mac での例です。Ubuntu ホストで実行する場合は `--ssh vm-host` を省きます。

```sh
uv run --project console/mcp python console/mcp/files.py --ssh vm-host send ./資料.zip
uv run --project console/mcp python console/mcp/files.py --ssh vm-host request-upload
uv run --project console/mcp python console/mcp/files.py --ssh vm-host receive TRANSFER_ID ./受信.zip
uv run --project console/mcp python console/mcp/files.py --ssh vm-host discard TRANSFER_ID
```

`send` と `request-upload` は転送 ID と Windows 用 URL を JSON で返します。URL は転送ごとにランダムに発行され、パスワード入力は不要です。QEMU は返されたゲスト専用アドレスの HTTP 接続を、その VM の転送専用 Unix ソケットへ中継します。通常は `10.77.0.4:80` を使い、トポロジーと重複する場合は別のアドレスを返します。VM は起動時に Intel 82574L（e1000e）を接続します。Windows 11 25H2 には対応ドライバーが含まれます。発行結果の `guest_network` は、接続済みなら `attached`、停止中は `on_next_start` です。更新前から稼働している VM で `requires_cold_start` と表示された場合は、Windows を正常終了して VM を起動すると、同じ URL が使えます。MCP の SSH 接続と Web コンソールの転送設定は引き継がれます。旧 VM を削除した後も、それ以前の転送 ID を使ったホスト側の状態取得・読み出しは ID を省略して呼び出せます。

## MCP の画面ストリームと連続操作

stdio または SSE の同じ MCP 接続で `console_stream_open(max_fps=12, max_width=1280)` を呼ぶと、`stream_id`、画面リソースの `frame_uri`、元と配信時の解像度、最初のフレーム番号を取得できます。映像にはホスト内の QEMU ディスプレイの共有・読み取り専用 RFB 接続を使い、矩形の差分をメモリ上で合成して JPEG にします。入力は従来のコンソールと同じ QMP 経路を使います。SSH 転送ポートの追加は不要です。

- `console_stream_open(views="both")` で、素の `bare` と吹き出し・強調カーソル・残り時間バーを合成した `demo` を同時に取得できます。`views="demo"` はデモ映像のみ、既定の `views="bare"` は素の映像のみです。デモの文字描画にはホストの `fonts-noto-cjk` を使います。別の日本語TrueType/OpenTypeフォントは `WINDOWS_CONSOLE_DEMO_FONT` で指定できます。
- 戻り値の `views.bare.frame_uri` と `views.demo.frame_uri` は別々に購読できます。ツールで取得する場合は `console_stream_next(stream_id, view="demo", after=0)` のように指定し、次回の `after` には同じ映像の `frame_sequence` を使います。入力と終了は共通の `stream_id` を使います。録画時は、ストリームを開いてからの経過時間を示す `timestamp_ms` で両映像を揃えられます。

- リソース購読に対応する MCP クライアントは `resources/subscribe(frame_uri)` のあと初回の `resources/read(frame_uri)` を実行します。その後は `notifications/resources/updated` を受けて最新の JPEG を読みます。未読通知と画像は各1件にまとめます。
- ツールのみを使うクライアントは `console_stream_next(stream_id, after=0)` で初回の JPEG を取得します。以降は最後に受け取った `frame_sequence` を `after` に渡すと更新まで待機し、`timeout_ms` の間に変更がなければメタデータだけが返ります。標準設定の最大更新レートは 12 fps、指定範囲は 1〜30 fps です。表示更新、画像サイズ、SSH 回線、クライアントの処理速度によって実レートは変わります。
- 入力は `console_stream_input(stream_id, sequence, events)` にまとめて送ります。`sequence` は 1 から始め、応答ごとに 1 増やします。同じ番号と同じ内容の直前のバッチを再送した場合は直前の ACK を返します。`accepted` は QEMU が処理したイベント数、`held` は押下中のキーとボタンの数です。`error` があれば部分適用の件数を確認します。
- 座標は横・縦それぞれ 0〜32767 の絶対座標で、イベントは `move`、`button`、`key`、`wheel` に対応します。キーやボタンを押したまま次のバッチで移動・解放できます。保持中に入力が20秒途切れた場合や切断時には、サーバーが押下を解放します。
- 終了時は `console_stream_close(stream_id)` を呼びます。各 stdio クライアントが持てるストリームは1本です。従来の `console_screen` も原寸 PNG の単発取得に使えます。

画面中央をクリックする1バッチの例です。

```json
{"stream_id":"OPENで得たID","sequence":1,"events":[
  {"op":"move","x":16384,"y":16384},
  {"op":"button","button":"left","down":true},
  {"op":"button","button":"left","down":false}
]}
```

Python SDK では、初期化済みの `ClientSession` で次の画面を待ちながら入力を送れます。実際の操作では `ack` の `accepted` と `error` を確認してから入力番号を進めます。

```python
import asyncio
import json

opened = await session.call_tool("console_stream_open", {"max_width": 960})
stream = json.loads(opened.content[0].text)
sid = stream["stream_id"]
try:
    pending = asyncio.create_task(session.call_tool("console_stream_next", {
        "stream_id": sid, "after": stream["frame_sequence"], "timeout_ms": 1000,
    }))
    result = await session.call_tool("console_stream_input", {
        "stream_id": sid, "sequence": 1,
        "events": [{"op": "move", "x": 16384, "y": 16384}],
    })
    ack = json.loads(result.content[0].text)
    frame = await pending
    # 更新時は frame.content[1] が JPEG、content[0] がフレーム番号などの JSON。
finally:
    await session.call_tool("console_stream_close", {"stream_id": sid})
```
