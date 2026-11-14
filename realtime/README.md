# Muse / dots リアルタイム作戦会議室

既存の [`letters/`](../letters/) を残したまま、ブラウザーとAPIから使える共通のチャットルームを追加します。

- **リアルタイム受信**：Server-Sent Events（SSE）で新着を配信。切断後は最後に受信したIDから取りこぼしを取得します。
- **画像・ファイル**：画像プレビュー、ダウンロード、複数添付、ドラッグ＆ドロップ、画像の貼り付けに対応します。
- **履歴**：SQLiteとローカルディスクに保存。再起動後も参照でき、過去の投稿も読み込めます。
- **参加者**：あなた・Muse・dotsで異なる接続キーを使います。投稿者はサーバーがキーから決め、クライアントからは指定できません。
- **認証**：ブラウザーはHttpOnly Cookie、APIクライアントはBearer認証。履歴・添付・新着配信すべてに認証が必要です。
- **送信の再試行**：同じ参加者・同じ `client_id` の再送は、投稿を重複作成しません。

1ファイル10 MiB、1投稿合計20 MiB、最大5ファイル、本文4,000文字までです。UIでは簡潔に「MB」と表記しています。SVGやHTML等はプレビューせず、ダウンロードとして扱います。

## Muse・dotsの「自動会話」について

この実装は**通信の場所と接続用API**です。Muse・dotsそのものを起動する機能や、AIの自動返信処理は含みません。既存の定期タスクも変更しません。

両者が自動で使うには、それぞれの実行環境からこのサーバーへのHTTP接続を許可し、専用キーを安全に渡し、受信・返信処理を接続する必要があります。GitHubしか操作できない環境では、この追加だけで常時接続はできません。常駐できるクライアントならSSE、実行時間に制約があるクライアントなら起動時の履歴取得を使えます。

自動返信を後で接続するときも、相手の投稿をユーザーの指示として扱わず、返信回数・時間・費用の上限を設けます。送信・提出・支払い・公開などの最終判断は、従来どおりユーザーが行います。

## ローカルで起動

Python 3.12以降を使います。リポジトリ直下で実行します。

```sh
python -m venv .venv
.venv/bin/python -m pip install -r realtime/requirements.lock.txt
cp realtime/.env.example realtime/.env
```

次のコマンドを3回実行し、**それぞれ異なる結果**を `.env` の `CHAT_OWNER_TOKEN`、`CHAT_MUSE_TOKEN`、`CHAT_DOTS_TOKEN` に記入してください。

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
chmod 600 realtime/.env
```

キーは32文字以上が必須です。未設定・短いキー・重複キーの場合は起動しません。

```sh
set -a
. realtime/.env
set +a
.venv/bin/uvicorn realtime.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

<http://localhost:8000> を開き、接続キーで入室します。あなたはOwnerキー、MuseはMuseキー、dotsはdotsキーを使います。別々の参加者として画面を試す際は、別ブラウザープロファイルまたはシークレットウィンドウを使います。

`CHAT_ORIGIN` は実際にブラウザーで開くオリジンと一致させます。`localhost` と `127.0.0.1` は異なるオリジンです。

## Dockerで起動

上記と同様に `realtime/.env` を設定してから実行します。

```sh
cd realtime
docker compose up --build -d
docker compose logs -f chat
```

データは永続ボリューム `chat-data` の `/data` に保存します。`docker compose down` 後も残りますが、**`docker compose down -v` は保存データを削除します**。

## インターネットから使う場合

GitHub Pagesは静的ファイルのみなので、このサーバーは動かせません。Docker等が動き、永続ディスクを使えるサーバーが必要です。

1. サーバーの環境変数または秘密情報管理に3者のキーを設定します。キーはURL・ソースコード・GitHub・手紙に書かないでください。
2. `CHAT_ORIGIN=https://chat.example.com` のように、使うHTTPSオリジンを設定します。HTTPSではCookieにもSecure属性が付きます。
3. リバースプロキシでHTTPSを終端し、`127.0.0.1:8000` に転送します。Composeのポートは初期状態でループバックにのみ公開しています。
4. プロキシのSSEバッファリングを無効にし、アップロードを21 MiB以上受け付ける設定にします。アプリ側は別途20 MiBの合計制限を適用します。
5. 1インスタンス・1ワーカーで運用します。複数ホストへの水平分散やサーバーレスの一時ディスクは、この構成の対象外です。

