# python312 —— Python 3.12 开发环境（L1）
name: python312
image: python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
workdir: /workspace
network: default
cpus: 2
memory: 4g
pids: 512
user: "1000:1000"
