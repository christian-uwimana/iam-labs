#!/usr/bin/env bash
# Deploys the SCIM test server to Azure App Service from Azure Cloud Shell (Bash).
# Usage, from the folder that holds app.py:   bash deploy.sh
# Optional: RG=... LOCATION=... SKU=B1 bash deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

RG=${RG:-rg-rwandacorp-lab}
LOCATION=${LOCATION:-southafricanorth}
SKU=${SKU:-F1}
PLAN=${PLAN:-asp-rwandacorp-scim}
APP=${APP:-scim-rwandacorp-$RANDOM}
TOKEN=$(openssl rand -hex 32)
CLIENT_ID=netskope-lab
CLIENT_SECRET=$(openssl rand -hex 32)
URL="https://$APP.azurewebsites.net"

echo "Creating web app $APP in $RG ($LOCATION, $SKU plan). This takes about 5 minutes..."
az webapp up --name "$APP" --resource-group "$RG" --location "$LOCATION" --plan "$PLAN" \
  --sku "$SKU" --os-type Linux --runtime "PYTHON:3.12" --only-show-errors > /dev/null

echo "Setting the token and start-up command..."
az webapp config appsettings set -g "$RG" -n "$APP" --only-show-errors --settings \
  SCIM_TOKEN="$TOKEN" CLIENT_ID="$CLIENT_ID" CLIENT_SECRET="$CLIENT_SECRET" DATA_DIR=/home/data > /dev/null
az webapp config set -g "$RG" -n "$APP" --only-show-errors \
  --startup-file "gunicorn --bind=0.0.0.0 --timeout 600 --workers 1 --threads 4 app:app" > /dev/null
az webapp update -g "$RG" -n "$APP" --https-only true --only-show-errors > /dev/null

echo "Waiting for the app to answer (up to 5 minutes)..."
code=000
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$URL/scim/v2/Users" || true)
  [ "$code" = "200" ] && break
  sleep 10
done
[ "$code" = "200" ] && echo "The SCIM endpoint answers: OK" || echo "No answer yet (last status $code). Wait a few minutes, then open $URL/ui"

cat <<OUT

================ Save these in your password manager ================
Tenant URL (Entra):     $URL/scim/v2
Secret Token (Entra):   $TOKEN

If the app only offers OAuth2 Client Credentials Grant:
  Token Endpoint:       $URL/oauth/token
  Client ID:            $CLIENT_ID
  Client Secret:        $CLIENT_SECRET

Users and requests page: $URL/ui
  (sign in with any user name, and the Secret Token as the password)
=====================================================================
OUT
