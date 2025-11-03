# DigitalOcean Secrets Configuration Guide

**Complete guide for deploying SignalHire MCP Server to DigitalOcean App Platform with proper secrets management.**

## Overview

DigitalOcean App Platform provides encrypted secrets management that keeps your API keys secure. This guide shows you how to properly configure secrets instead of hardcoding them in deployment files.

## Why Use DigitalOcean Secrets?

✅ **Encrypted at rest** - Secrets are encrypted in DigitalOcean's database
✅ **Encrypted in transit** - Transmitted securely to your app
✅ **Never in git** - Secrets stay out of version control
✅ **Team access control** - Manage who can view/edit secrets
✅ **Easy rotation** - Update secrets without code changes
✅ **Audit logging** - Track who changed what and when

## Prerequisites

- DigitalOcean account: https://cloud.digitalocean.com
- `doctl` CLI installed (optional): https://docs.digitalocean.com/reference/doctl/
- SignalHire API key: https://www.signalhire.com/settings/api

---

## Step 1: Prepare Deployment Configuration

Your `app.yaml` should declare environment variables WITHOUT values:

```yaml
name: signalhire-callback
region: nyc

services:
  - name: callback-server
    github:
      repo: your-username/your-repo
      branch: main
      deploy_on_push: true

    dockerfile_path: Dockerfile

    http_port: 8000
    instance_count: 1
    instance_size_slug: basic-xxs

    # Environment variables
    envs:
      # SECRET - configured in DigitalOcean UI
      - key: SIGNALHIRE_API_KEY
        scope: RUN_TIME
        type: SECRET
        # DO NOT add 'value' here!

      # Non-secret configuration
      - key: HOST
        value: "0.0.0.0"
        scope: RUN_TIME

      - key: PORT
        value: "8000"
        scope: RUN_TIME

    health_check:
      http_path: /health
      initial_delay_seconds: 10
```

**Key points:**
- `type: SECRET` tells DigitalOcean this is encrypted
- No `value` field - that goes in the UI
- Safe to commit to git

---

## Step 2: Create App in DigitalOcean

### Option A: Using Web UI

1. Go to: https://cloud.digitalocean.com/apps
2. Click **Create App**
3. Choose **GitHub** as source
4. Select your repository and branch
5. Click **Next**
6. Configure:
   - **Name:** `signalhire-callback`
   - **Region:** Choose closest to your users
   - **Environment Variables:** We'll add secrets next
7. Click **Review** → **Create Resources**

### Option B: Using doctl CLI

```bash
# Install doctl if needed
brew install doctl  # macOS
# or
snap install doctl  # Linux

# Authenticate
doctl auth init

# Create app from spec
doctl apps create --spec app.yaml
```

---

## Step 3: Add Secrets in DigitalOcean UI

### Navigate to Environment Variables

1. Go to https://cloud.digitalocean.com/apps
2. Click on your app: `signalhire-callback`
3. Click **Settings** tab
4. Scroll to **App-Level Environment Variables**
5. Click **Edit**

### Add SIGNALHIRE_API_KEY Secret

1. Click **Add Variable**
2. Configure:
   - **Key:** `SIGNALHIRE_API_KEY`
   - **Value:** [Paste your actual API key from SignalHire]
   - **Type:** **Secret** ⚠️ IMPORTANT: Select "Secret" not "Variable"
   - **Scope:** All Components (or specific component)
3. Click **Save**

**Visual confirmation:**
- You should see 🔒 lock icon next to the variable
- Value should show as `••••••••••`

### Add Optional Secrets (if needed)

Repeat the process for other secrets:

| Key | Type | Description |
|-----|------|-------------|
| `MEM0_API_KEY` | Secret | Mem0 memory platform key |
| `SUPABASE_KEY` | Secret | Supabase service role key |

**Non-secret variables** (can use type: Variable):

| Key | Type | Value | Description |
|-----|------|-------|-------------|
| `EXTERNAL_CALLBACK_URL` | Variable | Your public URL | Webhook endpoint |
| `HOST` | Variable | `0.0.0.0` | Server bind address |
| `PORT` | Variable | `8000` | Server port |

---

## Step 4: Deploy the App

### Trigger Deployment

After adding secrets:

1. Click **Deploy** in the DigitalOcean UI
2. Or push to GitHub (if `deploy_on_push: true`)

### Monitor Deployment

```bash
# Using doctl
doctl apps list
doctl apps get <APP_ID>

# View logs
doctl apps logs <APP_ID> --type run

# Or in UI
# Go to app → Runtime Logs tab
```

### Verify Secrets Loaded

Check logs for:
```
✓ SIGNALHIRE_API_KEY loaded from environment
✓ Server starting on 0.0.0.0:8000
```

**Should NOT see:**
```
✗ SIGNALHIRE_API_KEY not found
```

---

## Step 5: Test the Deployment

### Health Check

```bash
curl https://your-app.ondigitalocean.app/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "signalhire-callback",
  "timestamp": "2025-11-02T15:30:00Z"
}
```

### Test Webhook Endpoint

```bash
curl -X POST https://your-app.ondigitalocean.app/signalhire/callback \
  -H "Content-Type: application/json" \
  -d '[{"status":"success","item":"test"}]'
```

Expected response:
```json
{
  "status": "accepted"
}
```

---

## Using doctl for Secrets Management

### List All Environment Variables

```bash
# Get app ID
APP_ID=$(doctl apps list --format ID --no-header | head -1)

# View app spec (secrets will show as placeholders)
doctl apps spec get $APP_ID
```

