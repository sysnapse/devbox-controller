FROM alpine:3.21

# 只装必要件：python3 + py3-yaml 解析模板、docker-cli 操作 socket、git 做 clone（push 由 devbox 內自行處理）
RUN apk add --no-cache python3 py3-yaml docker-cli git openssh-client ca-certificates tzdata \
 && update-ca-certificates

ARG DCA_UID=1000
ARG DCA_GID=1000
RUN addgroup -g ${DCA_GID} dca && adduser -D -u ${DCA_UID} -G dca dca

COPY controller/app.py /app/app.py
# 内置模板副本：挂载目录为空时兜底，保证 API 永远至少有三个模板
COPY templates/ /opt/templates/
# 2026-11-25: 不再建立 /work host work root；devbox 的 workspace 在容器內，隨 dc rm 消失。
RUN chmod 0555 /app/app.py && chmod -R a+rX /opt/templates

ENV PYTHONUNBUFFERED=1 \
    DCA_TEMPLATES_DIR=/templates \
    DCA_TEMPLATES_DIR_FALLBACK=/opt/templates \
    DCA_PORT=47123

USER dca
EXPOSE 47123
ENTRYPOINT ["python3", "/app/app.py"]
