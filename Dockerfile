# One container: FastAPI serves the JSON API and the web UI.
# Works on Hugging Face Spaces (port 7860, non-root user 1000) and on any container host (set PORT).
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/home/user/app/models \
    PORT=7860
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

# Bake the knowledge base, the ONNX models and the embedding cache into the image, so a cold start
# only loads files: no crawling and no embedding at runtime. The crawl is skipped when the pages were
# supplied with the code (deploy_hf.py --include-kb, private Spaces only) and fails the build if core
# policy pages can't be fetched, rather than shipping a degraded bot.
RUN if ls knowledge_base/amazon_in/*.md >/dev/null 2>&1; then echo "Using supplied knowledge base"; \
    else python -m scripts.fetch_kb; fi \
 && python -c "from shopassist.retrieval import KnowledgeBase; KnowledgeBase().search('warm up')"

EXPOSE 7860
CMD ["sh", "-c", "uvicorn shopassist.api:app --host 0.0.0.0 --port ${PORT}"]