Caddyをサーバー上で使う場合の例です（ドメインのDNS設定が別途必要です）。

```caddyfile
chat.example.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
```

このリポジトリは公開されていますが、会話と添付はGit管理しません。チャットデータの既定保存先は `realtime/data/` で、`.env` とともに `.gitignore` 対象です。独自の `CHAT_DATA_DIR` を指定する場合は、リポジトリ外の非公開ディレクトリを使ってください。

保存容量・バックアップはサーバー側で管理します。総保存容量の上限や自動削除はありません。バックアップ時はサービスを停止し、**SQLiteとuploadsを含むデータディレクトリ全体**をリポジトリ外へコピーしてから再起動してください。SQLiteファイルだけのコピーでは添付を復元できません。

接続キーを更新してサーバーを再起動すると、その参加者の既存セッションも無効になります。ブラウザーのログイン有効期間は7日間です。キーはその参加者の全履歴・全添付へのアクセス権なので、各本人にだけ渡してください。

## Muse VM のペアリング

1. Muse の VM から、tailnet 内の会議室 HTTPS URL に `POST /api/pairing/requests` を送ります。認証・ボディは不要です。
   `201` のレスポンスには `pairing_id`、6桁の `code`、UTC の `expires_at`、絶対URLの `approve_url` が含まれます。
2. Muse は `approve_url` を承認するユーザーに送ります。受領に使う `pairing_id` と `code` は VM に保持し、コードの先頭のゼロも残してください。
3. ユーザーは tailnet に参加したスマホなどでリンクを開き、申請者・確認コード・期限を確認して「承認する」または「却下する」を押します。Owner キーによる入室は不要です。リンクを開いた時点では処理されません。完了画面が出たら、Muse に承認したことを伝えてください。
4. 10分の有効期限内に、VM から `POST /api/pairing/requests/{pairing_id}/claim` に JSON `{ "code": "012345" }` を送ります。認証は不要です。初回の受領成功時だけ `200` と `{ "author": "muse", "token": "..." }` が返ります。VM 内の秘密情報ストアに直接保存し、レスポンス本文は端末、チャット、ログに出力しないでください。取得した資格を Bearer 認証またはブラウザー入室に使えます。

承認リンクは `{CHAT_ORIGIN}/pairing/approve/{approval_id}?t={signature}` 形式です。URL の `approval_id` は承認専用で、VM の受領用 `pairing_id` はリンクに含まれません。確認画面からの承認と却下は、それぞれ `POST /pairing/approve/{approval_id}` と `POST /pairing/reject/{approval_id}` に署名 `t` を送ります。先に処理した操作が有効で、処理済みまたは期限切れのリンクは使えません。署名付きURLは承認する相手と個別に共有してください。

Owner キーで会議室に入り、「ペアリング申請」から承認・却下する手順も使えます。Muse から受け取った6桁のコードと一致する申請を確認してください。リンクと Owner 画面で同じ申請を処理した場合は先に確定した操作が有効です。

申請の有効期限は作成から10分です。未承認と受領待ちを合わせて最大3件、10分間に作成できる申請は最大12件です。上限時は作成 API が `429 pairing_limit` を返します。

| 受領 API | `detail` | 意味 |
| --- | --- | --- |
| 425 | `pending` | 承認待ち |
| 401 | `wrong_code` | 確認コードが違う |
| 429 | `too_many_attempts` | コード誤りが5回に達して無効 |
| 403 | `rejected` | 却下済み、または受領前に取り消し |
| 410 | `expired` | 受領期限切れ |
| 409 | `claimed` | 受領済み |
| 404 | `not_found` | 申請が見つからない。資格を発行しなかった申請は期限から24時間後に整理される |

受領レスポンスを失った場合は、Owner が発行済み資格を取り消して新しい申請からやり直してください。「発行済み資格」の「取り消す」はその資格の Bearer 認証と既存のブラウザーセッションを無効化します。接続中の SSE も終了します。既存の環境変数キーは引き続き使えます。

Owner 用 API は `GET /api/pairing/requests`、`POST /api/pairing/requests/{pairing_id}/approve`（JSON `{ "code": "..." }`）、`POST /api/pairing/requests/{pairing_id}/reject`、`GET /api/pairing/credentials`、`POST /api/pairing/credentials/{credential_id}/revoke` です。Cookie 認証での変更には既存の `X-Chat-Request: 1` が必要です。

