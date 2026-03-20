# GitHub Actions Setup for Automatic Docker Hub Publishing

This workflow automatically builds and publishes your Docker image to Docker Hub whenever you push to the `main` branch.

## Setup Instructions

### 1. Create Docker Hub Account (if you don't have one)
- Go to https://hub.docker.com
- Sign up for free
- Create a public repository called `autodub`

### 2. Create Docker Hub Access Token
1. Log in to Docker Hub
2. Go to Account Settings → Security
3. Click "New Access Token"
4. Name it: `github-actions`
5. Copy the token (you won't see it again!)

### 3. Add GitHub Secrets
1. Go to your GitHub repository
2. Settings → Secrets and variables → Actions
3. Click "New repository secret"
4. Add two secrets:

| Name | Value |
|------|-------|
| `DOCKER_USERNAME` | Your Docker Hub username |
| `DOCKER_PASSWORD` | The access token you created above |

### 4. Test the Workflow
1. Make a small change to your code
2. Push to main: `git push origin main`
3. Go to your GitHub repo → Actions tab
4. Watch the build happen automatically!
5. After ~5 minutes, check Docker Hub → your autodub repo
6. Your image is now available to pull!

---

## What the Workflow Does

When you push to `main`:
1. ✅ Checks out your code
2. ✅ Builds Docker image with `Dockerfile`
3. ✅ Tags it with:
   - `yourusername/autodub:main` (latest from main branch)
   - `yourusername/autodub:latest` (always the newest)
   - `yourusername/autodub:v1.0.0` (if you create git tags)
   - `yourusername/autodub:sha-abc123` (specific commit)
4. ✅ Pushes to Docker Hub
5. ✅ Updates your repo description on Docker Hub

---

## Usage After Setup

Once the workflow is running, anyone can use your image:

```bash
# Pull your Docker image
docker pull yourusername/autodub:latest

# Run it
docker run -d \
  -p 8080:8080 \
  --env-file .env \
  yourusername/autodub:latest
```

---

## Versioning

To create a specific version (e.g., v1.0.0):

```bash
# Tag your commit
git tag v1.0.0
git push origin v1.0.0

# Workflow automatically builds and tags:
# - yourusername/autodub:v1.0.0
# - yourusername/autodub:1.0
# - yourusername/autodub:latest
```

---

## Troubleshooting

### Build is failing
1. Check the Actions tab for error logs
2. Common issues:
   - Missing API keys in `.env` (but those shouldn't be in docker build)
   - Python dependency version conflicts
   - Missing system packages in Dockerfile

### Docker secrets not found
- Make sure secrets are named exactly: `DOCKER_USERNAME` and `DOCKER_PASSWORD`
- Secrets are case-sensitive!

### Image didn't push
- Check that Docker Hub secrets are set correctly
- Make sure your Docker Hub access token is valid

---

## Disabling Auto-Publish

If you want to disable automatic publishing, just comment out or delete the `.github/workflows/docker-publish.yml` file.

Or, to only publish on specific tags:
```yaml
on:
  push:
    tags:
      - 'v*'  # Only publish on version tags
```

---

## Private Docker Image (Optional)

To make your Docker image private:
1. Create a **private** repository on Docker Hub
2. The workflow will still work the same way
3. Users will need Docker Hub credentials to pull

---

## CI/CD for Tests (Optional)

You can also add tests:

```yaml
- name: Run Tests
  run: |
    docker build -t autodub:test .
    docker run autodub:test pytest tests/
```

Add this step before the push step to fail the build if tests fail.

---

That's it! Now you have fully automated Docker publishing. Just push your code and the image automatically goes to Docker Hub. 🚀
