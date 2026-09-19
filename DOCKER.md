# Pushing to Docker Hub

## One-time setup

1. Create a free account at [hub.docker.com](https://hub.docker.com) if you don't have one.
2. Create a repository named `running-leaderboard` (public or private).
3. Log in from the CLI:
   ```bash
   docker login
   ```

## Build and push

Replace `yourusername` with your Docker Hub username.

```bash
docker build -t adrianchen8662/running-leaderboard:latest .
docker push adrianchen8662/running-leaderboard:latest
```

Tag a versioned release alongside `latest`:

```bash
docker build -t yourusername/running-leaderboard:v1.0 \
             -t yourusername/running-leaderboard:latest .
docker push yourusername/running-leaderboard:v1.0
docker push yourusername/running-leaderboard:latest
```

## Update docker-compose.yml

Replace the placeholder image name with your real one:

```yaml
image: yourusername/running-leaderboard:latest
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
