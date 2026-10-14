FROM docker.m.daocloud.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/
RUN apt-get update && apt-get install -y --no-install-recommends nodejs git gcc libc6-dev && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/
USER 1000:1000
ENTRYPOINT ["python", "-B", "/app/ops/observer/observer.py", "--container", "--state", "/home/bestvcboy/dgx-thermal/state", "--record-root", "/home/bestvcboy/dgx-thermal-observer-history"]
