FROM python:3.12-slim

# Only dev-time dep. Runtime code is stdlib-only.
# Pinned to match what passed on the dev machine.
RUN pip install --no-cache-dir pytest==8.4.2

WORKDIR /app

# Copy the whole project (respecting .dockerignore).
# For interactive dev, override with `-v "$PWD":/app` at run time.
COPY . .

# Port the HTTP server binds to (PG_PORT). Must be published with -p at run time.
EXPOSE 8000

# Drop into a shell by default. Individual commands override:
#   docker run --rm IMAGE pytest -q
#   docker run --rm -p 8000:8000 IMAGE python server.py
#   docker run --rm -v "$PWD/out":/app/out IMAGE python find_components.py ...
CMD ["bash"]
