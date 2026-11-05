# Cubase GUI AI 操作ブリッジ 設計案

MIDI Remote API ブリッジ(本リポジトリの既存機能)では届かない領域 =
**アレンジ編集・トラック作成/削除・プラグイン選択・各種ダイアログ・Key Editor などの
カスタム UI** を、AI エージェントが直接操作できるようにする仕組みの設計。

- 対象: macOS (Cubase / Nuendo 14 で実機検証済み)
- 日付: 2026-11-05
- 状態: 設計 + Phase 0 (実機プローブ) 完了

---

## 1. 実機検証で確認できたこと

本リポジトリの `.venv` Python から、**起動中の Cubase Pro 14 に対して**以下を確認した。

| # | プリミティブ | 結果 | 確認方法 |
|---|---|---|---|
| 1 | ウィンドウ列挙 (CGWindowList) | ✅ タイトル・座標・サイズ取得可 | `プロジェクト`, `MIDI Remote マネージャー`, Hub などを取得 |
| 2 | ウィンドウ単位スクリーンキャプチャ | ✅ `screencapture -x -o -l <winid>` | プロジェクトウィンドウの PNG を取得(既存 MCP_Bridge パネルも写る) |
| 3 | メニューバー読み取り (AX API) | ✅ 全メニュー取得可 | Apple/Cubase/ファイル/編集/プロジェクト/Audio/MIDI/… |
| 4 | メニュー項目のテキスト読み取り | ✅ メニューを開かずに項目名列挙可 | 「プロジェクト」→ `トラックを追加` など 24 項目 |
| 5 | メニュー実行 (AXPress) | ✅ プログラムから発火可 | 「Cubase Pro について」を開くことに成功 |
| 6 | 合成キーボード (CGEventPostToPid) | ✅ プロセス指定で配送可 | Escape 送信で About ダイアログを閉じた |
| 7 | AXIsProcessTrusted | ✅ 既に許可済み | 端末側に「アクセシビリティ」権限が付与されている |
| 8 | メインウィンドウ内部の AX ツリー | ❌ ほぼ不透明 | 子要素はボタン数個のみ(カスタム描画キャンバス) |
| 9 | ダイアログ内部の AX ツリー | ⚠️ 一部不可 | About 等は子要素ゼロ。標準部品のダイアログは未検証 |
| 10 | `AXEnhancedUserInterface` 強制展開 | ❌ set 不可 (-25208) | Cubase は属性設定を拒否。サブメニューの中身は遅延読み込み |

結論:

- **メニューシステムは完全に操作可能**(読み取り + 実行)。Cubase のメニューはほぼ全機能を
  網羅するため、これが GUI 操作の主経路になる
- **キャンバス(Key Editor / アレンジ領域 / プラグインGUI)は AX では見えない** →
  ここだけ「スクリーンショット + ビジョン + 合成マウス」の領域
- **キーボード合成は確実に届く**(プロセス指定可能なので他アプリを誤爆しない)

## 2. 全体アーキテクチャ

```
AI エージェント (Claude Code / DSH)
 │
 ├─ MCP: cubase (既存) ── 仮想MIDI ── MIDI Remote スクリプト ── Cubase API層
 │    ミキサー / トランスポート / QC / DirectAccess / KC_SLOTS
 │    ★ 高速・決定的・状態フィードバックあり
 │
 └─ MCP: cubase-gui (新規, 本設計) ── OS プリミティブ層
      ├─ AX層    : メニューツリー読み取り / メニュー実行 (AXPress)
      ├─ 入力層  : 合成キーボード (CGEventPostToPid) / 合成マウス
      ├─ 視覚層  : ウィンドウ単位キャプチャ / 差分検出
      ├─ 知識層  : コマンドカタログ (Key Commands.xml 解析) / レシピライブラリ
      └─ ガード層: 対象プロセス固定 / クリップ領域制限 / モーダル監視
```

原則: **レイヤー L0 → L3 の順に手段を選び、GUI は「API で届かない所だけ」使う。**

| レイヤー | 手段 | 速度 | 確実性 | 用途 |
|---|---|---|---|---|
| L0 | MIDI Remote ブリッジ (既存) | 高 | 最高 | ミキサー値・トランスポート・DA パラメーター |
| L1 | 合成キーボード(キーコマンド) | 高 | 高 | トラック追加、ズーム、保存、PLE 適用など |
| L2 | メニュー AX 読み取り + AXPress | 中 | 高 | L1 に割り当てがないコマンド、メニューでしか開けないダイアログ |
| L3 | スクショ + ビジョン + 合成マウス | 低 | 中 | プラグインセレクタ、Key Editor、MediaBay 等のカスタム UI |

「GUI で **開く/選ぶ** だけして、**値の設定は L0 に戻る**」のが定石。
(例: プラグインは L3 で選択 → パラメーターは `cubase_da_set_param` で設定)

## 3. cubase-gui MCP ツール設計

新規パッケージ `gui_agent/`(Python, pyobjc)。FastMCP サーバーとして既存 `mcp_server/` と並行登録。

