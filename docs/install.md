# インストールと最初の Windows VM

この手順は x86-64 の Ubuntu ホストで Docker Engine と KVM を使います。ブラウザから VM を作り、Windows のセットアップまで進めます。MCP は任意です。

## 1. ホストを準備する

- Ubuntu ホストに Git、Docker Engine、uv を用意します。uv は Python 3.12 以上を使用します。Python が未導入の場合、uv は利用する Python を取得します。最初のビルドと Python 環境の準備にはインターネット接続が必要です。[Docker Engine の導入](https://docs.docker.com/engine/install/ubuntu/) · [uv の導入](https://docs.astral.sh/uv/getting-started/installation/)
- Web サービスを動かすユーザーで、`docker info` と `test -c /dev/kvm` が成功することを確認します。Docker の操作に sudo が必要な状態では、同じユーザーで起動した Web サービスから Docker にアクセスできません。仮想ホスト内で使う場合は KVM のネスト仮想化も確認してください。
- Windows 11 x64 の ISO を Microsoft から取得します。Windows のエディションとライセンス、初回設定で使うアカウントを用意してください。[Windows 11 ISO](https://www.microsoft.com/ja-jp/software-download/windows11) · [Windows 11 Enterprise 評価版](https://www.microsoft.com/ja-jp/evalcenter/evaluate-windows-11-enterprise)
- ホストには ISO に加え、VM が使用するディスク容量の余裕が必要です。Windows 11 の最初の VM は 4 vCPU、8 GiB RAM、80 GiB の設定を基準にします。スパースディスクはゲストの書き込みに応じてホスト容量を消費します。[Windows 11 のシステム要件](https://www.microsoft.com/ja-jp/windows/windows-11-specifications)

## 2. ビルドして起動する

Ubuntu ホストの通常ユーザーで実行します。

```sh
git clone https://github.com/nyasukun/windows-console-mcp.git
cd windows-console-mcp
docker build -t windows-console-mcp:local .
```

ISO は Web 起動後にアップロードできます。既定の保存先はリポジトリの直下です。変更する場合は、書き込み可能な保存先を `WINDOWS_MANAGER_ISO_DIR` に指定し、Web と MCP に同じ設定を渡します。

まず端末から起動してログを確認できます。

```sh
./console/start.sh
```

継続して使う場合は、端末から起動したプロセスを Ctrl+C で終了してから、同じユーザーの systemd サービスに登録します。

```sh
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
ln -s "$PWD/console/start.sh" "$HOME/.local/bin/windows-console-web"
ln -s "$PWD/console/mcp/start.sh" "$HOME/.local/bin/windows-console-mcp"
install -m 644 systemd/windows-html-console.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now windows-html-console.service
curl -fsS --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:6083/api/management
```

シンボリックリンクの名前をすでに使っている場合は、`readlink` でリンク先を確認します。ログアウト後もサービスを起動し続ける必要がある場合は、ホスト管理者にユーザーの linger を有効化してもらいます。

### Chrome で開く

Ubuntu ホストで Chrome を使う場合は [VM 管理](http://127.0.0.1:6083/vms/) を開きます。別端末からは次の SSH トンネルを開いている間、同じローカル URL を使います。`vm-host` を自分の SSH ホスト名に置き換えてください。

```sh
ssh -N -o ExitOnForwardFailure=yes -L 6083:127.0.0.1:6083 vm-host
```

## 3. ブラウザで Windows VM を作る

ISO がまだない場合は最初に「ISO を登録」を押し、ダウンロード済みの `.iso` を選んで「登録」を押します。アップロードが終わるとテンプレート作成へ進みます。「ISO 管理」欄では、後からISOの登録、表示名の変更、削除ができます。表示名を変更しても、既存のVMやテンプレートから引き続き使えます。削除するとホスト上のISOファイルが消えます。使用中のISOは、参照しているVMやテンプレートがある間は削除できません。

1. VM の欄で「テンプレートを作成」を押し、名前と Windows ISO を選びます。初回は 4 vCPU、8192 MiB、80 GiB のまま保存できます。
2. 「VM を作成」で名前を入れます。初回設定で外部接続が必要なら、トポロジーを「接続なし」にして「インターネット接続を有効にする（個別NAT）」を選びます。トポロジーを使う場合は、そのトポロジーの透過プロキシ設定を確認します。
3. 一覧で「起動してコンソールを開く」を押します。画面上に `Press any key to boot from CD or DVD...` が出たら、コンソールの画面をクリックして Enter を押します。青い起動メニューになった場合は、最初の `UEFI QEMU DVD-ROM` を矢印キー＋Enter で選び、ISO のキー入力待ちでもう一度 Enter を押します。もう一方の DVD は補助ツール用です。
4. Windows セットアップで言語とキーボードを選びます。インストール先は新規 VM に割り当てた未割り当て領域です。インストールするエディションは自分のライセンスに合わせます。インストール中の再起動後は、画面が DVD のキー入力を求めても待ち、仮想ディスクから起動させます。再起動後の黒い画面で進捗が 0% から始まることがあります。電源を切らずに初回設定が表示されるまで待ちます。
5. 地域、ネットワーク、利用するアカウントなど、Windows の初回設定を完了します。Windows 11 Home および個人用途の Pro では、初回設定にインターネット接続と Microsoft アカウントが必要です。[Windows 11 のシステム要件](https://www.microsoft.com/ja-jp/windows/windows-11-specifications)
   - Windows 11 Pro の組織向けセットアップでローカルアカウントを使う場合は、「職場または学校用に設定する」→「サインイン オプション」→「代わりにドメインに参加する」を選び、Windows の画面に沿ってアカウントを作成します。
   - 初回設定の最終段階で「後で更新」を選んだ場合は、サインイン後に「設定」から Windows Update を実行します。
6. 最初の管理者サインイン後、メモ帳などで日本語入力とコピー・貼り付けを確認します。サインイン直後は補助ツールが起動するまで少し時間がかかることがあります。待っても応答しない場合は、Windows のエクスプローラーで「PC」→ CD ドライブ「CONSOLE_TOOLS」を開き、`install.cmd` を実行します。拡張子を非表示にしている場合の表示名は「install」です。補助ツールはその場で起動し、以後のサインイン時にも起動します。

AIエージェントに最初の VM の作成を依頼する場合は、VM 管理の上部にある「AIエージェントで操作」を開きます。クライアントを接続し、「最初のVMを作成する指示をコピー」から依頼を渡せます。作成済み VM の行から開くと、VM ID 入りの指示をコピーできます。VM ホスト上の OpenCode はリポジトリ直下で起動すると、同梱の設定を読み込みます。詳しい接続方法は [MCP 操作ガイド](../console/README.md) にあります。

## つまずいたとき

| 症状 | 確認すること |
| --- | --- |
| Web が開かない | ホスト上で `systemctl --user status windows-html-console.service` と `journalctl --user -u windows-html-console.service -n 50` を確認します。別端末からは SSH トンネルを先に開きます。 |
| ISO を登録できない | ファイルの拡張子、同名ファイルの有無、ホストの空き容量と保存先の書き込み権限を確認します。ホストに直接配置した場合は「更新」を押します。 |
| VM が起動しない | 管理画面のエラーを読み、`docker info`、`/dev/kvm`、ホストのメモリ・ディスク空き容量を確認します。 |
| 初回設定の地域選択で `OOBEREGION` が表示される | 「やり直す」を押し、地域を選び直します。 |
| Windows の「ネットワークに接続しましょう」で「インターネットなし」と表示される | スタンドアロン VM を停止し、一覧の「インターネット設定」で個別 NAT を有効にして「起動してコンソールを開く」を押します。初回設定が地域選択に戻った場合は、選択し直して進めます。トポロジーを使う場合はルーターと透過プロキシの設定を確認します。 |
| 停止中のまま待ちが終わらない | Windows がインストール中なら処理の完了を待ちます。通常停止が進まない場合は VM 行の「その他」から「強制停止」を選べます。「強制停止して削除」で仮想ディスクも消えます。 |

すでに利用中の環境は [アップグレードと復旧](upgrade.md) を参照してください。
