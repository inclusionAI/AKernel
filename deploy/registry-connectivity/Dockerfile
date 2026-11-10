FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY --chmod=0444 requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=0444 check.py ./check.py
USER 65532:65532
ENTRYPOINT ["python", "/app/check.py"]