| ツール | 機能 | 実装メモ |
|---|---|---|
| `gui_windows` | Cubase ウィンドウ一覧 (タイトル/ID/bounds/最前面) | CGWindowList。他の Space のものも `--all` で取得 |
| `gui_menu_tree` | メニュー構造のテキスト取得(部分木指定可) | タイトル一致。サブメニューは遅延読み込みを考慮 |
| `gui_menu_press` | メニューパス実行 例 `["プロジェクト","トラックを追加","Audio"]` | 各階層で AXPress(サブメニュー展開)→ 葉で AXPress。部分一致可 |
| `gui_key` | キー合成 例 `cmd+t`, `escape`, `shift+cmd+s` | CGEventPostToPid。送信前に frontmost 確認 |
| `gui_type` | テキスト入力(名前変更・検索欄など) | Unicode 対応 |
| `gui_screenshot` | ウィンドウ画像を取得 → エージェントへ返す | `screencapture -l`。スケール/JPEG 品質指定可 |
| `gui_click` / `gui_drag` / `gui_scroll` | 座標操作(ウィンドウ相対ポイント指定) | ウィンドウ bounds 外は拒否 |
| `gui_frontmost` / `gui_activate` | 前面確認 / アクティブ化 | キー送信前のガードに使う |
| `gui_diff` | 直前スクショとの差分(変化率) | 操作後の UI 変化検出(ビジョン不要の安価な検証) |
| `gui_recipe_run` / `gui_recipe_save` | レシピ(YAML 手順)の実行・保存 | §5 参照 |

ポイント:

- **座標系**: スクショは物理ピクセル(Retina 2x)、CGEvent はポイント。ツール層で統一し、
  エージェントにはウィンドウ相対ポイントだけを見せる(画像に幅/高さのメタデータを添付)
- **複数ディスプレイ**: ウィンドウの絶対位置から対象ディスプレイを解決してから投稿する
- **エージェントは画像を読める**(この harness の Read 相当)ので、ビジョンは
  「外部モデル」ではなくエージェント自身が担う。MCP はプリミティブ提供に徹する

## 4. エージェントの行動ループ

```
Plan   : タスクを分解。「L0 で済む部分」「L1/L2 で済む部分」「L3 が必要な部分」に仕分け
Act    : 最も低いレイヤーから実行
Verify : (a) L0 フィードバック (SysEx 現在値)  (b) gui_diff  (c) スクショ再取得
         の順で結果を確認。失敗なら座標/手順を補正して再試行(上限 2〜3 回)
Learn  : 成功した手順をレシピとして保存 → 次回はビジョン不要の決定的実行
```

### 動作例: 「Bass トラックを追加して EQ を入れて 100Hz +3dB」

| 手 | レイヤー | 具体操作 |
|---|---|---|
| 1 | L1 | `gui_key "shift+cmd+n"` 等のキーコマンド、または `gui_menu_press ["プロジェクト","トラックを追加","Audio"]` |
| 2 | L1 | 追加ダイアログ名前欄へ `gui_type "Bass"` → `gui_key "return"` |
| 3 | L0 | `cubase_select_track "Bass"` |
| 4 | L3 | Insert スロットをクリック → プラグインセレクタのスクショを取得 → "Frequency" をクリック(ここだけビジョン) |
| 5 | L0 | `cubase_da_walk` → パラメーターツリー列挙 → `cubase_da_set_param` で 100Hz band gain +3dB |
| 6 | L0 | `cubase_da_get_param` で読み戻して完了確認 |

ビジョンは手 4 の一度きり。値の精度は L0 が保証する。

## 5. レシピ(手順メモリ)

UI の見た目に依存する手順は**学習して保存**し、2 回目以降は視覚なしで再生する。

```yaml
# gui_recipes/add_audio_track.yaml
name: add_audio_track
version: 1
target_ui: "Cubase 14 (ja)"
steps:
  - action: menu_press
    path: ["プロジェクト", "トラックを追加", "Audio"]
  - action: wait_window   # ダイアログ出現を待つ(タイトル部分一致)
    title: "オーディオトラック追加|Audio Track"   # 正規表現
    timeout_ms: 3000
  - action: type          # 名前欄はフォーカス済み前提(レシピ作成時に確認)
    text: "{name}"
  - action: key
    key: return
verify:                 # 実行後の検証(どちらか成功でOK)
  - midi: { tool: cubase_state, expect_track_name: "{name}" }
on_fail:
  - action: key
    key: escape
```

- 変数 `{name}` はエージェントが実行時に埋める
- 失敗時の回復手順(`on_fail`)をレシピに含める(Esc でダイアログ閉じ等)
- Cubase の UI 言語・バージョン差は `target_ui` とメニュー部分一致で吸収

## 6. コマンドカタログと PLE プリセット生成(テキスト裏道)

### 6.1 キーコマンドカタログ

`~/Library/Preferences/Cubase 14/Key Commands.xml` に **全コマンドのカテゴリ/名称が
テキストで存在する**(実機確認済み)。これをパースしてコマンドカタログを生成すれば、
エージェントは「何がキーコマンドでできるか」を Vision なしで把握できる。

