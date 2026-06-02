FROM python:3.10-slim
WORKDIR /code
COPY . /code/
RUN find . -name "requirements.txt" -exec pip install --no-cache-dir -r {} \;
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
