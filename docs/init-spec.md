\# Programmer AI Agent Handoff — Damaged HDD File Rescue Tool



Windows上の死にかけHDDから、数十〜数百GBのFLV録画ファイルを可能な限り救出するための小さなCLIツールを実装してください。



\## 背景・目的



HDDに不良セクタがある可能性があります。



目的は完全なデータ復旧ではなく、



\* 正常に読める領域を可能な限り早くSSDへ退避する

\* 読めない小領域には長時間粘らない

\* 読めなかった領域だけ0埋めする

\* 元ファイルと同じoffset・ファイルサイズを維持する

\* その後FFmpegで30倍速タイムラプスへ変換できればよい



というものです。



対象は主に巨大な `.flv` ファイルで、1ファイルあたり数十GB〜400GB程度あります。



HDD全体のクローンを作るツールにはしません。通常のWindowsファイルパスから、1ファイルずつ救出する用途に特化してください。



\## 最重要設計方針



故障しかけたHDDへのアクセス回数をできるだけ減らしてください。



「完全復旧のために不良セクタへ何度もretryする」より、



> HDDが完全に死ぬ前に正常領域をできるだけ多く1回で取得する



ことを優先します。



元ファイルには絶対に書き込まないでください。



\## v1で必要な機能



Python 3でWindows向けCLIとして実装してください。



基本読み込み単位:



\* Normal block: 8 MiB

\* Error fallback: 64 KiB

\* Final fallback: 4 KiB

\* 4 KiBでもI/O errorなら、その領域を0埋め



基本フロー:



```text

8 MiB read

├─ success

│    └─ SSDへwrite

│

└─ I/O error

&#x20;    ↓

&#x20;    同範囲を64 KiBずつ処理

&#x20;    ├─ success → write

&#x20;    └─ error

&#x20;         ↓

&#x20;         その64 KiBのみ4 KiBずつ処理

&#x20;         ├─ success → write

&#x20;         └─ error → zero fill + bad range記録

```



エラー後のファイルポインタ状態は信用せず、すべて明示offsetベースで処理してください。



例:



```python

read\_at(offset, length)

write\_at(offset, data)

```



という抽象化を推奨します。



\## 必須機能



\### Resume



中断・クラッシュ・Ctrl+C後に続きを再開できるようにしてください。



最低限、



\* source path

\* source size

\* destination path

\* current/recovered position

\* bad ranges



をsidecar JSON等に保存してください。



ただし巨大ファイルなので、正常な8MiBブロックを1個ずつJSONへ全部保存する必要はありません。



前方へ順次処理するv1なら、



```text

completed\_until

bad\_ranges

```



程度で構いません。



\### Bad range log



読めなかった領域について、



```json

{

&#x20; "offset": 123456789,

&#x20; "length": 4096

}

```



のように記録してください。



連続するbad blockは可能ならrangeとしてmergeしてください。



最終的に、



```text

Recovered: 99.982 GiB

Unreadable: 28 KiB

Bad ranges: 4

```



のようなsummaryを表示してください。



\### Ctrl+C



Ctrl+Cを受けたら、



\* 現在の状態を保存

\* 出力をflush

\* map/resume情報を確定

\* 正常終了



できるようにしてください。



\### Progress



最低限、



```text

File

processed / total

percentage

current speed

average speed

elapsed

recovered bytes

unreadable bytes

bad range count

```



を表示してください。



表示更新のために過剰なディスクI/Oを発生させないこと。



\### 出力



sourceとdestinationの論理的なoffset関係を維持してください。



読めない範囲は0x00で埋めて構いません。



最終出力ファイルは原則として元ファイルと同じサイズにしてください。



\## 重要な注意



Pythonの通常の`read()`でI/O errorが発生した場合、OSやHDD自身のretryによって長時間ブロックする可能性があります。



v1では無理にtimeout実装まで行わなくて構いません。



ただし、



\* どこでブロックしうるか

\* Python側から安全にキャンセルできる範囲

\* 将来的にWindows APIの `CreateFileW` / `ReadFile` 等へ移行する価値があるか



はREADMEか設計メモに記載してください。



スレッドをkillして強引にI/Oをキャンセルするような危険な実装は、十分な根拠なしには入れないでください。



\## テスト戦略



