# MCP Bridge プロトコル仕様

Claude Code (MCP サーバー / Python) ⇄ Cubase MIDI Remote スクリプト (`ClaudeCode_MCP_Bridge.js`) 間の MIDI プロトコル定義。

- MIDI チャンネル: **0** (Cubase 画面上では ch 1)
- 仮想 MIDI ポート: macOS は IAC Driver Bus 1 / Windows は loopMIDI

---

## 1. CC マップ (Cubase への入力 / Cubase からのフィードバック)

スクリプトは同じ CC 番号を入力バインドと出力フィードバックの両方に使います。
MCP → Cubase で送ると操作、Cubase → MCP で返ってくるのが現在値です。

### ミキサーバンク (slot 0〜7 = ウィンドウ内の 8 チャンネル)

| CC         | 機能                          | 値                                    |
|------------|-------------------------------|---------------------------------------|
| 0–7        | slot 0–7 **Volume**           | 0–127 (7bit)                          |
| 16–23      | slot 0–7 **Pan**              | 0–127 (64 = center)                   |
| 24–31      | slot 0–7 **Mute**             | 127 = トグル押下                      |
| 32–39      | slot 0–7 **Solo**             | 127 = トグル押下                      |
| 40–47      | slot 0–7 **Record Arm**       | 127 = トグル押下                      |
| 48–55      | slot 0–7 **Monitor Enable**   | 127 = トグル押下                      |
| 56–63      | slot 0–7 **選択** (Selected)  | 127 = トグル押下                      |
| 64–71      | slot 0–7 **Send 1** レベル    | 0–127                                 |
| 72–79      | slot 0–7 **Send 2** レベル    | 0–127                                 |

### バンク / トラック操作

| CC | 機能                    |
|----|-------------------------|
| 8  | Bank Prev               |
| 9  | Bank Next               |
| 10 | Bank Shift Left         |
| 11 | Bank Shift Right        |
| 12 | Bank Reset              |
| 13 | 選択トラック Prev        |
| 14 | 選択トラック Next        |

### トランスポート (momentary: 127 → すぐ 0 を送ること)

| CC | 機能       |
|----|------------|
| 89 | Play       |
| 90 | Stop       |
| 91 | Record     |
| 92 | Cycle      |
| 93 | Metronome  |
| 94 | Rewind     |
| 95 | Forward    |

### 選択トラック / フォーカス付きプラグイン

| CC    | 機能                                                        |
|-------|-------------------------------------------------------------|
| 96–103| Quick Controls 1–8 (選択トラック)                           |
| 104–111| フォーカス中の Insert FX パラメーター 1–8 (Direct Access)  |
| 80    | Insert FX スロット Prev (フォーカスビューアー)              |
| 81    | Insert FX スロット Next                                     |
| 82    | パラメーターバンク Prev                                     |
| 83    | パラメーターバンク Next                                     |
| 112–117| キーコマンドスロット 0–5 (スクリプト内 `KC_SLOTS` テーブル) |

---

## 2. SysEx プロトコル

形式: `F0 7D <cmd> <payload...> F7` (0x7D = 教育用/非商用メーカー ID)

14bit 値は 2 バイト (big-endian, 各 7bit)、21bit 値は 3 バイト。
文字列は `[mode][unitLen][データ...]` 形式:

- `mode = 0`: ASCII (unit = 1 バイト)
- `mode = 1`: UTF-16 コード単位 × 21bit (3 バイト)。日本語のトラック名も損失なく転送できる

### 2.1 MCP → Cubase (コマンド)

コマンド ID は SysEx データバイトとして流れるため **すべて 0x7F 以下**です。
`0x01–0x1F` = MCP → Cubase、`0x20–0x3F` = Cubase → MCP。

