FROM python:3.9-slim

WORKDIR /app

# 시스템 패키지 (gcloud CLI 폴백용 curl 포함)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 복사 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# 데이터·캐시 디렉토리 생성 (볼륨 마운트 전 기본 경로 보장)
RUN mkdir -p /app/data /root/.msp_history

EXPOSE 9070

CMD ["python3", "server.py"]
