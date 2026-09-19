# Pushing to Docker Hub

## One-time setup

1. Create a free account at [hub.docker.com](https://hub.docker.com) if you don't have one.
2. Create a repository named `running-leaderboard` (public or private).
3. Log in from the CLI:
   ```bash
   docker login
   ```

## Build and push

Always tag a version alongside `latest`, so a bad release can be rolled back
by pinning the previous tag in `docker-compose.yml`:

```bash
docker build -t adrianchen8662/running-leaderboard:v2.0 \
             -t adrianchen8662/running-leaderboard:latest .
```

Check the image actually starts before pushing it — this imports every module
inside the container, which catches a module missing from the `COPY` line:

```bash
docker run --rm -e DISCORD_TOKEN=x -e GEMINI_API_KEY=x \
  adrianchen8662/running-leaderboard:latest \
  python -c "import bot; print('imports ok')"
```

Then push both tags:

```bash
docker push adrianchen8662/running-leaderboard:v2.0
docker push adrianchen8662/running-leaderboard:latest
```

## Deploy on the VM

SSH into the VM, then:

```bash
docker compose pull
docker compose up -d
```

This pulls the new image and restarts the container with zero downtime for the volume.

## Environment variables

The container expects these to be set (via `.env` or your host environment):

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Discord developer portal |
| `GEMINI_API_KEY` | Gemini API key for run insights |

The database is stored in the `leaderboard_data` Docker volume and persists across restarts and image updates.