- カタログは初回セットアップ時に JSON 化してリポジトリに同梱
- メニュー項目とカタログを突き合わせ、`gui_key` と `gui_menu_press` のどちらで発火するかを
  エージェントが選べるようにする

### 6.2 PLE / Logical Editor プリセットの自動生成

ノート/イベントの**一括変換**は、マウスで編集するより PLE プリセット生成が圧倒的に正確:

1. エージェントが PLE プリセット XML を生成
   (配置先: `~/Library/Preferences/Cubase 14/Presets/Logical Edit(or)/`。Cubase 13 フォルダでは
   `Logical Edit` / `Project Logical Editor` ディレクトリを確認済み)
2. 範囲選択はキーコマンド(`範囲選択`系)で行う
3. 「論理エディタプリセットを適用」コマンドを発火(メニュー or キーコマンド)

制約: 新規プリセットが Cubase に認識されるタイミング(ダイアログ再オープン/再起動)は
Phase 1 で要検証。解決しない場合は「既存プリセット上書き」戦略に切り替える。

## 7. 安全設計

| リスク | 対策 |
|---|---|
| キー/クリックが他アプリに飛ぶ | **CGEventPostToPid でプロセス固定**。加えて送信前 frontmost チェック(設定で無効化可) |
| Cubase ウィンドウ外をクリック | bounds 外座標をツール層で拒否 |
| モーダルで固まる | 各操作後に `gui_windows` で変化を監視。タイムアウトで `escape` を送る監視ループ |
| 破壊的操作(削除・保存せず閉じる・レンダリング) | 「破壊的コマンドリスト」を持ち、`gui_menu_press` は一致時に実行確認を返す(エージェントがユーザーに確認) |
| ユーザーの手操作と競合 | 操作中はユーザーに告知。可能ならエージェント実行前にアクティブ化、終了時に復帰 |
| アンドゥ不可の操作 | プロジェクト保存系コマンドはユーザー確認必須とする |

## 8. 段階的実装計画

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | 実機プローブ: AX/CGEvent/キャプチャの可否検証 + `scripts/probe_gui.py` | ✅ 完了 |
| 1 | `gui_agent/` パッケージ化 + MCP `cubase-gui` 登録。L1/L2(キー・メニュー・ウィンドウ)実装。ビジョン不要で「トラック追加/削除、テンポ・拍子、マーカー、PLE 適用、設定ダイアログ」まで到達 | 次の一手 |
| 2 | L3 ビジョンループ: `gui_screenshot` / `gui_click` / `gui_diff`。プラグインセレクタと Key Editor を担当 | 未 |
| 3 | レシピライブラリ整備 + Key Commands.xml からのコマンドカタログ生成 + L0 併用検証の自動化 | 未 |

Phase 1 の受け入れ基準: 「Claude Code に『Audio トラックを 2 本追加して名前を付ける』と
指示して、ビジョンなしで完遂できる」。Phase 2 の受け入れ基準: 「選択トラックに
Frequency を挿入でき、DA でゲインを設定できる」。

## 9. 既知の限界・リスク

- **AX で子要素が取れないウィンドウ**(About 等)がある → キーボード(Esc/Tab/Space)と
  座標クリックで拾う。標準ダイアログ(開く/保存)は AX 効く可能性が高い(Phase 1 で測定)
- **サブメニューは遅延読み込み**: 中身は実際に展開した瞬間にしか読めない。
  `gui_menu_press` は階層を順に AXPress しながら読む設計で回避
- **Cubase 更新でメニュー文言が変わる** → レシピはタイトル部分一致 + カタログ再生成で追従
- **スクショは 1 枚 0.3〜1 秒・数十 KB〜** → レシピ/AX/キーで済む場面では使わない。
  差分検証は `gui_diff`(低解像度)で行いフル解像度は座標決定時のみ
- **速度**: ビジョンループは 1 アクション数秒。リアルタイム性が要る場面(再生中の操作)は
  L0/L1 を使う
- **Cubase のフォーカス**: フローティングウィンドウや最前面のダイアログでキー配送先が
  変わる → `gui_frontmost` + ウィンドウタイトル確認を毎ステップ挟む

## 10. Windows 展開の指針

設計はそのままに、OS プリミティブ層だけ差し替え:

| macOS | Windows |
|---|---|
| CGWindowList / `screencapture -l` | Win32 API (EnumWindows) / `pyautogui.screenshot` |
| AX API (メニュー読み取り/AXPress) | UI Automation (UIA) — pywinauto。Win 版 Cubase はメニューが UIA で取れる可能性が高い |
| CGEventPostToPid | SendInput / PostMessage(フォアグラウンド制約あり) |

macOS 版を先行実装し、プリミティブをインターフェース化しておけば移植は局部で済む。

## 11. 参照

- `scripts/probe_gui.py` — 本設計の検証に使ったプローブ CLI(Phase 0 成果物)
- `README.md` / `PROTOCOL.md` — 既存 MIDI Remote ブリッジ(L0)
- MIDI Remote API 公式ドキュメント: https://steinbergmedia.github.io/midiremote_api_doc/