| cmd  | 名前            | payload                                    | 概要 |
|------|-----------------|--------------------------------------------|------|
| 0x01 | SELECT_SLOT     | slot                                       | slot のチャンネルを選択 |
| 0x02 | KC_SLOT         | slot                                       | キーコマンドスロット 0–5 を発火 |
| 0x03 | SET_VOLUME      | slot, volHi, volLo                         | Volume (14bit, 0–16383) |
| 0x04 | SET_PAN         | slot, valHi, valLo                         | Pan (14bit, 8192 = center) |
| 0x05 | SET_SEND        | slot, sendIdx, valHi, valLo                | Send n レベル (14bit) |
| 0x06 | SET_MUTE        | slot, on(0/1)                              | Mute 状態を直接指定 |
| 0x07 | SET_SOLO        | slot, on(0/1)                              | Solo 状態を直接指定 |
| 0x08 | SET_RECARM      | slot, on(0/1)                              | Record Arm を直接指定 |
| 0x09 | SET_MONITOR     | slot, on(0/1)                              | Monitor を直接指定 |
| 0x0A | SET_QC          | idx, valHi, valLo                          | 選択トラックの Quick Control (14bit) |
| 0x0B | LOCATE          | 文字列 (LOC_FORMAT 参照)                   | ロケーター移動 (例 `2.3.1.0`) |
| 0x0C | SET_TEMPO       | bpm10Hi, bpm10Lo                           | テンポ設定 (BPM×10, 14bit) |
| 0x0D | DA_WALK         | limitHi, limitLo                           | DirectAccess ツリー列挙 (Cubase 13+) |
| 0x0E | DA_SET          | seq, tagHi, tagMid, tagLo, valHi, valLo    | 列挙済みオブジェクトのパラメーター設定 (process value 14bit) |
| 0x0F | DA_GET          | seq, tagHi, tagMid, tagLo                  | パラメーター値の読み出し |
| 0x10 | BANK            | step (0=next,1=prev,2=shiftL,3=shiftR,4=reset) | バンク移動 (SysEx 経由) |
| 0x11 | PING            | —                                          | 接続テスト → 0x20 IDENT 応答 |
| 0x12 | SNAPSHOT        | —                                          | 全状態の再送を要求 |

> LOC_FORMAT で位置文字列はスクリプトへ `[mode][len][ascii...]` で渡す (常に ASCII モード)。

### 2.2 Cubase → MCP (フィードバック)

| cmd  | 名前        | payload                                             | 発火タイミング |
|------|-------------|-----------------------------------------------------|----------------|
| 0x20 | TRANSPORT   | flags, tempo10Hi, tempo10Lo, LOC 文字列, FORMAT 文字列 | 変化時 + ハートビート |
| 0x21 | SLOT        | slot, flags, volHi, volLo, panHi, panLo, 名称文字列  | 変化時 + ハートビート |
| 0x22 | QC          | idx, valHi, valLo                                    | 変化時 + ハートビート |
| 0x23 | PLUGIN      | role, 名称, ベンダー, バージョン (各文字列)          | プラグイン変化時 |
| 0x24 | DA_PARAM    | seq, tag(21bit), valHi, valLo                        | DA 監視中のパラメーター変化 |
| 0x25 | DA_OBJ      | walkId, seq, parentSeq, 型名文字列, タイトル文字列   | DA_WALK 応答 |
| 0x26 | DA_PARAM    | walkId, seq, tag(21bit), valHi, valLo, タイトル, 表示値 | DA_WALK 応答 |
| 0x27 | DA_DONE     | walkId, countHi, countLo                             | DA_WALK 完了 |
| 0x6E | IDENT       | 識別文字列                                           | ページ有効化時 / SNAPSHOT 時 |

**TRANSPORT flags ビット**: `bit0 = 再生中 (mStart)`, `bit1 = 録音中 (mRecord)`, `bit2 = Cycle`, `bit3 = Metronome`

**SLOT flags ビット**: `bit0 = Selected`, `bit1 = Mute`, `bit2 = Solo`, `bit3 = Record Arm`, `bit4 = Monitor`, `bit5 = チャンネルあり (名称が非空)`

**PLUGIN role**: `1 = 選択トラックのインストゥルメント`, `2 = フォーカス中の Insert FX`

---

## 3. DirectAccess (DA_WALK) の使い方

1. MCP が `DA_WALK` を送る
2. スクリプトは「選択トラックのミキサーチャンネル」をルートにして子オブジェクトを BFS で列挙し、
   各 `DA_OBJ` に連番 `seq` を振る。パラメーターは各オブジェクトにつき最大 `DA_MAX_PARAMS_PER_OBJECT` 個
3. 完了時に `DA_DONE` を送る
4. MCP は `seq` + `parameterTag` をキーに `DA_SET` / `DA_GET` で読み書きする

制限:

- DirectAccess は API v1.2 = **Cubase / Nuendo 13.0.50 以降**。Cubase 12 では DA_WALK 系は無効 (スクリプトが機能検出して無視します)
- アレンジ編集・トラック作成/削除・MIDI ノート編集は MIDI Remote API の範囲外
- キーコマンドは `KC_SLOTS` テーブル (スクリプト冒頭で編集) 経由で発火できる
