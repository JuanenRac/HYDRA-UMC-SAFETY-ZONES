<p align="center">
  <img src="images/HYDRA_UMC_BANNER.svg" alt="HYDRA-UMC-SAFETY-ZONES banner" width="100%">
</p>

# 🛡️ HYDRA-UMC-SAFETY-ZONES

<p align="center"><a href="README.md">🇺🇸 English</a> | <a href="README_spa.md">🇪🇸 Español</a> | <a href="README_fra.md">🇫🇷 Français</a> | <a href="README_ita.md">🇮🇹 Italiano</a> | <a href="README_deu.md">🇩🇪 Deutsch</a> | <a href="README_zho.md">🇨🇳 简体中文</a> | 🇯🇵 <b>日本語</b> | <a href="README_por.md">🇵🇹 Português</a></p>

### 🚨 ゾーン侵入と E-STOP 要求のロジック(v0:検出器も物理 E-STOP もまだない)

<p align="center">
  <img src="https://img.shields.io/badge/Licencia-GPL%203.0-blue.svg" alt="GPL 3.0">
  <img src="https://img.shields.io/badge/Safety-ISO%2013849--1%20aspirational-red.svg" alt="Safety">
  <img src="https://img.shields.io/badge/Stage-Functional%20v0-yellow.svg" alt="Functional v0 stage">
</p>

---

