name: PapersBot -> Teams

on:
  schedule:
    - cron: "0 0 * * *"   # 毎日 00:00 UTC = 09:00 JST
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      # ★追加：posted.dat を永続化（復元＆保存）
      - name: Restore posted.dat cache
        uses: actions/cache@v4
        with:
          path: posted.dat
          key: papersbot-posted-${{ github.repository }}-${{ github.ref_name }}
          restore-keys: |
            papersbot-posted-${{ github.repository }}-

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Run PapersBot and notify Teams
        shell: bash
        env:
          TEAMS_WEBHOOK_URL: ${{ secrets.TEAMS_WEBHOOK_URL }}
        run: |
          set -euo pipefail
          set -x

          # Secret チェック
          test -n "${TEAMS_WEBHOOK_URL:-}" || { echo "❌ Secret empty"; exit 1; }

          # 初回用：posted.dat が無ければ作る（空でOK）
          touch posted.dat

          # Python 依存関係
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi

          # 実行環境の確認
          echo "=== pwd ==="
          pwd
          echo "=== ls -la ==="
          ls -la

          # (B) 任意：--do-not-tweet 時に posted.dat を更新させない（おすすめ）
          #     ※デバッグ実行で「投稿してないのに既投稿扱い」になるのを防ぐ

          # stdout + stderr をまとめて log.txt に保存
          export PYTHONFAULTHANDLER=1
          python -u -X faulthandler papersbot.py > log.txt 2>&1 || {
            code=$?
            echo "❌ papersbot.py failed (exit=$code)"
            echo "=== last 200 lines of log.txt ==="
            tail -n 200 log.txt || true
            exit $code
          }

          # (B) を有効にした場合：posted.dat を元に戻す
          #     本番投稿のときはこの2行をコメントアウトして「更新を反映」させる運用にする

          echo "=== last 50 lines of log.txt (success) ==="
          tail -n 50 log.txt || true

          # Teams に送る JSON を作成
          python - <<'PY' > payload.json
          import json, datetime, re

          text = open("log.txt", "r", encoding="utf-8", errors="replace").read()

          # "TWEET: <title> <url>" を抽出
          items = []
          seen_urls = set()

          for line in text.splitlines():
              line = line.strip()
              if not line.startswith("TWEET: "):
                  continue

              body = line[len("TWEET: "):].strip()

              # 末尾URLを取り出す
              m = re.match(r"(.+?)\s+(https?://\S+)$", body)
              if not m:
                  continue

              title = m.group(1).strip()
              url = m.group(2).strip()

              # 重複排除（URLでユニーク化）
              if url in seen_urls:
                  continue
              seen_urls.add(url)

              if len(title) > 140:
                  title = title[:137] + "..."

              items.append((title, url))

          now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

          if items:
              msg_lines = [f"{i}. [{t}]({u})" for i, (t, u) in enumerate(items[:10], 1)]
              msg = "\n".join(msg_lines)
          else:
              msg = "（新着なし）"

          payload = {
            "user_name": "PapersBot",
            "feedback_message": msg,
            "timestamp": now,
            "host": "github-actions",
          }
          print(json.dumps(payload, ensure_ascii=False))
          PY

          # Teams Webhook（Power Automate）へ POST
          curl -sS --fail-with-body -X POST "$TEAMS_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            --data-binary @payload.json