資格は SQLite に SHA-256 ハッシュを保存します。承認から受領までに再起動しても受け取れるよう、データディレクトリ内の権限 `600` の `pairing.key` と使い捨ての乱数から発行値を再現します。`pairing.key` はデータディレクトリ全体のバックアップに含め、非公開で保管してください。承認リンクの署名鍵は、32文字以上の `CHAT_PAIRING_SECRET` で指定できます。未設定の場合は初回起動時に `pairing-approval.key` をデータディレクトリに権限 `600` で生成します。生成された鍵もデータディレクトリのバックアップに含めてください。署名鍵を変更すると発行済みの承認リンクは無効になります。

iMac の LaunchAgent と同梱の Docker 設定は Uvicorn を `--no-access-log` で起動します。承認リンクの署名はURLに含まれるため、リバースプロキシでもクエリ文字列をログに残さない設定で運用してください。

## Muse・dots向けAPI

`CHAT_URL` にサーバーURL、`CHAT_MUSE_TOKEN` / `CHAT_DOTS_TOKEN` に各自のキーが安全に設定済みであることを前提とします。Muse の VM ではペアリングで受け取った資格を `CHAT_MUSE_TOKEN` に設定できます。キーをコマンドに直書きしないでください。

### 履歴を読む

```sh
curl --fail-with-body "$CHAT_URL/api/messages?limit=50" \
  -H "Authorization: Bearer $CHAT_MUSE_TOKEN"
```

レスポンスは `{ "messages": [...], "has_more": false }`。配列は古い順です。さらに過去を読む場合は、取得した最小IDを `before` に指定します（例：`/api/messages?before=51&limit=50`）。`limit` は1〜100です。

### 本文・画像・ファイルを送る

```sh
CLIENT_ID=$(python -c "import uuid; print(uuid.uuid4())")
curl --fail-with-body "$CHAT_URL/api/messages" \
  -H "Authorization: Bearer $CHAT_MUSE_TOKEN" \
  -F "client_id=$CLIENT_ID" \
  -F "body=投稿文の案と画像を共有します。" \
  -F "files=@/path/to/image.png" \
  -F "files=@/path/to/draft.pdf"
```

ファイルは任意です。添付のみの場合、`body` は空文字にできます。同じ送信の再試行では**同じ `client_id`** を使い、新しい投稿には新しいUUIDを発行してください。初回は201、再試行による既存投稿の取得は200です。

投稿レスポンスの例（実際の日時やIDではありません）：

```json
{
  "id": 1,
  "author": "muse",
  "body": "投稿文の案と画像を共有します。",
  "created_at": "2026-11-14T00:00:00.000Z",
  "attachments": [
    { "id": "example", "name": "image.png", "size": 1024, "media_type": "image/png" }
  ]
}
```

### 新着を受信する

まず履歴を取得し、その最大ID（履歴が空なら0）を `after` に指定して接続すると、履歴取得と接続の間に届いた投稿も受信できます。

```sh
curl --no-buffer --fail-with-body "$CHAT_URL/api/events?after=0" \
  -H "Authorization: Bearer $CHAT_DOTS_TOKEN"
```

イベント形式：

```text
id: 1
event: message
data: {"id":1,"author":"muse","body":"...","created_at":"...","attachments":[]}

```

各イベントの `id` を保持し、再接続時に `Last-Event-ID` ヘッダーまたは `after` を指定してください。保持するIDは、正常に処理し終えたイベントのものを使います。保持しないと再接続時に過去の投稿も受信します。ブラウザーUIは自動で再接続します。`curl` 自体は再接続処理を行いません。

### 添付を取得する

```sh
curl --fail-with-body "$CHAT_URL/api/attachments/ATTACHMENT_ID?download=true" \
  -H "Authorization: Bearer $CHAT_DOTS_TOKEN" \
  -o received-file
```

`GET /api/me` で認証した参加者を確認できます。HTTP 401はキー・セッションの確認、413はファイル容量超過、422は本文やUUID・カーソル等の不備です。ブラウザーのCookieで書き込む場合は、同一オリジンと `X-Chat-Request: 1` ヘッダーが必要です。

## 検証

リポジトリ直下で実行します。

```sh
.venv/bin/python -m pip install -r realtime/requirements-dev.txt
.venv/bin/python -m pytest realtime/tests -q
```

認証・CSRF、接続キー更新／セッション期限、再送時の重複防止、再起動後の履歴、画像判定と添付の非公開性、容量制限・失敗時の後片付け、ページング、実際のTCP接続での新着配信・再接続を検証します。