### Update Secrets via CLI

```bash
# Create updated spec with new secret
cat > app-update.yaml <<EOF
name: signalhire-callback
envs:
  - key: SIGNALHIRE_API_KEY
    value: "new_api_key_here"
    scope: RUN_TIME
    type: SECRET
EOF

# Update app
doctl apps update $APP_ID --spec app-update.yaml
```

**Security note:** When using doctl, the secret is in the command history. Clear it:
```bash
history -d $(history 1)  # Bash
history delete --last    # Fish
```

---

## Rotating Secrets

### When to Rotate

- 🔴 **IMMEDIATELY** if secret was exposed (committed to git, leaked, etc.)
- 🟡 **Quarterly** as best practice
- 🟢 **On employee departure** who had access

### How to Rotate

1. **Generate new API key** in SignalHire dashboard
2. **Test new key locally:**
   ```bash
   export SIGNALHIRE_API_KEY=new_key_here
   python3 test_configuration.py
   ```
3. **Update in DigitalOcean:**
   - Go to Settings → Environment Variables
   - Edit `SIGNALHIRE_API_KEY`
   - Paste new key
   - Save
4. **Redeploy app** (automatic or manual)
5. **Verify deployment** works with new key
6. **Revoke old key** in SignalHire dashboard

---

## Security Best Practices

### 1. Never Log Secrets

**Bad:**
```python
logger.info(f"Using API key: {os.getenv('SIGNALHIRE_API_KEY')}")
```

**Good:**
```python
api_key = os.getenv('SIGNALHIRE_API_KEY')
logger.info(f"Using API key: {'*' * 10}...")
```

### 2. Validate Secret Format

```python
def validate_api_key(key: str) -> bool:
    """Validate SignalHire API key format"""
    if not key:
        logger.error("API key not configured")
        return False

    if not key.startswith("202."):
        logger.error("Invalid API key format")
        return False

    logger.info(f"API key validated: {key[:8]}...")
    return True
```

### 3. Use Least Privilege

- Don't use admin keys if read-only suffices
- Create separate keys for dev/staging/prod
- Limit key permissions in SignalHire dashboard

### 4. Monitor Usage

Set up alerts for:
- Unusual API call patterns
- Unexpected credit usage
- Failed authentication attempts

---

## Troubleshooting

### Problem: "SIGNALHIRE_API_KEY not found"

**Cause:** Secret not configured in DigitalOcean

**Solution:**
1. Check Settings → Environment Variables
2. Verify `SIGNALHIRE_API_KEY` exists with 🔒 icon
3. Verify type is "Secret" not "Variable"
4. Redeploy app

### Problem: Secret shows in logs

**Cause:** Application is logging the secret value

**Solution:**
1. Search code for: `print(.*SIGNALHIRE_API_KEY)`
2. Remove or redact logging statements
3. Use `logger.info(f"Key: {'*' * 10}")` instead

### Problem: App won't deploy after adding secret

**Cause:** Invalid secret format or missing required secrets

**Solution:**
1. Check Runtime Logs for specific error
2. Verify API key is valid in SignalHire dashboard
3. Test key locally first
4. Check for typos in variable name

### Problem: Secret updated but app still uses old value

**Cause:** App needs restart to reload environment

**Solution:**
1. Trigger manual deployment in DigitalOcean UI
2. Or: Settings → Force Rebuild and Deploy

---

## Environment Variables Reference

### Required Secrets

| Variable | Type | Description | Get From |
|----------|------|-------------|----------|
| `SIGNALHIRE_API_KEY` | Secret | API authentication | [SignalHire Settings](https://www.signalhire.com/settings/api) |

### Optional Secrets

| Variable | Type | Description | Get From |
|----------|------|-------------|----------|
| `MEM0_API_KEY` | Secret | Mem0 memory platform | [Mem0 Dashboard](https://app.mem0.ai) |
| `SUPABASE_KEY` | Secret | Supabase service key | Supabase Settings → API |

### Configuration (Non-Secret)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `EXTERNAL_CALLBACK_URL` | Variable | - | Public webhook URL |
| `HOST` | Variable | `0.0.0.0` | Server bind address |
| `PORT` | Variable | `8000` | Server port |

---

## Related Documentation

- [SECRETS_MANAGEMENT.md](../SECRETS_MANAGEMENT.md) - Complete secrets management guide
- [SECURITY_INCIDENT.md](../SECURITY_INCIDENT.md) - What to do if secret is exposed
- [DEPLOY.md](./DEPLOY.md) - General deployment guide
- [DigitalOcean App Platform Docs](https://docs.digitalocean.com/products/app-platform/)
- [DigitalOcean Environment Variables](https://docs.digitalocean.com/products/app-platform/how-to/use-environment-variables/)

---

## Quick Reference Commands

```bash
# List apps
doctl apps list

# Get app details
doctl apps get <APP_ID>

# View logs
doctl apps logs <APP_ID> --type run --follow

# Trigger deployment
doctl apps create-deployment <APP_ID>

# Update app spec
doctl apps update <APP_ID> --spec app.yaml

# Delete app (careful!)
doctl apps delete <APP_ID>
```

---

**Remember:** Secrets should NEVER appear in:
- ✗ Git commits
- ✗ Code files
- ✗ YAML files (except as `type: SECRET` without value)
- ✗ Logs
- ✗ Documentation
- ✗ Chat messages

Secrets should ONLY be in:
- ✓ DigitalOcean encrypted secrets
- ✓ Local `.env` files (in .gitignore)
- ✓ Password managers
- ✓ Encrypted secret vaults