> **安全性とセキュリティの現状:** このエコシステムは認証された安全システムではありません。ハードウェアの E-STOP 経路、ファームウェアが強制する制限やインターロック、検証済みの動作コマンドはまだありません。ファームウェアイメージは CRC32 と共有鍵の HMAC-SHA256 で検査しており、非対称署名ではありません。TLS と相互 TLS は任意で、既定では無効です。独自基板はまだ実装も試験もされていません。ここに書かれた内容を人の安全のために頼る前に、[セキュリティベースライン](https://github.com/JuanenRac/HYDRA-UMC-DOCS/blob/main/docs/SECURITY_BASELINE.md)と[脅威モデル](https://github.com/JuanenRac/HYDRA-UMC-DOCS/blob/main/docs/THREAT_MODEL.md)をお読みください。

## 📋 ひと目で

| | |
|---|---|
| バージョン | `0.1.5` |
| 成熟度 | 機能する |
| 技術 | Python / 3D vision / Hailo-8 |
| 対象 | HYDRA-UMC セルの Raspberry Pi CM5 |
| 実機での確認 | まだです：このバージョンについて実機での動作記録はありません |

## 🚦 プロジェクトの状況

各行はこのリポジトリの一部分で、信号は今日どこまで進んでいるかを示します。計画ではなく、リポジトリが示せるもの（テスト、ビルド、実行）に基づいて書かれています。

🟢 完了・検証済み · 🟡 動作する（既知の制限あり） · 🟠 一部のみ、または未検証 · 🔴 未実装または動作しない

| 領域 | 状態 | ここでの意味 |
|---|---|---|
| ソフトウェア | 🟡 | 主な目的では動作します。足りない点は上の正直さの確認に書かれています |
| テスト | 🟠 | このリポジトリのテスト件数はまだ記録されていません |
| 実機 | 🔴 | まだです：このバージョンについて実機での動作記録はありません |
| ドキュメント | 🟢 | 8 言語の README。変更のたびにマニフェストと照合されます |

## 1. 🛠️ 技術概要

**HYDRA-UMC-SAFETY-ZONES** は、Vision AI Node ファミリーの重要な安全サブ
システムとなることを目指しています。その想定される役割は、ロボットの周囲に仮想的な
3D バウンディングボリュームを投影し、Hailo-8 NPU による高速な空間セグメン
テーション(計画中。このパイプラインはまだ存在しません)を用いて、定義された「警告」および「危険」ゾーン内での人の侵入
や異物の侵入を検知することです。

これは、ファミリーの統合親プロジェクトである **[HYDRA-UMC-VISION-NODE](https://github.com/JuanenRac/HYDRA-UMC-VISION-NODE)** の 4 つの子プロジェクトの 1 つであり、その知覚入力は兄弟プロジェクトである
**[HYDRA-UMC-DETECTION-HEF](https://github.com/JuanenRac/HYDRA-UMC-DETECTION-HEF)** がコンパイルしたモデル上に構築されています。

### 要点

* 🚦 **多段階ゾーン（v0）：** 軸に沿った 3D ボリューム上の実際の `Zone`/`ZoneLevel`（警告/危険）定義、およびゾーン集合と検知対象位置集合の間の実際の越境チェック（`check_breaches`）。
* 🛑 **E-STOP リクエスト（v0、実行はしない）：** 最悪の越境が危険レベルであるすべての対象について、実際の `EStopRequest` が生成され、`EStopRequester` に渡されます——ここで物理的な停止を自ら実行するものが一切ない理由については、下記の設計上の境界を参照してください。
* 🔒 **キャリブレーション鮮度の強制（v0）：** すべてのゾーン集合はオプションの `calibration`（バージョン、ソース、キャリブレーション日、最大許容日数）を持ちます。`evaluate_safety()` は越境ロジックを実行する**前に**それをチェックします——キャリブレーションが全く無いゾーン集合、自身の宣言した `max_age_days` より古いもの、あるいは未来の日付のものは、常に `INHIBITED` に解決され、検知対象がどのゾーンにも近づいていないという理由だけで黙って `READY` に流れ込むことは決してありません。
* 👁️ **観測者健全性の強制(v0):** `evaluate_safety()` はオプションの `observation` ステータス(有効/無効、最終観測時刻、自身のエラー)も受け付けます—— [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) 参照。観測エビデンスが全くない、観測者が無効化されている、観測者が内部エラーを報告している、または観測が古すぎる場合はすべて、越境ロジックが実行される前に `INHIBITED` に解決されます——キャリブレーション欠如とまったく同じです: クラッシュした検知器や起動直後の状態が、対象が報告されなかったというだけで黙って「確認済みでクリア」と読まれることは決してありません。
* 🧮 **有限座標フェイルセーフ(v0):** `config.py` はゾーンまたは検知ファイル内の `NaN`/`Infinity`/`-Infinity` の `x`/`y`/`z` を、`evaluate_safety()` が実行される前に拒否し、実在の点を表せない座標で境界評価を行う代わりに直接 `INHIBITED`(終了コード `3`)に解決します。
* 🧱 **NaN がゾーンを無効にすることはありません（ライブラリでも入力端でも）：** `Point3D` は有限でない座標を拒否します（NaN のゾーン角は `contains()` を常に偽にし、決して作動しないゾーンになっていました）。`AABB.expanded()` と `velocity_margin_m()` は NaN や無限大のマージン・速度を拒否し（NaN の速度はすべてのゾーンの周りに NaN の箱を作っていました）、`DetectedObject` は NaN の位置を持てません。*（実装済み）*
* 🧍 **物体には大きさがあります：** `DetectedObject.radius_m`（JSON `radiusM`、既定 0）で物体は球になり、球が触れるすべてのゾーンを侵害します。中心がゾーンの外にあっても腕が入り込む人は侵害として扱われます（`AABB.intersects_sphere`。`contains()` と同様に境界を含みます。半径 0 は従来の点の挙動と完全に同じです）。*（実装済み）*
* 🌐 **JSON/HTTP API(v0.0.7):** `serve` サブコマンドは `check` とまったく同じロジック(`evaluate_safety()`/`check_breaches()`/`request_estop_for()`)を、stdlib の `http.server`(`POST /check`、`GET /stats`)経由でCLI以外の呼び出し元にも公開します。デフォルトはループバックのみで、`systemd/hydra-umc-safety-zones.service` ユニットと同じです。すべての実コマンド・フラグ・終了コードは [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) を参照してください。
* 📏 **速度に比例する安全エンベロープ（v0.1.0、最初の上限付きバージョン）：** `POST /check` は任意の `toolVelocityMps`（実際のヘッド/ツール速度、メートル/秒）を受け付けます。各ゾーンの静的ボリュームは、その速度に比例する余白だけ拡大され、固定の最大値で頭打ちになります。設定された静的ボリュームは常に無条件の下限です。速度を省略した場合やゼロの場合、各ゾーンはバイト単位で変わらず、スケーリングはゾーンを大きくするだけで、決して小さくしません。
* 📐 **任意の ISO 13855 / ISO 13857 分離距離モデル：** ゾーンファイルに `"separation": {"approach_speed_mps": K, "total_stop_time_s": T, "intrusion_distance_m": C}`（任意で `tool_deceleration_mps2`）を宣言できます。するとゾーンは `S = K·T + C` に、機械が停止する間にツールがなお移動する距離（`v·T`、減速度を指定した場合は `v²/2a`）を加えた分だけ広がります（上限 10 m）。すべての数値はセル自身のリスクアセスメントに基づき、何も仮定しません。線形マージンは保守的な下限として残るため、モデルはゾーンを広げることしかできず、モデルがあれば速度が送られなくてもゾーンは広がります。*(実装済み、宣言しない限り無効)*
* 🔐 **サーバーは自分のルールを持てます（v0）：** `serve --zones-file PATH` でゾーンとキャリブレーションをサーバー側に固定します（独自のゾーンを送るリクエストは拒否され、すべての判定にファイルの `configSha256` が付きます）。`--max-observation-age-seconds` と `--max-calibration-age-days` は、リクエストが自らの根拠を有効と主張できる期間に上限を設けます。`--token` / `SAFETY_ZONES_TOKEN` で `POST /check` に bearer トークンを要求できます。本文を予告して何も送らないクライアントは 10 秒後に切断されます。`--zones-file` がなければ、ポートに到達できる者がゾーンを決めるため、loopback に留めるかトークンの背後に置いてください。すべての JSON 入力で NaN と Infinity は拒否され、タイムゾーンのない時刻は拒否され、ゾーンのないセルは受け付けられず、CLI が読めないものはすべて `INHIBITED`（終了コード `3`）で、終了コード `1` にはなりません。*（実装済み）*
* 📐 **動的オクルージョン（計画中）：** ロボット自身の構造を安全トリガーから自動的にマスクし、ロボットが「自分自身」を侵入として検知しないようにします。
* 🔍 **異物検知（計画中）：** 作業スペースに残された工具や破片を識別します。
* 🎥 **Hailo-8 による実際の 3D 占有マッピング（計画中）：** v0 の `check` サブコマンドが検知対象位置を JSON ファイルから取得するのは、まさにそれらを実際に生成する Hailo-8 空間セグメンテーションパイプラインがこの環境にまだ存在しないためです——詳細は下記「正直な現状確認」を参照してください。
* 🧩 **独立したプロジェクトとして存在する理由：** 安全ロジックは他の知覚処理とは異なる検証基準を持ちます——独自のサービスとして切り離すことで、ファミリー内の他のカメラパイプラインやモデルコンパイルの変更とは独立して、テスト、監査、そして最終的な認証（上記の ISO 13849-1 バッジ参照、現段階では目標に過ぎません）が可能になります。

**既に決定され、今やコード上でも実施されている重要な設計上の境界：** 本プロジェクトは常に E-STOP を
**検知しリクエストするだけ**であり——物理的な停止信号を自ら発することは
決してありません。`estop.py` の唯一の実際のリクエスト実装である
`NullEStopRequester` は、送信するはずだった内容を記録するだけで、どこにも
何も送信しません——本リポジトリには意図的に、まだ実際の CAN 伝送手段が
存在しません。CAN 経由で実際にモーター電源を切断するのは
[HYDRA-UMC](https://github.com/JuanenRac/HYDRA-UMC)（ファームウェア）の責任であり、その役割のために構築されたハード
ウェア上で行われます。この境界を維持することで、本 Python サービス内の
バグが停止を*リクエストできない*結果になることはあっても、ファームウェア
が独立して停止を実行することを*妨げる*ことは決してありません。 現在、ファームウェアには受信すべき E-STOP フレームがなく、`NullEStopRequester` は何も送信しません。この多層防御は設計上のものであり、相手側にはまだ存在しません。

**正直な現状確認 —— 今日実際に動くもの：** 実際のエントリポイント
（`src/hydra_umc_safety_zones/main.py`）は、引数なしで呼び出された場合は
これまで通り識別情報・バージョン・役割を表示しますが、今では実際の
`check --zones パス --detections パス [--observation パス]` サブコマンドも備えています：
JSON からゾーン集合（ゾーン + オプションのキャリブレーションメタデータ）、
検知対象位置、そしてオプションの観測ステータスを読み込み、まずキャリブレーションの
鮮度をチェックしてから、観測者の健全性をチェックします（観測ステータスが
全くない、無効化/エラー/古すぎる観測者は、いずれもキャリブレーション欠落と
まったく同じように `INHIBITED` に解決されます）。その後、
実際の越境チェックを実行し、危険レベルの越境ごとに E-STOP をリクエストし、
結果に応じて 0（Ready）/ 1（Warning）/ 2（Danger、E-STOP をリクエスト済み）
/ 3（Inhibited——キャリブレーションが欠落/期限切れ、または観測者が欠落/
無効化/古すぎる/エラー）で終了します。
本当にまだ実際には存在しないもの：実際のハードウェア上
でこれらの検知対象位置を生成する Hailo-8 セグメンテーション、自己遮蔽
マスキング、`--observation` がその生存状態を報告する実際の観測者プロセス、
そして E-STOP リクエストのための実際の CAN 伝送手段です。
実際に出荷済みの内容は
[`CHANGELOG.md`](CHANGELOG.md) を、まだ残っている作業は下記の「現在の
状況と次のステップ」セクションを参照してください。

---

## 2. 🔄 目標安全ロジックフロー

下図は、本プロジェクトが構築を目指している目標データフローです。JSON
ファイルから読み込んだ検知対象位置を起点として、図中の `CAL`（キャリブ
レーションチェック）、`OBS`（観測者健全性チェック）、`ZONE`（ゾーンチェック）とそれに続く警告/危険の
分岐は、`evaluate_safety()`（`check_breaches()`/`request_estop_for()` を
ラップ）によって駆動され、今日すでに実際に動作しています。
`CAL`/`OBS`/`ZONE` より前（実際の Hailo-8 パイプライン、および `OBS` がその生存状態をチェックする実際の観測者プロセス）と `STOP` より後（実際の CAN
伝送手段）はすべて、まだ今後の課題です。

```mermaid
flowchart TB
    DET["Object Detection (Hailo-8) - 計画中"] --> SEG["Spatial Segmentation - 計画中"]
    SEG --> MAP["3D Occupancy Map - 計画中"]
    MAP --> CAL{"Calibration Fresh? - 実際の v0"}
    CAL -- No --> INHIBIT["INHIBITED - 実際の v0（フェイルセーフ）"]
    CAL -- Yes --> OBS{"Observer Active & Fresh? - 実際の v0"}
    OBS -- No --> INHIBIT
    OBS -- Yes --> ZONE{"Zone Check - 実際の v0"}
    ZONE -- Warning --> SLOW["Velocity Scaling Command - 計画中"]
    ZONE -- Danger --> STOP["CAN E-STOP Request - 実際の v0（リクエストのみ）"]
    SLOW --> CAN["HYDRA CAN Bus - 計画中"]
    STOP --> CAN
```

---

## 3. 🧠 高度な技術情報

### 検知と執行の境界、そしてなぜここで特に重要なのか

本 README のすべての内容の中で、これは単なる実装の詳細ではない唯一の設計
決定です。本サービスは停止が必要であると*判断*し、CAN 経由でそれを
*リクエスト*することを意図していますが、実際の物理的なモーター電源切断は
[HYDRA-UMC](https://github.com/JuanenRac/HYDRA-UMC) 内部の、その役割の
ために構築・認証されたハードウェア上で発生します。これは意図的な多層防御
の選択です——ここでのソフトウェアのバグ（クラッシュ、ハング、不正な
フレーム）は「新しい停止リクエストが送信されなくなる」という結果に留まり、
「ロボットの既存の安全ハードウェアが機能しなくなる」という結果には
なりません。

### なぜ `hardware/`、`firmware/`、`os/`、`models/` がここに存在しないのか

CM5 + Hailo-8 は市販のハードウェアであり、独自に設計する基板はありません。
そのため——Vision AI Node ファミリーの他のプロジェクトと同様に——ここに
`hardware/`/`firmware/` フォルダは存在しません。`os/`（共有 HydraOS
イメージ）と `models/`（実際に NPU に配信される、コンパイル済みの `.hef`
ファイル）は、CM5 ホストイメージと Hailo-8 デバイスハンドルを保持する
統合親プロジェクト [HYDRA-UMC-VISION-NODE](https://github.com/JuanenRac/HYDRA-UMC-VISION-NODE) にのみ存在します。

### 既に行われた設計上の決定

* **バージョンはハードコードではなく、インストール済みパッケージのメタデータから読み取られます** —— `main.py` は 2 つ目の `__version__` 文字列の代わりに `importlib.metadata.version("hydra-umc-safety-zones")` を呼び出すため、`bump_version.py` が編集すべき箇所は常に 1 か所です。
* **オドメーター式のインクリメントは自動的に `PATCH`/`MINOR` にのみ触れます** —— `bump_version.py` は `PATCH` が 9 を超えると `MINOR` に、`MINOR` が 9 を超えると `MAJOR` に繰り上がりますが、`MAJOR` 自体を自動で増加させることは決してありません。`HYDRA-UMC-EDITOR-URDF/bump_version.py` および `HYDRA-UMC-SUITE/bump_version.py` と同じ慣例です。
* **メッシュや凸包ではなく AABB ゾーン** —— `check_breaches()` を正確かつ高速に保てる、最もシンプルなボリューム表現です。現実の境界は実際には箱に近い形であることが非常に多く、より豊かな形状は同じ `Zone`/`AABB.contains()` インターフェースの背後に、`breach.py` や `estop.py` に触れることなく後から追加できます。
* **ゾーン境界の判定は排他的ではなく包含的です** —— `AABB.contains()` は境界線上ちょうどの点を内側として扱います。安全境界にとっては、これが誤りの許される保守的な方向です：越境報告が早まることはあっても、見逃すことは決してありません。
* **`NullEStopRequester` は本リポジトリ内で唯一のリクエスト実装です** —— 気軽に差し替えられるプレースホルダーではなく、検知と執行の境界そのものを誠実に体現するものです：ここには実際の CAN 伝送手段は存在せず、今後も不用意に追加すべきではありません（上記「既に決定された重要な設計上の境界」および `estop.py` モジュール自身のドキュメントを参照してください）。
* **ゾーンと検知データは YAML ではなく単純な JSON です** —— `pyproject.toml` の依存関係リストは依然として `[]` です。`json` は標準ライブラリの一部であり、`pyyaml` はシリアライズする価値のある実際のゾーン作成ツールが登場した時点での、実際の今後の課題です。
* **キャリブレーションは越境ロジックより前にチェックされ、後には決してチェックされません** —— `evaluate_safety()` は、`check_breaches()` を呼び出す前に、キャリブレーションが欠落または期限切れになった瞬間に `INHIBITED` を返します。これは意図的です：キャリブレーションが古いということは、ゾーンジオメトリ自体が信頼できないことを意味するため、それに対して越境チェックを実行した結果もどのみち無意味です——キャリブレーションを先にチェックすることは、期限切れのキャリブレーションが、本物の危険越境のように見えるものより常に優先されることも意味します。その逆ではありません。
* **`"calibration"` キーが欠落していても正常に読み込まれ、単に `INHIBITED` になるだけです** —— `load_zone_set()` は、ゾーンファイルがこの機能より前のものであったり、キャリブレーションメタデータなしで手書きされたりしたという理由だけではエラーを送出しません。読み込み時ではなく、評価時に安全に失敗するよう設計されています。
* **`--observation`/`"observation"` はインターフェースレベルではオプションですが、安全レベルではそうではありません** —— それを一度も渡さない呼び出し元は、毎回正直な `INHIBITED` を得るだけで、クラッシュすることも黙って `READY` になることも決してありません。これにより、まだ更新されていない呼び出し元に対する後方互換性をパースレイヤーで維持しつつ、空の `objects` リストが実際にアクティブな観測者がセルのクリアを確認したことと区別できなかったという実際のギャップを閉じます。

---

## 📂 リポジトリ構成

```text
HYDRA-UMC-SAFETY-ZONES/
├── src/hydra_umc_safety_zones/
│   ├── geometry.py       # 実際の Point3D/AABB プリミティブ
│   ├── zones.py          # 実際の ZoneLevel/Zone/ZoneSet 定義
│   ├── breach.py         # 実際のゾーン越境チェック
│   ├── calibration.py    # 実際のキャリブレーション鮮度追跡
│   ├── observation.py    # 実際の観測者健全性追跡 —— calibration.py を反映
│   ├── safety_state.py   # 実際のフェイルセーフ判定：READY/WARNING/DANGER/INHIBITED
│   ├── estop.py          # 実際の E-STOP リクエスト（実行は決してしない）
│   ├── config.py         # ゾーン/検知/観測データの実際の JSON 読み込み
│   ├── api.py             # シンプルなJSON/HTTPサーフェス(stdlibのhttp.server)。実際の`check`ロジックを橋渡し
│   └── main.py            # エントリポイント + 実際の `check` サブコマンド
├── tests/                # 実際のテスト：幾何、越境、キャリブレーション、観測、safety_state、estop、設定、api、CLI
├── docs/                # ドキュメントと安全基準
├── build/               # ビルド出力（ローカルの .venv もここに存在）
├── images/              # メディアと図表
├── systemd/
│   └── hydra-umc-safety-zones.service # ローカルCM5ゾーン越境チェックAPIのsystemdユニット
├── pyproject.toml       # パッケージメタデータ、依存関係、オドメーターバージョン
├── bump_version.py      # オドメーター式バージョンインクリメント（明示的な `bump` コマンドのみが実行）
├── bump_manifest_version.py # hydra-umc.project.json のバージョンをネイティブ版と同期(--sync)
├── build.sh / build.bat # venv + editable インストール（dev エクストラ付き） + コンパイルチェック + テスト
├── build-test.sh / .bat # バージョン管理しないビルドチェック（version や CHANGELOG には触れません）
├── tools/
│   ├── build_test.py    # 両方の build-test ランチャーが委譲する共通エンジン
│   └── ci_validate.py   # CI が使用するマニフェスト/CHANGELOG/ドキュメント検証
├── run.sh / run.bat     # ローカル venv からエントリポイントを実行（引数を転送）
└── CHANGELOG.md         # バージョンごとの履歴（オドメーター方式、日付なし）
```

`hardware/`、`firmware/`、`os/`、`models/` フォルダは存在しません——理由は
上記「高度な技術情報」を参照してください。`os/` と `models/` は統合親
プロジェクトである [HYDRA-UMC-VISION-NODE](https://github.com/JuanenRac/HYDRA-UMC-VISION-NODE) にのみ存在します。

---

## 🏗️ ビルドと実行

### 前提条件

* `PATH` 上に **Python 3.10 以降**があること（スクリプトは先に `python3` を試し、次に `python` にフォールバックします）。
* 安全/視覚のランタイム依存関係は現時点では不要です——この段階では**サードパーティのランタイム依存関係が一切ありません**（`pyproject.toml` の `dependencies = []`）。`pytest` は開発専用のエクストラであり、実際のテストスイートのためだけに使用されます。
* ローカル仮想環境（`.venv/` 下）には数十 MB のディスク容量が必要です。

### ステップバイステップ

```bash
# Linux / macOS
./build.sh
```

1. **バージョンの確認** — プロジェクトのバージョンを読み取って表示します。ビルドがバージョンを変更することはありません（上げるのは `python tools/hydra_project_tool.py bump` だけです）。
2. **仮想環境** — `.venv/` が存在しない場合は作成し、存在する場合は再利用します。
3. **Editable インストール（dev エクストラ付き）** — `pip install -e ".[dev]"` により `src/` 下の変更が即座に反映され、`pytest` がインストールされ、`hydra-umc-safety-zones` コンソールエントリポイントが登録されます。
4. **コンパイルチェック** — `python -m compileall -q src` が `src/` 下の各ファイルをバイトコンパイルし、エコシステム全体にわたる構文エラーを検出します。
5. **実際のテストスイート** — `pytest tests/` が全 228 件のテストを実行します。

`set -euo pipefail` は最初に失敗したステップでスクリプトを停止させます。
ダブルクリックで起動された場合（既に開いているターミナルからではなく）は、
ウィンドウは開いたままになります（`Press Enter to close...`）。

```bash
./run.sh
```

`.venv` 内のインタープリタを特定し（POSIX と Windows 両方の `.venv`
ディレクトリ構造を処理）、`python -m hydra_umc_safety_zones.main` を実行
し、あらゆる引数を転送します。

引数なしで呼び出すと名前・バージョン・役割を表示します：

```text
HYDRA-UMC-SAFETY-ZONES v0.1.5
Real-time 3D intrusion detection and E-STOP orchestration for robotic safe-working areas.
```

実際の `check` サブコマンドには、ゾーンファイルと検知ファイルの両方が
必要で、どちらも単純な JSON です。さらにオプションの観測ステータス
ファイルも使えます。`calibration` はゾーンファイル内でオプションで、
コマンドラインの `--observation` もオプションです——それが無い場合に
何が起きるかは以下を参照してください：

```json
// zones.json
{
  "calibration": {"version": "cal-1", "source": "manual", "calibrated_at": "2024-01-15", "max_age_days": 30, "calibrated_by": "A. Operator"},
  "zones": [
    {"id": "warn1", "level": "warning", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 5, "y": 5, "z": 5}},
    {"id": "danger1", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}
  ]
}
```

```json
// detections.json
{"objects": [{"id": "op1", "position": {"x": 0.5, "y": 0.5, "z": 0.5}}]}
```

```json
// observation.json - 上記の検知が実際にアクティブで最近更新された
// 観測者から得られたものであるという実際のエビデンス
{"active": true, "observedAt": "2024-01-15T12:00:00Z", "maxAgeSeconds": 5}
```

これらの例の日付はあくまで例です。`calibrated_at` は `max_age_days` に収まる新しさで、`observedAt` は現在から `maxAgeSeconds` 以内（末尾の `Z` などのタイムゾーン付き）でなければなりません。示した日付のままでは、今日の結果は下の `DANGER` ではなく `INHIBITED` になります。

```bash
./run.sh check --zones zones.json --detections detections.json --observation observation.json
```

```text
SAFETY STATE: DANGER - object(s) ['op1'] breached a danger zone
BREACH: object 'op1' inside warning zone 'warn1'
BREACH: object 'op1' inside danger zone 'danger1'
E-STOP REQUESTED: object 'op1' breached danger zone 'danger1' (not asserted - see estop.py)
```

終了コードは `2`（危険、E-STOP をリクエスト済み）、`1`（警告レベルの
越境のみ）、`0`（越境なし、キャリブレーション有効、観測者がアクティブで
新鮮）、または `3`
（**Inhibited**——キャリブレーションが欠落/期限切れ、または観測者が欠落/
無効化/古すぎる/エラー、越境ロジックより
前にチェック）です。以下はフェイルセーフパスの実際の例です——上記と同じ
`detections.json` を使い、`zones.json` には `"calibration"` キーが全く
ありません：

```bash
./run.sh check --zones zones_no_calibration.json --detections detections.json --observation observation.json
```

```text
SAFETY STATE: INHIBITED - no calibration metadata present - zone geometry cannot be trusted
```

終了コードは `3` です——`op1` が両方のゾーン内にあるにもかかわらず、
`BREACH`/`E-STOP` の出力が全く無いことに注目してください：信頼できない
ゾーン集合は、越境チェックのステップに決して到達しません。`--observation`
を完全に省略した場合も、キャリブレーションが新鮮でどのゾーンにも対象が
近づいていなくても、同じように安全に失敗します：

```bash
./run.sh check --zones zones.json --detections detections_clear.json
```

```text
SAFETY STATE: INHIBITED - no observation status provided - cannot confirm the supplied detections reflect a real, active observer
```

```bat
:: Windows - 手順は同じ、バッチ構文
build.bat
run.bat
run.bat check --zones zones.json --detections detections.json --observation observation.json
```

同じ `check` ロジックは、CLI以外の呼び出し元向けにHTTP経由でも利用できます—`zones`/`detections`/`observation` はファイルパスではなくJSONボディで渡されます:

```bash
./run.sh serve --addr 127.0.0.1 --port 8108
# 別のターミナルで:
curl -s -X POST http://127.0.0.1:8108/check -d '{"zones": {...}, "detections": {...}, "observation": {...}}'
```

実際のコマンド・フラグ・終了コードの完全なリファレンス(実際の実行から採取した `READY`/`WARNING`/`DANGER`/`INHIBITED` の各状態を含む)は [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) を参照してください。

### トラブルシューティング

* **`python`/`python3` が見つからない** —— Python 3.10+ をインストールし `PATH` に含まれていることを確認してください。
* **`compileall` が失敗する** —— `src/` 下に実際の構文エラーが導入されたことを意味します。ビルドは意図的にインストールに触れることなく停止します。
* **`run.sh`/`run.bat` が「`.venv` が見つかりません」と表示する** —— 先に少なくとも 1 回 `build.sh`/`build.bat` を実行してください。
* **editable インストールが古いままになる** —— `.venv/` を削除して再構築してください。これが必要になることはまれです。
* **`check` が非ゼロの終了コードで終わる** —— これは実際の正しい動作であり、失敗ではありません：`1` は警告レベルの越境のみが見つかったことを、`2` は危険レベルの越境が E-STOP をリクエストしたことを、`3` はゾーン集合のキャリブレーションが欠落/期限切れであるか、観測者ステータスが欠落/無効化/古すぎる/エラーであること（フェイルセーフ、越境ロジックが実行される前にチェック）を意味します。Python のトレースバックや不正な JSON によるエラーだけが、実際のバグです。

---

## 🚀 現在の状況と次のステップ

**今日実現していること：** 実際の警告/危険ゾーン定義と実際の越境チェック
（`geometry.py`/`zones.py`/`breach.py`）、越境ロジックより前に安全に
`INHIBITED` にフェイルする実際のキャリブレーション鮮度強制
（`calibration.py`/`safety_state.py`）、構造上検知と執行の境界を守る
実際の E-STOP *リクエスト*パイプライン（`estop.py`）、ゾーン/検知 JSON
ファイルに対する実際の CLI サブコマンド `check`、そして通過した 228 件の
テスト——完全な実際のビルド/実行出力は
[`CHANGELOG.md`](CHANGELOG.md) を参照してください。

**まだ残っている作業（順不同、確定した期限なし）：**

* Hailo-8 の空間セグメンテーションに基づく実際の 3D 占有マッピング。これに
  より `check` が現在 JSON 入力として想定している検知対象位置を、実際に
  生成できるようになります。
* ロボット自身の構造の動的な自己遮蔽マスキング。
* [HYDRA-UMC](https://github.com/JuanenRac/HYDRA-UMC) への `EStopRequester`
  を実装した実際の CAN 伝送手段 —— `estop.py` は、実際の実装が満たすべき
  インターフェースをすでに定義しています。
* 実際の安全認証作業（上記の ISO 13849-1 バッジは目標を示すものであり、完了した認証ではありません）。

---

## 🔗 関連プロジェクト

本プロジェクトは、同じ作者(JuanenRac / Electro Hobby 3D)による HYDRA-UMC ロボティクスエコシステムの一部です。リクエストが実はこの中のどれかについてのものである可能性があるため、知っておく価値があります。

**親プロジェクト**
- **[HYDRA-UMC-VISION-NODE](https://github.com/JuanenRac/HYDRA-UMC-VISION-NODE)** — Hailo-8 ビジョンパイプラインの統合ハブ、段階ごとの実際のハードウェア準備状況チェック付き。本リポジトリは、その自身の知覚パイプライン内における特定の段階・消費者として、この親の一部を成す。

**兄弟プロジェクト** —— HYDRA-UMC-VISION-NODE 自身の Hailo-8 知覚パイプラインにおける他の段階・消費者
- **[HYDRA-UMC-VISION-STREAMER](https://github.com/JuanenRac/HYDRA-UMC-VISION-STREAMER)** — 実際の HailoRT 統合境界を持つ、実際の GStreamer パイプライン + MediaMTX 設定生成器。
- **[HYDRA-UMC-DETECTION-HEF](https://github.com/JuanenRac/HYDRA-UMC-DETECTION-HEF)** — Hailo アーキテクチャ/チェックサムによる安全読み込み検証を備えた、実際のコンパイル済みモデルレジストリ。
- **[HYDRA-UMC-VISUAL-SERVOING-API](https://github.com/JuanenRac/HYDRA-UMC-VISUAL-SERVOING-API)** — ゾーン状態を条件に承認する Position-Based Visual Servoing 補正則。カメラによる姿勢推定はまだない。

**直接関連**
- **[HYDRA-UMC](https://github.com/JuanenRac/HYDRA-UMC)** — ロボットアームのマザーボードの設計——CM5 ホスト + デュアルコア STM32H745、CAN-OTA/SPI-OTA 経由で最大 8 本のツールアームを統括することを目指す(ファームウェアは初期段階で、実装済みの基板はまだない)。本プロジェクトは E-STOP を要求できるが、ファームウェアにはまだ E-STOP の経路がない。

**エコシステムの他のプロジェクト**

*コアハードウェア&プラットフォーム*
- **[HYDRA-UMC-OS](https://github.com/JuanenRac/HYDRA-UMC-OS)** — CM5 向けの再現可能な Raspberry Pi OS プロダクト層——読み取り専用エージェント、検証済み設定/プロファイル、WiFi 初回接続プロビジョニング。
- **[HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK)** — すべてのブリッジが自身のコマンドを検証する共有 JSON-Schema 契約とソフトウェアのジョブゲート境界(認証済みの安全機能ではない)。
- **[HYDRA-UMC-CONNECTOR-HUB](https://github.com/JuanenRac/HYDRA-UMC-CONNECTOR-HUB)** — 外部マシン用コネクタのための宣言的アダプターマニフェストのレジストリとバリデーター。SDK 自身の契約という発想を外部マシンにまで拡張し、産業用ゲートウェイ系のプロジェクトを置き換えることはありません。

*コアバックエンド&クライアント*
- **[HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER)** — すべての制御クライアントが実際に通信する、本物のヘッドレスバックエンド(REST/WebSocket)。
- **[HYDRA-UMC-STUDIO](https://github.com/JuanenRac/HYDRA-UMC-STUDIO)** — リアルタイムのマルチロボット 3D 可視化を備えたウェブ制御ダッシュボード。
- **[HYDRA-UMC-SUITE](https://github.com/JuanenRac/HYDRA-UMC-SUITE)** — 複数のサーバーを同時に扱えるデスクトップ(PySide6)スウォームコマンドセンター、スタンドアロン実行ファイルとしてパッケージ化。
- **[HYDRA-UMC-ANDROID-CONTROL](https://github.com/JuanenRac/HYDRA-UMC-ANDROID-CONTROL)** — 生体認証ログインとペアリングされた Wear OS コンパニオンを備えたネイティブ Android 制御アプリ。
- **[HYDRA-UMC-IOS-CONTROL](https://github.com/JuanenRac/HYDRA-UMC-IOS-CONTROL)** — リアルタイム WebSocket 同期を備えた iOS/iPadOS 制御アプリ(Flutter)。
- **[HYDRA-UMC-DSI](https://github.com/JuanenRac/HYDRA-UMC-DSI)** — 本体搭載の 7 インチ DSI タッチスクリーン向けネイティブタッチ UI、CM5 自体に組み込み。
- **[HYDRA-UMC-EDITOR-URDF](https://github.com/JuanenRac/HYDRA-UMC-EDITOR-URDF)** — 完成したモデルを STUDIO 自身のカタログへ送信するデスクトップ用グラフィカル URDF 作成/編集ツール。
- **[HYDRA-UMC-EDITOR-STL](https://github.com/JuanenRac/HYDRA-UMC-EDITOR-STL)** — HYDRA-UMC-EDITOR-URDF も編集する同じモデルカタログ内で、実在するパーツを変換/置換/削除/追加するデスクトップ STL モデルエディタ。
- **[HYDRA-UMC-BRIDGE-AMR](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-AMR)** — VDA 5050 MQTT パブリッシャーによる AGV/AMR フリートの調整境界(エミュレートしたフリートに対してのみテスト済み)。
- **[HYDRA-UMC-BRIDGE-CNC](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-CNC)** — GRBL ステータス/制御バイトへのアクセスを持つ、CNC セルの高レベルコーディネーター(エミュレーターに対してのみテスト済み)。
- **[HYDRA-UMC-BRIDGE-DROIDS](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-DROIDS)** — Boston Dynamics Spot コマンド送信機能を持つ、脚型/ヒューマノイドドロイドの調整境界(エミュレーターに対してのみテスト済み)。
- **[HYDRA-UMC-BRIDGE-LASER](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-LASER)** — キー/筐体/インターロックの GPIO 信号 3 系統を読み取るレーザーセルのコーディネーター(模擬チップに対してのみテスト済み。レーザーの武装や発射はできない)。
- **[HYDRA-UMC-BRIDGE-OPENPNP](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-OPENPNP)** — OpenPnP ピックアンドプレースの基板フローを安全に統括する高レベルコーディネーター。
- **[HYDRA-UMC-BRIDGE-PRINTER3D](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-PRINTER3D)** — 実際にゲート制御されたジョブコマンドを持つ、Moonraker/Klipper 3D プリンター向けの安全な調整境界。
- **[HYDRA-UMC-BRIDGE-ROS2](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-ROS2)** — 遅延インポートの rclpy ROS 2 トランスポートを持つコーディネーター(ROS 2 環境ではまだ実行していない)。
- **[HYDRA-UMC-BRIDGE-UAV](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-UAV)** — MAVLink コマンド送信機能を持つ、カメラ搭載 UAV の調整境界(エミュレーターに対してのみテスト済み)。

*URTC ツールプラットフォーム*
- **[URTC](https://github.com/JuanenRac/URTC)** — 物理的な Universal Robot Tool Controller 基板向けファームウェア、CAN バス経由の 25 以上のツールプロファイル。
- **[URTC-FLASHER](https://github.com/JuanenRac/URTC-FLASHER)** — URTC 基板用のデスクトップ GUI 書き込みツール、CAN-OTA およびフルチップ SWD/JTAG。
- **[URTC-TESTER](https://github.com/JuanenRac/URTC-TESTER)** — URTC 基板向けのデスクトップ CAN バスライブ診断ツール、ツールプロファイルごとに 1 パネル。
- **[URTC-WEB-STUDIO](https://github.com/JuanenRac/URTC-WEB-STUDIO)** — Web Serial API を使ったブラウザベースの URTC-TESTER の代替、ローカルインストール不要。
- **[URTC-GATEWAY](https://github.com/JuanenRac/URTC-GATEWAY)** — URTC の CAN バスの前に置くゲートウェイ：許可する識別子のリスト、レート制限、セッション鍵。コンピューター上でテスト済みで、実際のバスではまだ試していません。

*コグニティブ AI ノード(Hailo-10)*
- **[HYDRA-UMC-COGNITIVE-NODE](https://github.com/JuanenRac/HYDRA-UMC-COGNITIVE-NODE)** — Hailo-10 コグニティブパイプライン(LLM/VLA/音声オーケストレーション)の統合ハブ。
- **[HYDRA-UMC-VLA-ENGINE](https://github.com/JuanenRac/HYDRA-UMC-VLA-ENGINE)** — Vision-Language-Action モデル向けの、実際のアクショントークンのエンコード/デコードと軌道生成。
- **[HYDRA-UMC-VOICE-UI](https://github.com/JuanenRac/HYDRA-UMC-VOICE-UI)** — 確認ゲート付きの限定的な Watch リレーを備えた、実際の音声フロントエンド(VAD + 意図解析)。
- **[HYDRA-UMC-SEMANTIC-PLANNER](https://github.com/JuanenRac/HYDRA-UMC-SEMANTIC-PLANNER)** — MCU エラーコードに対する、実際のルールベースのタスク分解と意味的エラー復旧。
- **[HYDRA-UMC-DOCS-QA](https://github.com/JuanenRac/HYDRA-UMC-DOCS-QA)** — このエコシステム自身の Markdown ドキュメントに対する、標準ライブラリのみの実際の TF-IDF 文書検索。
- **[HYDRA-UMC-LOCAL-TECHNICIAN](https://github.com/JuanenRac/HYDRA-UMC-LOCAL-TECHNICIAN)** — エコシステム自身のための、ローカルでポリシーゲート付きのAIメンテナンス技術者 — 観察、診断し、修正を提案する;最上位の2つのリスクレベルは意図的にまだ実装されていない。

*オーケストレーション&スウォーム*
- **[HYDRA-UMC-ORCHESTRATOR](https://github.com/JuanenRac/HYDRA-UMC-ORCHESTRATOR)** — 実際の gRPC/Protobuf ヘルスレポート契約とミッションステートマシンを持つ統合ハブ。
- **[HYDRA-UMC-JOB-DISPATCHER](https://github.com/JuanenRac/HYDRA-UMC-JOB-DISPATCHER)** — 実際の HTTP API 上に構築された、優先度ベースの実際のジョブキュー(重複排除付き)。
- **[HYDRA-UMC-NODE-HEALING](https://github.com/JuanenRac/HYDRA-UMC-NODE-HEALING)** — リトライ/バックオフとアイデンティティ不一致検出を備えた、実際の gRPC ベースのフリートヘルスウォッチドッグ。
- **[HYDRA-UMC-PATH-PLANNER-3D](https://github.com/JuanenRac/HYDRA-UMC-PATH-PLANNER-3D)** — 実際の障害物/ワークスペース衝突検証を備えた、実際の RRT ベースの 3D 経路プランナー。
- **[HYDRA-UMC-SWARM-SYNC](https://github.com/JuanenRac/HYDRA-UMC-SWARM-SYNC)** — 複数セルの収束についてプロパティテストされた、実際の CRDT LWW-Element-Map 状態同期。

*デジタルツイン&シミュレーション*
- **[HYDRA-UMC-TWIN](https://github.com/JuanenRac/HYDRA-UMC-TWIN)** — 実際のバージョン互換性同期契約を持つ、デジタルツインエンジンの統合ハブ。
- **[HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE)** — シミュレーションと実機の間にソフトウェアの衝突インターロックを備えたハードウェア・イン・ザ・ループのルーター(実機なしでテスト済み)。
- **[HYDRA-UMC-PHYSICS-REPLICA](https://github.com/JuanenRac/HYDRA-UMC-PHYSICS-REPLICA)** — 実際の URDF サブセットに対する、実際の順運動学と関節限界検証。
- **[HYDRA-UMC-SYNTHETIC-DATA-GEN](https://github.com/JuanenRac/HYDRA-UMC-SYNTHETIC-DATA-GEN)** — YOLO/COCO アノテーションのエクスポート機能を持つ、実際のプロシージャル 2D シーンジェネレーター。

*データ&分析*
- **[HYDRA-UMC-DATALAKE](https://github.com/JuanenRac/HYDRA-UMC-DATALAKE)** — 実際の取り込み/クエリ HTTP API を備えた、実際の sqlite3 ベースの時系列ストア。
- **[HYDRA-UMC-ANOMALY-DETECTOR](https://github.com/JuanenRac/HYDRA-UMC-ANOMALY-DETECTOR)** — ドリフト監視を備えた、実際の FFT + 統計ベースラインによる異常検知器。
- **[HYDRA-UMC-PRODUCTION-REPORTS](https://github.com/JuanenRac/HYDRA-UMC-PRODUCTION-REPORTS)** — DATALAKE の履歴に対する実際の OEE/稼働率計算、再現可能な CSV エクスポート付き。
- **[HYDRA-UMC-TELEMETRY-COLLECTOR](https://github.com/JuanenRac/HYDRA-UMC-TELEMETRY-COLLECTOR)** — シーケンス重複排除機能を備えた、DATALAKE への実際の CAN/WebSocket 取り込みパイプライン。

*産業用ゲートウェイ*
- **[HYDRA-UMC-GATEWAY-INDUSTRIAL](https://github.com/JuanenRac/HYDRA-UMC-GATEWAY-INDUSTRIAL)** — 実際のコマンド許可リスト/バックプレッシャー層を持つ、産業用プロトコルへ中継する統合ハブ。
- **[HYDRA-UMC-OPCUA-SERVER](https://github.com/JuanenRac/HYDRA-UMC-OPCUA-SERVER)** — 実際のバイナリプロトコルクライアントセッションで検証された、実際の OPC-UA アドレス空間。
- **[HYDRA-UMC-MQTT-BROKER](https://github.com/JuanenRac/HYDRA-UMC-MQTT-BROKER)** — クライアント単位のオプション認証とトピック ACL を備えた、実際の MQTT ブローカー。
- **[HYDRA-UMC-MTCONNECT-ADAPTER](https://github.com/JuanenRac/HYDRA-UMC-MTCONNECT-ADAPTER)** — 縮退モード出力を備えた、実際の MTConnect `/probe` および `/current` XML エンドポイント。

*補完ツール&エコシステム運用*
- **[HYDRA-UMC-DASHBOARD-AI](https://github.com/JuanenRac/HYDRA-UMC-DASHBOARD-AI)** — 誠実な統計フォールバックを備えた、DATALAKE/ANOMALY-DETECTOR 上のスマートサマリーと異常ハイライトパネル。
- **[HYDRA-UMC-TOOL-CLI](https://github.com/JuanenRac/HYDRA-UMC-TOOL-CLI)** — 実際の安定した終了コード契約を持つフリート CLI、HYDRA-UMC-SERVER 自身の API の本物のライブクライアント。
- **[HYDRA-UMC-WATCH](https://github.com/JuanenRac/HYDRA-UMC-WATCH)** — 触覚アラートのパターンとペアリング済みスマートフォンへの音声リレーを備えた WearOS コンパニオンアプリ(物理的なウォッチではまだ実行していない)。
- **[URTC-SMART-RACK](https://github.com/JuanenRac/URTC-SMART-RACK)** — 実際の工具 ID デコードと Smart Idle 予熱ロジックを備えた、基板搭載ラック用ファームウェア。
- **[URTC-VISION-TOOL](https://github.com/JuanenRac/URTC-VISION-TOOL)** — サーマル/RGB 検査ツールヘッド向けの、ファームウェアと実際の Python ビジョンコンパニオン。
- **[HYDRA-UMC-UPDATER](https://github.com/JuanenRac/HYDRA-UMC-UPDATER)** — このエコシステム内のすべてのリポジトリを検出・クローン・更新する、管理用デスクトップツール。
- **[HYDRA-UMC-OS-REBUILDER](https://github.com/JuanenRac/HYDRA-UMC-OS-REBUILDER)** — エコシステムの最新バージョンをプリロードした、書き込み可能なCM5イメージを構築するWindows/Linuxデスクトップツール。Raspberry Pi Imager方式の初回起動Wi-Fi/ユーザー/SSH設定を備える。
- **[HYDRA-UMC-OPS-AGENT](https://github.com/JuanenRac/HYDRA-UMC-OPS-AGENT)** — 保守インシデントコーディネーター: 低権限のエッジ役割がサニタイズされたインベントリ/ヘルスのスナップショットを収集し、コントロールプレーン役割がそれを読み取り専用でレンダリングして AI プロバイダーに診断の提案を依頼します - パッチを適用することも、何かをデプロイすることも決してありません。
- **[HYDRA-UMC-DEV-SERVER](https://github.com/JuanenRac/HYDRA-UMC-DEV-SERVER)** — 再現可能な開発ホスト（Raspberry Pi 5 / CM5）。エコシステムのソースコードを保管し、永続キューのもとで境界の明確なビルド/テストタスクを実行する。運用中の CM5 とは明確に異なる、開発専用の役割。

---

## 📚 ドキュメント & コミュニティ

- **[CONTRIBUTING.md](CONTRIBUTING.md)** —— プルリクエストのための技術スタックとコーディング指針。
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** —— このコミュニティで期待される行動規範。
- **[SECURITY.md](SECURITY.md)** —— 脆弱性の報告方法と、このプロジェクトの実際のセキュリティ重点領域。
- **[SUPPORT.md](SUPPORT.md)** —— 質問の投稿先とバグの報告先。
- **[LICENSE.md](LICENSE.md)** —— このプロジェクト自身のライセンス。

## 👤 作者
**JuanenRac** (Electro Hobby 3D)
📧 electrohobby3d@gmail.com
📺 [youtube.com/@electrohobby3d](https://youtube.com/@electrohobby3d)

## 📜 ライセンス
GPL-3.0 —— 詳細は LICENSE を参照してください。