実物の死にかけHDDを最初のテスト対象にしないでください。



\### Test A: 正常ファイル



数百MB程度のテストファイルを作成し、



```text

source == rescued

```



となることをhash比較してください。



\### Test B: 模擬I/O error



これが最重要です。



ファイル自体を破損させるだけでは、OSから見れば正常に読み込めるためI/O errorのテストになりません。



読み込み層を抽象化し、



```python

class Reader:

&#x20;   def read\_at(offset, size): ...

```



のようにしてください。



テスト用Readerでは指定rangeとreadが交差した場合に意図的に`OSError`を発生させてください。



例:



```text

bad:

100 MiB + 12 KiB ～ 100 MiB + 16 KiB

```



この状態で、



\* 8MiB readが失敗する

\* 64KiB fallbackへ移る

\* 4KiBまで狭める

\* 対象4KiBだけ0埋めされる

\* それより後ろのデータが正常に回収される



ことを自動テストしてください。



また、複数bad range、連続bad range、block境界を跨ぐbad rangeも試してください。



\### Test C: 中断・resume



例えば30〜50%地点で意図的に処理を止め、



再実行時に既に救出済みの先頭部分を再読せず続きを処理できることを確認してください。



最終結果が一回で処理した場合と一致することも確認してください。



\### Test D: FLV corruption



実際の短いFLVサンプルも作成してください。



可能ならffmpegで数十秒〜数分のテストFLVを生成します。



オリジナル:



```text

sample.flv

```



から以下を作ってください。



1\. 中央の4 KiBを0埋め

2\. 中央の64 KiBを0埋め

3\. 一部をランダムbytesで上書き

4\. ファイル末尾をtruncate

5\. 異なる位置に複数の破損



その後FFmpegで、



```text

ffmpeg -fflags +discardcorrupt+genpts -err\_detect ignore\_err ...

```



等を試し、



\* decodeが最後まで進むか

\* どこでwarning/errorが出るか

\* 破損後に映像へ復帰するか

\* durationがどの程度保たれるか



を確認してください。



これはrescueアルゴリズムのI/O errorテストとは別物として扱ってください。



\### Test E: 救出結果をFLVとして検証



模擬I/O error Readerを使って、



```text

正常FLV

↓

特定offsetをread error扱い

↓

rescue tool

↓

一部zero-filled FLV

↓

FFmpeg

```



というend-to-endテストも行ってください。



実際の目的に最も近いテストです。



\## まず避けるもの



v1では以下は不要です。



\* HDD全体のraw clone

\* partition復旧

\* filesystem repair

\* SMART操作

\* 自動的なsector再retry

\* 複雑な二分探索

\* GUI

\* ネットワーク機能

\* 同時並列読み込み



特に故障HDDへの並列readはしないでください。



\## 将来のv2候補



v1完成後に必要性を評価してください。



\* reverse pass

\* bad region retry pass

\* skip-ahead strategy

\* Windows native/unbuffered I/O

\* physical sector alignment

\* I/O cancellation

\* configurable block sizes

\* mapfile compatibility

\* bad regionだけ後日再試行

\* HDD温度/SMART情報の補助表示



ただし最初から実装しないでください。



\## CLI例



```powershell

python rescue.py "E:\\recordings\\1006.flv" "C:\\recovery\\1006.flv"

```



オプション例:



```powershell

python rescue.py SOURCE DEST `

&#x20; --map DEST.rescue.json `

&#x20; --block 8M `

&#x20; --fallback 64K `

&#x20; --sector 4K

```



デフォルト値が今回の用途に合うなら、オプション指定なしでも使えるようにしてください。



\## 成果物



1\. 実装

2\. README

3\. 自動テスト

4\. 模擬I/O errorテスト

5\. テストFLV生成スクリプト

6\. FLV破損生成スクリプト

7\. FFmpegによる検証方法

8\. 実HDDで試す前の安全確認手順



実装後、テスト結果を簡潔にまとめ、



\* 正常コピー

\* 4KiB破損

\* 64KiB破損

\* 複数破損

\* resume

\* corrupted FLV → FFmpeg



それぞれがPASS/FAILどちらだったか報告してください。



最初に設計を長々説明するより、まず小さな実装＋テストまで進め、その実測結果を基に必要な変更を提案してください。



