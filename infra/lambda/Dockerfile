FROM python:3.12-slim

# AWS Lambda Web Adapter: a Lambda extension that terminates Lambda events
# and forwards them as HTTP requests to an app server on $PORT. Lets us run
# the exact same server.py we use locally, unchanged, as a Lambda function.
# Pinned version so cold-start behavior doesn't drift.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 \
     /lambda-adapter /opt/extensions/lambda-adapter

# The adapter reads PORT to know where to forward. Our server defaults to 8000.
ENV PORT=8000

WORKDIR /app

# Only the files the runtime actually needs. No tests, no bench, no docs.
# Keep the image lean -> faster cold starts.
COPY graph_builder.py main.py components.py find_components.py server.py ./
COPY web/ ./web/
COPY sample_data/ ./sample_data/

# Env defaults for Lambda. Override via CDK if the data files move.
ENV PG_DICTIONARY=/app/sample_data/dictionary.txt
ENV PG_THESAURUS=/app/sample_data/thesaurus.json
ENV PG_HOST=0.0.0.0
ENV PG_PORT=8000

CMD ["python", "server.py"]
