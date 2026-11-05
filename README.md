# cubase-midi-remote-api

Claude Code (MCP) から **Cubase / Nuendo** のミキサー・トランスポート・プラグインパラメーターを
操作するためのブリッジです。Cubase 12 以降に搭載の **MIDI Remote API**(JavaScript)を
仮想 MIDI ケーブル経由で橋渡しします。

```
Claude Code
   ⇄  MCP サーバー (Python: mcp + mido + python-rtmidi)      ← このリポジトリ mcp_server/
   ⇄  仮想 MIDI (macOS: IAC Driver Bus 1 / Windows: loopMIDI)
   ⇄  Cubase MIDI Remote スクリプト (JS)                      ← このリポジトリ cubase_script/
   ⇄  Cubase 本体 (MixConsole / Transport / Quick Controls / DirectAccess / Key Commands)
```

双方向にメッセージが流れるので、トラック名・現在値・選択トラックの情報を
Cubase から MCP 側へ SysEx で返せます(プロトコルの詳細は [PROTOCOL.md](PROTOCOL.md))。

## できること / できないこと

できること(MIDI Remote API の範囲):

- トランスポート(再生/停止/録音/サイクル/メトロノーム/早送り/巻き戻し)、ロケーター移動、テンポ設定
- ミキサーバンク 8ch ずつの Volume / Pan / Mute / Solo / Record Arm / Monitor / Send レベル
- バンク移動 + トラック名の読み取り、**トラック名での検索と選択**
- 選択トラックの Quick Controls、フォーカス中の Insert FX パラメーター(Direct Access)
- **Cubase 13.0.50+** では DirectAccess によるパラメーターツリー列挙と個別読み書き
- キーコマンド発火(スクリプトの `KC_SLOTS` テーブルに登録したもの。トラック追加なども可能)

できないこと(API の制限):

- トラックの作成・削除、複数トラックの同時選択、MIDI ノートやアレンジの編集
- プロジェクトファイル (.cpr) の解析(非公開形式のため)
- スクリプトはサンドボックス内で動作するため HTTP・ファイルアクセスは不可

キーコマンドで足りない部分は、Cubase 側のマクロ / PLE プリセットにキーコマンドを割り当て、
それを `KC_SLOTS` に登録すれば橋渡しできます。

## セットアップ (macOS / IAC)

### 1. 仮想 MIDI ポート(IAC Driver)を有効化

1. 「Audio MIDI 設定」を開き、メニューの「ウインドウ > MIDI スタジオを表示」
2. 「IAC Driver」をダブルクリック →「装置はオンライン」にチェック
3. バスが無い場合は「+」で Bus 1 を作成(既定で Bus 1 があります)

確認: `./.venv/bin/python -m mcp_server.cli list-ports` に `IAC Driver Bus 1` が出れば OK。

### 2. MIDI Remote スクリプトをインストール

```bash
./scripts/install_cubase_script.sh
```

`~/Documents/Steinberg/Cubase/MIDI Remote/Driver Scripts/Local/ClaudeCode/MCP_Bridge/ClaudeCode_MCP_Bridge.js`
にコピーされます(環境が異なる場合は手動で同じ場所へ)。

Cubase を起動してプロジェクトを開き、下部ゾーンの **Remote** タブ(MIDI Remote パネル)を開きます。
デバイス欄に **ClaudeCode MCP_Bridge** が自動検出で現れます(既にパネルを開いていた場合は
「Reload Scripts」を押す)。

### 3. Python 環境と MCP 登録

```bash
./scripts/setup_venv.sh          # .venv を作成して依存をインストール
./scripts/register_claude_mcp.sh # .mcp.json を書き出し(リポジトリ直下で claude を起動して読み込む)
```

`register_claude_mcp.sh` はカレントリポジトリ用の `.mcp.json` を書きます。
ユーザー全体で使いたい場合は:

```bash
claude mcp add --scope user cubase -- "$PWD/.venv/bin/python" -m mcp_server
```

### 4. 動作確認

Cubase でトラックを 2〜3 本作り、Claude Code から:

```
Cubase の状態を確認して(cubase_state)、Bass トラックのボリュームを 0.5 にして
```

ターミナルから単体テストする場合:

