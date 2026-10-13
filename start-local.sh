#!/usr/bin/env bash
# הרצה מקומית בפקודה אחת: יוצר סביבה, מתקין, מגדיר מפתח מנהל אקראי ומפעיל. כתובת: http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
if [ ! -f .env.local ]; then
  echo "CPA_AI_API_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')" > .env.local
  python -m cpa_ai gen-key | sed 's/^/CPA_AI_ENCRYPTION_KEY=/' >> .env.local
fi
set -a; . ./.env.local; set +a
echo "מפתח הכניסה שלך (הדביקו בהגדרות ⚙️ בממשק): $CPA_AI_API_KEY"
exec python -m cpa_ai
