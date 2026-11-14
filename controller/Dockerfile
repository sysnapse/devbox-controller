FROM alpine:3.21

# 只装必要件：python3 跑 controller、docker-cli 操作 socket、git 做 clone/push
RUN apk add --no-cache python3 docker-cli git openssh-client ca-certificates tzdata \
 && update-ca-certificates

ARG DCA_UID=1000
ARG DCA_GID=1000
# uid/gid 默认与模板里的 devbox user 一致（1000:1000），
# 这样 controller 写的档案 devbox 可以直接改；要改就 --build-arg DCA_UID=...
RUN addgroup -g ${DCA_GID} dca && adduser -D -u ${DCA_UID} -G dca dca

COPY controller/app.py /app/app.py
# 内置模板副本：挂载目录为空时兜底，保证 API 永远至少有三个模板
COPY templates/ /opt/templates/
RUN chmod 0555 /app/app.py && chmod -R a+rX /opt/templates && mkdir -p /work && chown ${DCA_UID}:${DCA_GID} /work

ENV PYTHONUNBUFFERED=1 \
    DCA_TEMPLATES_DIR=/templates \
    DCA_TEMPLATES_DIR_FALLBACK=/opt/templates \
    DCA_WORK_ROOT=/work \
    DCA_PORT=47123

USER dca
EXPOSE 47123
ENTRYPOINT ["python3", "/app/app.py"]