```bash
./.venv/bin/python -m mcp_server.cli test                       # 実機に対して
CUBASE_MIDI_BACKEND=dummy ./.venv/bin/python -m mcp_server.cli test   # Cubaseなしの模擬応答でテスト
```

トラブルシュート:

- `cubase_ping` が応答しない → MIDI Remote パネルが開いているか、スクリプトがインストール
  されているか、IAC が有効か確認。`cli monitor` で双方向の MIDI を観察できます。
- デバイスが自動検出されない → MIDI Remote パネル右上の「+」から
  `ClaudeCode MCP_Bridge` を手動追加します(IAC Bus 1 を選択)。
- スクリプトを書き換えたら「Reload Scripts」で即反映されます。

## Windows (loopMIDI) の場合

1. [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) をインストールし、ポートを 1 つ作成
2. `cubase_script/Local/` の中身を
   `%USERPROFILE%\Documents\Steinberg\<Cubase or Nuendo>\MIDI Remote\Driver Scripts\Local\` へコピー
3. ポート名に `loopMIDI` が含まれていれば自動検出されます

## MCP ツール一覧

| ツール | 機能 |
|---|---|
| `cubase_ping` | スクリプトとの疎通確認 |
| `cubase_state` | トランスポート + 可視 8 スロット + QC + プラグイン情報 |
| `cubase_transport` | play / stop / record / cycle / metronome / rewind / forward |
| `cubase_locate` | ロケーター移動(例 `2.3.1.0`) |
| `cubase_set_tempo` | テンポ設定 |
| `cubase_set_channel` | slot の volume/pan/mute/solo/record_arm/monitor/select |
| `cubase_set_send` | slot の send レベル |
| `cubase_quick_control` | 選択トラックの Quick Control |
| `cubase_bank` | 8ch ウィンドウの送り(next / prev / shift_left / shift_right / reset) |
| `cubase_select_track` | トラック名(部分一致可)で検索し選択 |
| `cubase_insert_slot` / `cubase_param_bank` | フォーカス FX のスロット/パラメーターページ移動 |
| `cubase_key_command` | キーコマンドスロット 0–5 を発火 |
| `cubase_da_walk` | (Cubase 13+)選択トラックのパラメーターツリー列挙 |
| `cubase_da_set_param` / `cubase_da_get_param` | (Cubase 13+)パラメーターの個別読み書き |
| `cubase_raw_sysex` / `cubase_monitor` | プロトコル直打ち / MIDI モニタ(デバッグ用) |

## リポジトリ構成

```
cubase_script/Local/ClaudeCode/MCP_Bridge/ClaudeCode_MCP_Bridge.js  # Cubase 側スクリプト
mcp_server/                                                          # MCP サーバー
  protocol.py    # CC/SysEx プロトコル実装
  bridge.py      # MIDI I/O・状態管理・テスト用フェイク Cubase
  server.py      # FastMCP ツール定義
  cli.py         # list-ports / monitor / test
scripts/           # セットアップスクリプト
PROTOCOL.md        # プロトコル仕様
```

## 設計メモ

- スクリプトは MIDI Remote API **v1.0**(Cubase 12)で動く構成。DirectAccess(v1.2 =
  Cubase 13.0.50+)は `makeDirectAccess` の有無で機能検出し、無い環境では無効になります
- Volume / Pan は SysEx で 14bit(16383 段階)、CC は 7bit。どちらの経路でも操作できます
- スクリプトは状態変化で即フィードバックを返し、加えて数秒おきにフルスナップショットを
  送るため、MCP 側を後から起動しても状態が追いつきます
- トラック名は日本語を含む場合も UTF-16 コード単位で転送するので文字化けしません
- 設定は環境変数で上書きできます: `CUBASE_MIDI_IN` / `CUBASE_MIDI_OUT` /
  `CUBASE_MIDI_BACKEND` (rtmidi | dummy)

## 参照

- [MIDI Remote API 公式ドキュメント](https://steinbergmedia.github.io/midiremote_api_doc/)
  (API リファレンスと型定義 `midiremote_api_v1.d.ts` 同梱)
- スクリプトの置き場所・Reload 手順は同ドキュメント Getting Started 参照
