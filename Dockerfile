FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY jarvis ./jarvis
RUN pip install --no-cache-dir . && useradd --create-home jarvis
USER jarvis
EXPOSE 8000
CMD ["uvicorn", "jarvis.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
