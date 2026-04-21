# STYLY NetSync Custom Console

STYLY NetSync Server 用のブラウザベース管理コンソールです。

STYLY NetSync の REST API は主に Network Variables の設定用途で、現在の room 状態をリアルタイムに取得する API はありません。そのため、このプロジェクトでは Python 製の bridge が ZeroMQ 経由で NetSync room に参加し、受信したリアルタイムメッセージを WebSocket でブラウザへ転送します。

GitHub Pages: https://afjk.github.io/STYLY-NetSync-Custom-Console/

## ファイル構成

- `index.html` - GitHub Pages 用の入口
- `NetSyncWebClient.html` - ブラウザで開く管理コンソール UI
- `bridge_server.py` - WebSocket bridge、NetSync discovery、Web UI 配信サーバー
- `start_bridge_server.sh` - 起動スクリプト
- `idea/` - 今後の設計メモ

## 必要環境

- Python 3
- `uv` 推奨

`uv` が使える場合、`start_bridge_server.sh` は `bridge_server.py` を uv script として実行し、必要な Python 依存関係を自動で用意します。

必要な依存関係:

- `pyzmq`
- `websockets`

`uv` を使わない場合は手動でインストールしてください。

```bash
python3 -m pip install pyzmq websockets
```

## 起動

```bash
./start_bridge_server.sh
```

デフォルトでは以下の動作になります。

- 通常の NetSync discovery で STYLY NetSync Server を探索
- Web コンソールを `http://<bridge-ip>:8080/` で配信
- ブラウザからの WebSocket 接続を `ws://<bridge-ip>:8765` で受け付け
- `default_room` を購読

起動ログ例:

```text
[Bridge] Discovering NetSync server on port 9999...
[Bridge] Discovered NetSync server 'STYLY-NetSync-Server' at tcp://192.168.1.20 (dealer:5555, sub:5556, via udp-broadcast)
[HTTP] Web console URLs:
  - http://127.0.0.1:8080/
  - http://192.168.1.10:8080/
[Bridge] Reachable URLs:
  - ws://127.0.0.1:8765
  - ws://192.168.1.10:8765
```

同じ Mac、または同一ネットワーク上の別 PC から HTTP URL を開いてください。

## 外部 PC から開く

Mac 側で bridge を起動します。

```bash
./start_bridge_server.sh
```

別 PC のブラウザで以下を開きます。

```text
http://<mac-ip>:8080/
```

Web コンソールは自動的に以下の WebSocket URL を使用します。

```text
ws://<mac-ip>:8765
```

Mac のファイアウォールで TCP `8080` と `8765` の着信を許可してください。

## NetSync Server を手動指定する

ネットワーク構成によって discovery が届かない場合は、NetSync Server を明示指定できます。

```bash
./start_bridge_server.sh --server tcp://192.168.1.20
```

room を指定する場合:

```bash
./start_bridge_server.sh --room my_room
```

特定のネットワークインターフェースだけで待ち受ける場合:

```bash
./start_bridge_server.sh --http-host 192.168.1.10 --ws-host 192.168.1.10
```

HTTP 配信を無効にして WebSocket bridge だけ起動する場合:

```bash
./start_bridge_server.sh --no-http
```

## コンソール機能

- 参加者一覧のリアルタイム表示
- 参加者のダブルクリック詳細表示
- deviceId / clientNo マッピング表示
- pose 受信状態表示
- Global Network Variable の表示と設定
- Client Network Variable の表示と設定
- 選択中参加者への RPC 送信
- 簡易 top-down map 表示

## ネットワークメモ

STYLY NetSync discovery は基本的に同一サブネット内で動作します。別サブネットでは UDP broadcast discovery が届かない場合があります。その場合は `--server tcp://<server-ip>` を使用してください。

Mac が複数のネットワークインターフェースを持つ場合、bridge はデフォルトで `0.0.0.0` に bind するため、すべての有効な IP から Web コンソールと WebSocket bridge にアクセスできます。特定の interface に限定したい場合は `--http-host` と `--ws-host` を指定してください。
