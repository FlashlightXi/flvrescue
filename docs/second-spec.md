\# flvrescue — Fast Rescue / Multi-pass Recovery 実装ハンドオフ



現在の `flvrescue` を、実際の故障しかけたHDDで使いやすい設計へ更新してください。



対象ディスクでは、完全なI/Oエラーよりも「読み込み自体は成功するが、特定領域で応答時間が数秒まで悪化し、同じ周辺を読み続けると著しく停滞する」挙動が確認されています。



このため、完全復旧よりも \*\*読みやすい領域を優先して早く回収すること\*\* を重視します。



\## 基本方針



Fast rescue をデフォルト動作にしてください。



Pass 1では、正常に高速で読める領域を優先して回収します。



読み込みが異常に遅くなった場合や、通常ブロックの読み込みに失敗した場合、その場で細かく掘り下げず、一旦その周辺をスキップして先へ進んでください。



スキップ後は適切な間隔でprobeし、再び正常に読める領域を見つけたら通常読み込みへ復帰してください。



連続して遅い領域に遭遇した場合は、skip幅を段階的に拡大するadaptive skipを採用してください。



\## Multi-pass



以下の構成を基本としてください。



\### Pass 1 — Fast rescue



読みやすい領域を最優先で回収します。



slow readまたは通常ブロックのread failureを検出した場合、その領域を深追いせずスキップします。



既存の細粒度fallback処理はPass 1では原則使用しません。



\### Pass 2 — Skipped region recovery



Pass 1でスキップした領域のみを対象に、より細かくprobeして、読みやすい部分が残っていれば追加回収します。



Pass 1で正常に回収済みの領域は再読しないでください。



\### Pass 3 — Deep recovery



任意実行としてください。



既存のfallback処理を利用し、より小さいブロックへ細分化して未回収領域を可能な範囲で救出します。



今回の主用途ではPass 1またはPass 2までで十分なため、Pass 3をデフォルトでは実行しないでください。



\## Slow read判定



各readの所要時間を計測してください。



読み込みが成功した場合でも、一定時間以上かかったreadはslowとして扱います。



slow thresholdは設定可能にしつつ、実用的なデフォルト値を設定してください。



slow readで取得できたデータ自体は正常に保存して構いません。その後の周辺領域を深追いしないためのシグナルとしてslow判定を使用してください。



v1では進行中の同期readを強制キャンセルする必要はありません。read完了後に所要時間を判定し、その後の戦略を変更してください。



\## Map / recovery state



現在の単純な連続frontierだけではなく、少なくとも以下を区別できるようにしてください。



\* recovered

\* skipped

\* unreadable

\* unprocessed



slowを理由に読み飛ばした領域と、実際にread errorが確認された領域は区別してください。



これにより、後続passでは必要なrangeだけを再処理できるようにしてください。



map/resume情報は中断後も安全に再利用できることを維持してください。



\## Progress UI



現在の1行ログ形式を、常時更新されるコンパクトなLive表示へ変更してください。



ファイルパスの常時表示は不要です。



画面下部に固定された2〜3行程度の表示を基本とし、少なくとも以下を常時確認できるようにしてください。



\* 現在のpass

\* 経過時間

\* 全体進捗

\* 現在速度

\* recovered量

\* slow/skipped量

\* unreadable量

\* 現在の処理状態



長い1行に情報を詰め込まず、ターミナル幅に収まる読みやすいレイアウトにしてください。



ANSI colorを使い、正常・slow・error・skipを視覚的に区別してください。ただし過剰な装飾は不要です。



\## Progress更新



HDDのreadが数秒間ブロックしている最中でも、UIは最低1秒に1回更新されるようにしてください。



そのため、救出処理と描画処理を分離してください。



Progress rendererは別thread等で動作させ、共有された状態だけを参照してください。



Progress側からsource HDDへI/Oを発生させてはいけません。



read開始時刻、offset、size等を共有stateに記録することで、readがまだ完了していなくても現在何を待っているか表示できるようにしてください。



\## Event表示



以下のような重要イベントは、Live表示とは別にユーザーへ分かる形で通知してください。



\* slow readの検出

\* 通常ブロックのread failure

\* skip開始

\* skip幅変更

\* 正常領域の再発見

\* pass切り替え



Live表示を壊さずイベント履歴を表示できる構成にしてください。



非TTY環境では通常のline-based logへfallbackしてください。



\## Safety



以下は維持してください。



\* sourceはread-only

\* destinationとの取り違え防止

\* explicit-offset I/O

\* Ctrl+C時の安全なcheckpoint

\* resume

\* atomic map更新

\* destination write failureの適切な扱い



故障ディスクへの並列readは行わないでください。



同じ遅い領域に不用意に何度もアクセスしないことを優先してください。



\## Testing



既存のsimulated I/O errorテストに加えて、slow readを模擬できるReaderを追加してください。



少なくとも以下を確認してください。



\* 正常領域ではFast Passが連続して進む

\* slow read後にskipへ移行する

\* 連続slow時にskip幅が拡大する

\* 正常領域を再発見して通常読み込みへ復帰する

\* Pass 2がskipped rangeのみを処理する

\* Pass 3が任意実行である

\* readがブロックしている間もProgress UIが更新される

\* resume後に回収済みrangeを再読しない

\* map stateが各passを跨いで正しく維持される



実装後はテストを実行し、設計上の重要な変更点とテスト結果を簡潔に報告してください。



既存コードを必要以上に全面書き換えず、現在の `reader / rescue / mapfile / progress / cli` の責務分離を活かして実装してください。



